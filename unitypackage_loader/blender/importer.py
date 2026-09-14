"""インポート全体のオーケストレーション。

``prepare_package`` でパッケージを走査・解析し（モデル選択ダイアログにも使う）、
``run_import`` で実際の展開・FBX 読み込み・マテリアル構築を行う。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from ..core.arrange import arrange_offsets
from ..core.hierarchy import (
    CLASS_CAMERA,
    CLASS_LIGHT,
    Expander,
    Hierarchy,
    HierarchyError,
    effective_active,
    parse_asset,
    summarize,
)
from ..core.hierarchy import components as scene_components
from ..core.lights import LIGHT_CAMERA_BASIS, BlenderCamera, BlenderLight, convert_camera, convert_light, detect_pipeline
from ..core.unity_yaml import UnityRef
from ..core.fbx_units import read_unit_scale
from ..core.transform import BLENDER_TO_UNITY, UNITY_TO_BLENDER, trs, unity_to_blender
from ..core.mapping import resolve_materials, slot_assignments, submesh_slot_order
from ..core.material import MaterialParseError, NormalizedMaterial, UnityMaterial, parse_material
from ..core.meta import ModelImporterInfo, TextureImporterInfo, strip_numeric_suffix
from ..core.package import AssetEntry, PackageError, UnityPackage
from ..core.prefab import (
    PrefabDocument,
    RendererMaterials,
    merge_prefab_tables,
    parse_prefab,
    resolve_renderers,
    tables_by_model,
    unresolved_material_overrides,
)
from ..core.profiles import ShaderTable, normalize_material
from ..core.profiles.base import default_table
from ..core.report import ImportReport, MaterialReport
from ..core.units import (
    UNIT_PREFABS,
    UNIT_SCENES,
    PrefabSummary,
    SceneSummary,
    choice_count,
    summarize_prefabs,
    summarize_scene,
)
from . import materials as mat_builder
from . import outline as outline_builder
from .textures import load_image

_ROOT_PACKAGE = __package__.rsplit(".", 1)[0]  # bl_ext.<repo>.unitypackage_loader

# 直近のインポート結果（N パネル表示用）
LAST_REPORT: ImportReport | None = None

# 読み込めるモデル形式と、同じフォルダから一緒に展開する付随ファイル
SUPPORTED_MODEL_EXTS = frozenset({".fbx", ".obj", ".gltf", ".glb", ".vrm", ".dae", ".blend"})
_SIDECAR_EXTS = {".obj": {".mtl"}, ".gltf": {".bin"}}


@dataclass
class ImportOptions:
    models: str = "ASK"  # ASK / ALL / FIRST
    unit: str = "MODELS"  # 読み込む単位: MODELS / PREFABS
    model_guids: list[str] | None = None  # 明示的に選ばれたモデル（ダイアログ経由）
    prefab_paths: list[str] | None = None  # 明示的に選ばれた prefab の pathname（ダイアログ経由）
    scene_paths: list[str] | None = None  # 明示的に選ばれたシーンの pathname（ダイアログ経由）
    scene_lights: bool = True  # シーンのライトを読み込む
    scene_cameras: bool = True  # シーンのカメラを読み込む
    arrange: str = "SIDE_BY_SIDE"  # prefab を複数読み込むときの並べ方: SIDE_BY_SIDE / STACK
    material_mode: str = mat_builder.MODE_AUTO
    force_opaque: bool = False
    backface_culling: bool = True
    use_normal_maps: bool = True
    use_emission: bool = True
    reuse_existing: bool = False
    store_props: bool = True
    extract_mode: str = "BESIDE_BLEND"  # BESIDE_BLEND / CACHE / CUSTOM
    extract_path: str = ""
    pack_images: bool = False
    import_unreferenced: bool = False
    overwrite_extracted: bool = False
    max_extract_size: int = 0  # 1 回のインポートで展開する合計バイト数の上限（0 は無制限。Preferences から）
    fbx_importer: str = "AUTO"  # AUTO / NEW / LEGACY
    use_anim: bool = False
    ignore_leaf_bones: bool = True
    global_scale: float = 1.0
    import_blend: bool = False  # 同梱 .blend を append するか（Python スクリプトを含み得るので既定 OFF）
    blend_materials: str = "KEEP"  # .blend 同梱モデル: KEEP（既存マテリアルを残す）/ REBUILD
    use_vrm_addon: bool = True  # .vrm は VRM add-on（extensions.blender.org の "VRM format"）があればそちらで読む
    outlines: bool = False  # Unity のアウトライン設定を Solidify で再現する
    outline_width_scale: float = 0.01
    shader_table_path: str = ""
    extra_fbx_kwargs: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 事前解析
# ---------------------------------------------------------------------------


# 同梱 .blend を読まない理由（警告文とダイアログ表示に使う）
BLEND_DISABLED_REASON = (
    "bundled .blend files are not imported unless 'Import Bundled .blend Files' is enabled "
    "(a .blend can contain Python scripts)"
)


@dataclass
class ModelSummary:
    entry: AssetEntry
    material_count: int  # externalObjects に登録されたマテリアル数
    resolved_count: int  # そのうちパッケージ内の .mat に対応付けできた数
    supported: bool
    skip_reason: str = ""  # supported が False の理由

    @property
    def guid(self) -> str:
        return self.entry.guid


@dataclass
class PreparedPackage:
    path: Path
    pkg: UnityPackage
    unity_mats: dict[str, UnityMaterial]
    normalized: dict[str, NormalizedMaterial]
    models: list[ModelSummary]
    referenced_textures: set[str]
    missing_textures: set[str]
    # prefab の pathname → モデル GUID → GameObject 名をキーにした表
    prefab_tables: dict[str, dict[str, dict[str, RendererMaterials]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    prefabs: list[PrefabSummary] = field(default_factory=list)  # 読み込む単位 Prefabs の候補（pathname 順）
    scenes: list[SceneSummary] = field(default_factory=list)  # 読み込む単位 Scenes の候補（pathname 順）
    scene_hierarchies: dict[str, Hierarchy] = field(default_factory=dict)  # シーンの GUID → 展開した階層
    pipeline: str = "BUILTIN"  # マテリアルから判定したレンダーパイプライン（ライトの強さの換算に使う）

    @property
    def supported_models(self) -> list[ModelSummary]:
        return [m for m in self.models if m.supported]

    @property
    def supported_prefabs(self) -> list[PrefabSummary]:
        return [p for p in self.prefabs if p.supported]

    @property
    def supported_scenes(self) -> list[SceneSummary]:
        return [s for s in self.scenes if s.supported]

    @property
    def choice_count(self) -> int:
        """ダイアログで選べる（読み込める）候補の総数。"""
        return choice_count(self.prefabs, len(self.supported_models), self.scenes)

    def table_for(self, model_guid: str) -> dict[str, RendererMaterials]:
        """Models 単位でモデルに当てはめる表。そのモデルを使う prefab をパス順に先勝ちで統合したもの。"""
        candidates = sorted(p for p, tables in self.prefab_tables.items() if model_guid in tables)
        return merge_prefab_tables([self.prefab_tables[p][model_guid] for p in candidates])


def build_shader_table(extra_path: str = "") -> ShaderTable:
    table = default_table()
    if extra_path:
        path = Path(bpy.path.abspath(extra_path))
        if path.is_file():
            merged = ShaderTable()
            extra = ShaderTable(path)
            merged.by_guid.update(extra.by_guid)
            merged.by_builtin_id.update(extra.by_builtin_id)
            return merged
    return table


def _model_info(entry: AssetEntry, warn: Callable[[str], None]) -> ModelImporterInfo:
    """モデルの .meta を読む。壊れていても（構文エラー・ネスト過多）そのモデルだけ既定値で続ける。"""
    if not entry.meta_text:
        return ModelImporterInfo()
    try:
        return ModelImporterInfo.from_meta(entry.meta_text)
    except ValueError as exc:
        warn(f"could not parse .meta of {entry.pathname}: {exc}")
        return ModelImporterInfo()


def _texture_info(entry: AssetEntry, warn: Callable[[str], None]) -> TextureImporterInfo:
    if not entry.meta_text:
        return TextureImporterInfo()
    try:
        return TextureImporterInfo.from_meta(entry.meta_text)
    except ValueError as exc:
        warn(f"could not parse .meta of {entry.pathname}: {exc}")
        return TextureImporterInfo()


def prepare_package(
    filepath: str, shader_table: ShaderTable | None = None, *, import_blend: bool = False
) -> PreparedPackage:
    """パッケージを走査して解析する。``import_blend`` が False なら同梱 .blend は「読み込まない」扱いにする。"""
    path = Path(filepath)
    pkg = UnityPackage(path)
    pkg.scan()
    table = shader_table or default_table()
    warnings: list[str] = list(pkg.warnings)

    unity_mats: dict[str, UnityMaterial] = {}
    normalized: dict[str, NormalizedMaterial] = {}
    for entry in pkg.materials():
        try:
            umat = parse_material(pkg.read_asset(entry.guid), entry.guid, entry.pathname)
        except (MaterialParseError, ValueError, PackageError) as exc:
            warnings.append(f"could not parse {entry.pathname}: {exc}")
            continue
        unity_mats[entry.guid] = umat
        normalized[entry.guid] = normalize_material(umat, table)

    models: list[ModelSummary] = []
    for entry in pkg.models():
        info = _model_info(entry, warnings.append)
        names = list(info.external_materials)
        resolution = resolve_materials(names, info, unity_mats, entry.pathname)
        resolved = sum(1 for r in resolution.values() if r.guid)
        skip_reason = ""
        if entry.ext not in SUPPORTED_MODEL_EXTS:
            skip_reason = "model format not supported yet"
        elif entry.ext == ".blend" and not import_blend:
            skip_reason = BLEND_DISABLED_REASON
        models.append(ModelSummary(entry, len(names), resolved, not skip_reason, skip_reason))
    if not models:
        raise PackageError("the package contains no model files (.fbx/.obj/.gltf/.glb/.vrm/.dae/.blend)")

    referenced: set[str] = set()
    for norm in normalized.values():
        referenced.update(t.guid for t in norm.texture_refs())
        referenced.update(norm.extra_texture_guids())
    missing = {g for g in referenced if pkg.get(g) is None}

    # Prefab Variant / ネストの元をたどれるよう、先に全 prefab を読んでから Renderer を解決する
    documents: dict[str, PrefabDocument] = {}
    for entry in pkg.prefabs():
        try:
            documents[entry.guid.lower()] = parse_prefab(pkg.read_asset(entry.guid))
        except Exception as exc:  # noqa: BLE001 - prefab は補助情報なので失敗しても続ける
            warnings.append(f"could not parse prefab {entry.pathname}: {exc}")
    model_guids = [m.guid for m in models]
    resolved: dict[str, dict[int, RendererMaterials]] = {}
    prefab_tables: dict[str, dict[str, dict[str, RendererMaterials]]] = {}
    for entry in pkg.prefabs():
        if entry.guid.lower() not in documents:
            continue
        unresolved = unresolved_material_overrides(entry.guid, documents, resolved)
        if unresolved:
            warnings.append(
                f"prefab {entry.pathname}: {unresolved} material override(s) on objects inside a model "
                "are not read yet; the model's own assignments are used for them"
            )
        tables = tables_by_model(resolve_renderers(entry.guid, documents, resolved).values(), model_guids)
        if tables:
            prefab_tables[entry.pathname] = tables
    unsupported = {m.guid: m.skip_reason for m in models if not m.supported}
    prefabs = summarize_prefabs(((e.guid, e.pathname) for e in pkg.prefabs()), prefab_tables, model_guids, unsupported)
    scenes, hierarchies = _prepare_scenes(pkg, models, warnings.append, unsupported)
    return PreparedPackage(
        path, pkg, unity_mats, normalized, models, referenced, missing, prefab_tables, warnings,
        prefabs=prefabs, scenes=scenes, scene_hierarchies=hierarchies,
        pipeline=detect_pipeline(n.family for n in normalized.values()),
    )


def _prepare_scenes(
    pkg: UnityPackage, models: list[ModelSummary], warn: Callable[[str], None], unsupported: dict[str, str]
) -> tuple[list[SceneSummary], dict[str, Hierarchy]]:
    """シーンを展開して候補にする。prefab の展開結果はシーンの間で共有する。"""
    entries = pkg.scenes()
    if not entries:
        return [], {}
    prefab_guids = {e.guid for e in pkg.prefabs()}
    model_names = {m.guid: Path(m.entry.pathname).stem for m in models}

    def read(guid: str):
        if guid not in prefab_guids:
            return None
        try:
            return parse_asset(pkg.read_asset(guid))
        except (ValueError, PackageError) as exc:
            warn(f"could not parse prefab {pkg.get(guid).pathname}: {exc}")
            return None

    # 古い形式の .meta の fileIDToRecycleName で、モデルの中への上書きを名前に結び付ける（#53）
    recycle = {m.guid: _model_info(m.entry, warn).recycle_names for m in models}
    expander = Expander(read, model_names, recycle)
    scenes: list[SceneSummary] = []
    hierarchies: dict[str, Hierarchy] = {}
    for entry in entries:
        contents = None
        try:
            hierarchy = expander.expand_raw(parse_asset(pkg.read_asset(entry.guid)))
            contents = summarize(hierarchy, model_names)
            hierarchies[entry.guid] = hierarchy
        except (HierarchyError, ValueError, PackageError, RecursionError) as exc:
            warn(f"could not read scene {entry.pathname}: {exc}")
        scenes.append(summarize_scene(entry.guid, entry.pathname, contents, unsupported))
    return scenes, hierarchies


# ---------------------------------------------------------------------------
# 展開先
# ---------------------------------------------------------------------------


def cache_root() -> Path:
    return Path(bpy.utils.extension_path_user(_ROOT_PACKAGE, path="cache", create=True))


def resolve_extract_root(opts: ImportOptions, package_path: Path) -> Path:
    stem = package_path.stem
    if opts.extract_mode == "CUSTOM" and opts.extract_path:
        return Path(bpy.path.abspath(opts.extract_path)) / stem
    if opts.extract_mode == "BESIDE_BLEND" and bpy.data.filepath:
        return Path(bpy.data.filepath).parent / "textures" / stem
    return cache_root() / stem


# ---------------------------------------------------------------------------
# bpy.data の差分取得
# ---------------------------------------------------------------------------

_TRACKED = ("objects", "materials", "images", "meshes", "armatures", "actions", "collections")


def _snapshot() -> dict[str, set[str]]:
    return {name: {d.name for d in getattr(bpy.data, name)} for name in _TRACKED}


def _new_since(before: dict[str, set[str]]) -> dict[str, list]:
    result = {}
    for name in _TRACKED:
        coll = getattr(bpy.data, name)
        result[name] = [d for d in coll if d.name not in before[name]]
    return result


def _adopt_into_collection(new: dict[str, list], scene, collection) -> None:
    """インポーターがシーンのルートコレクションに直接入れたオブジェクト・コレクションをパッケージ用コレクションに移す。

    VRM add-on はアクティブコレクションを無視してルートに入れ、コライダー用のコレクションも作る。
    """
    root = scene.collection
    for coll in new["collections"]:
        if coll is collection or coll.name not in root.children:
            continue
        root.children.unlink(coll)
        if coll.name not in collection.children:
            collection.children.link(coll)
    for obj in new["objects"]:
        if obj.name in root.objects:
            root.objects.unlink(obj)
            if obj.name not in collection.objects:
                collection.objects.link(obj)


def vrm_addon_available() -> bool:
    """VRM add-on（import_scene.vrm）が登録されているか。bpy.ops の属性は常に存在するので bpy.types で見る。"""
    return hasattr(bpy.types, "IMPORT_SCENE_OT_vrm")


def _find_layer_collection(layer_coll, target):
    if layer_coll.collection == target:
        return layer_coll
    for child in layer_coll.children:
        found = _find_layer_collection(child, target)
        if found is not None:
            return found
    return None


def _sidecar_guids(pkg: UnityPackage, model: AssetEntry) -> list[str]:
    """OBJ の .mtl、glTF の .bin など、モデルと同じフォルダに置くべきファイルの GUID。"""
    exts = _SIDECAR_EXTS.get(model.ext)
    if not exts:
        return []
    folder = model.pathname.rsplit("/", 1)[0]
    return [
        e.guid
        for e in pkg.entries.values()
        if e.has_asset and e.ext in exts and e.pathname.rsplit("/", 1)[0] == folder
    ]


def _import_fbx(context, path: Path, opts: ImportOptions) -> None:
    use_new = hasattr(bpy.ops.wm, "fbx_import") and opts.fbx_importer in ("AUTO", "NEW")
    if use_new:
        kwargs = dict(
            filepath=str(path),
            global_scale=opts.global_scale,
            use_anim=opts.use_anim,
            ignore_leaf_bones=opts.ignore_leaf_bones,
            use_custom_normals=True,
        )
        kwargs.update(opts.extra_fbx_kwargs)
        result = bpy.ops.wm.fbx_import(**kwargs)
    else:
        kwargs = dict(
            filepath=str(path),
            global_scale=opts.global_scale,
            use_anim=opts.use_anim,
            ignore_leaf_bones=opts.ignore_leaf_bones,
            use_custom_normals=True,
            use_image_search=False,
        )
        kwargs.update(opts.extra_fbx_kwargs)
        result = bpy.ops.import_scene.fbx(**kwargs)
    if "FINISHED" not in result:
        raise RuntimeError(f"FBX import failed for {path.name}: {result}")


def _import_blend(context, path: Path, collection: bpy.types.Collection) -> None:
    """同梱 .blend の全オブジェクトを append してコレクションに入れる。"""
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        data_to.objects = list(data_from.objects)
    for obj in data_to.objects:
        if obj is not None:
            collection.objects.link(obj)


def _delegate_to_vrm_addon(model: AssetEntry, opts: ImportOptions) -> bool:
    return model.ext == ".vrm" and opts.use_vrm_addon and vrm_addon_available()


def _import_model(context, path: Path, model: AssetEntry, opts: ImportOptions, collection, report: ImportReport) -> bool:
    """モデルを読み込む。マテリアルを外部インポーターに任せた（こちらで組み直さない）場合は True。"""
    ext = model.ext
    if ext == ".fbx":
        _import_fbx(context, path, opts)
        return False
    if ext == ".obj":
        result = bpy.ops.wm.obj_import(filepath=str(path), global_scale=opts.global_scale)
    elif ext == ".vrm" and _delegate_to_vrm_addon(model, opts):
        # VRM add-on が MToon・Humanoid・スプリングボーンまで再現するので、マテリアルも add-on のものを使う。
        # use_addon_preferences=True で利用者の add-on 設定（テクスチャ展開先など）に従う
        result = bpy.ops.import_scene.vrm(filepath=str(path), use_addon_preferences=True)
        if "FINISHED" not in result:
            raise RuntimeError(f"VRM add-on import failed for {path.name}: {result}")
        return True
    elif ext in (".gltf", ".glb", ".vrm"):
        if ext == ".vrm" and opts.use_vrm_addon:
            report.warn(
                f"VRM add-on is not installed; {model.name} was imported with the glTF importer "
                "and MToon materials were rebuilt from .mat files (install the 'VRM format' add-on for full fidelity)"
            )
        # .vrm は glTF バイナリなので標準の glTF インポーターで読める（VRM 拡張は無視される）
        result = bpy.ops.import_scene.gltf(filepath=str(path))
    elif ext == ".dae":
        result = bpy.ops.wm.collada_import(filepath=str(path))
    elif ext == ".blend":
        _import_blend(context, path, collection)
        return False
    else:
        raise RuntimeError(f"unsupported model format: {model.pathname}")
    if "FINISHED" not in result:
        raise RuntimeError(f"import failed for {path.name}: {result}")
    return False


def _select_models(prepared: PreparedPackage, opts: ImportOptions) -> list[ModelSummary]:
    supported = prepared.supported_models
    if opts.model_guids is not None:
        wanted = set(opts.model_guids)
        return [m for m in supported if m.guid in wanted]
    if opts.models == "FIRST":
        return supported[:1]
    return supported


def _select_prefabs(prepared: PreparedPackage, opts: ImportOptions) -> list[PrefabSummary]:
    supported = prepared.supported_prefabs
    if opts.prefab_paths is not None:
        wanted = set(opts.prefab_paths)
        return [p for p in supported if p.pathname in wanted]
    if opts.models == "FIRST":
        return supported[:1]
    return supported


@dataclass
class _ImportGroup:
    """1 つのコレクションにまとめて読み込むモデルと、それぞれに当てはめる prefab の表。"""

    prefab: PrefabSummary | None  # Models 単位なら None（パッケージのコレクションに直接入れる）
    models: list[tuple[ModelSummary, dict[str, RendererMaterials]]]


def _plan_groups(prepared: PreparedPackage, opts: ImportOptions) -> list[_ImportGroup]:
    if opts.unit != UNIT_PREFABS:
        return [_ImportGroup(None, [(m, prepared.table_for(m.guid)) for m in _select_models(prepared, opts)])]
    supported = {m.guid: m for m in prepared.supported_models}
    groups = []
    for prefab in _select_prefabs(prepared, opts):
        tables = prepared.prefab_tables.get(prefab.pathname, {})
        models = [(supported[g], tables[g]) for g in prefab.model_guids if g in supported and g in tables]
        if models:
            groups.append(_ImportGroup(prefab, models))
    return groups


def _collection_bounds(collection: bpy.types.Collection):
    """コレクション内のメッシュのワールド座標での外形。メッシュが無ければ None。"""
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    for obj in collection.all_objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            point = obj.matrix_world @ Vector(corner)
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
    return (tuple(lo), tuple(hi)) if lo[0] <= hi[0] else None


def _arrange_collections(context, collections: list[bpy.types.Collection]) -> None:
    """prefab ごとのコレクションを、外形が重ならないように並べる（親を持たないオブジェクトを動かす）。"""
    context.view_layer.update()
    offsets = arrange_offsets([_collection_bounds(c) for c in collections])
    for coll, (dx, dy) in zip(collections, offsets):
        if not (dx or dy):
            continue
        for obj in coll.all_objects:
            if obj.parent is None:
                obj.location.x += dx
                obj.location.y += dy


def _select_scenes(prepared: PreparedPackage, opts: ImportOptions) -> list[SceneSummary]:
    supported = prepared.supported_scenes
    if opts.scene_paths is not None:
        wanted = set(opts.scene_paths)
        return [s for s in supported if s.pathname in wanted]
    if opts.models == "FIRST":
        return supported[:1]
    return supported


@dataclass
class _SceneTemplate:
    """シーンで最初に読み込んだモデルのオブジェクトと、原点にあったときの状態（複製と位置の補正に使う）。"""

    objects: list[bpy.types.Object]
    basis: dict[bpy.types.Object, Matrix]
    hide_render: dict[bpy.types.Object, bool]

    @classmethod
    def capture(cls, objects: list[bpy.types.Object]) -> _SceneTemplate:
        # matrix_world は depsgraph の評価が要るので持たない（数千回の読み込みでシーン全体を評価し直すと重い）
        return cls(list(objects), {o: o.matrix_basis.copy() for o in objects}, {o: o.hide_render for o in objects})


def _scene_empty(hierarchy: Hierarchy, key: int, collection, empties: dict[int, bpy.types.Object], active: dict[int, bool]):
    """Node と、まだ作っていない祖先の Empty を作り、Node の Empty を返す（Unity の親子関係を再現する）。"""
    path: list[int] = []
    seen: set[int] = set()
    current: int | None = key
    while current is not None and current in hierarchy.nodes and current not in empties and current not in seen:
        path.append(current)
        seen.add(current)
        current = hierarchy.nodes[current].parent
    parent = empties.get(current) if current is not None else None
    for node_key in reversed(path):
        node = hierarchy.nodes[node_key]
        empty = bpy.data.objects.new(node.name or "GameObject", None)
        empty.empty_display_type = "PLAIN_AXES"
        empty.empty_display_size = 0.1
        collection.objects.link(empty)
        empty.parent = parent
        empty.matrix_basis = Matrix(unity_to_blender(node.local))
        empty["unity_game_object"] = node.name
        if not active.get(node_key, True):
            empty.hide_set(True)
            empty.hide_render = True
        empties[node_key] = empty
        parent = empty
    return empties.get(key, parent)


def _json_text(body: dict) -> str:
    """コンポーネントの中身をカスタムプロパティ用の JSON にする（参照は fileID / guid の辞書にする）。"""

    def default(value):
        if isinstance(value, UnityRef):
            return {"fileID": value.file_id, "guid": value.guid, "type": value.type}
        return str(value)

    return json.dumps(body, ensure_ascii=False, default=default)


def _make_light(name: str, values: BlenderLight) -> bpy.types.Object:
    data = bpy.data.lights.new(name or "Light", values.type)
    data.color = values.color
    data.energy = values.energy
    data.use_shadow = values.use_shadow
    data.shadow_soft_size = values.shadow_soft_size
    if values.use_temperature and hasattr(data, "use_temperature"):  # 色温度は Blender 4.5 以降
        data.use_temperature = True
        data.temperature = values.temperature
    if values.type == "SUN":
        data.angle = values.angle
    if values.type in {"POINT", "SPOT"}:
        data.use_soft_falloff = False  # 近くで弱める補正を切り、Unity と同じく点光源として扱う
    if values.type == "SPOT":
        data.spot_size = values.spot_size
        data.spot_blend = values.spot_blend
    if values.type == "AREA":
        data.shape = values.shape
        data.size = values.size
        data.size_y = values.size_y
    if values.use_custom_distance:
        data.use_custom_distance = True
        data.cutoff_distance = values.cutoff_distance
    return bpy.data.objects.new(name or "Light", data)


def _make_camera(name: str, values: BlenderCamera) -> bpy.types.Object:
    data = bpy.data.cameras.new(name or "Camera")
    data.type = values.type
    data.sensor_fit = values.sensor_fit
    data.sensor_width = values.sensor_width
    data.sensor_height = values.sensor_height
    data.lens = max(values.lens, 1.0)
    data.ortho_scale = values.ortho_scale
    data.clip_start = values.clip_start
    data.clip_end = values.clip_end
    data.shift_x = values.shift_x
    data.shift_y = values.shift_y
    return bpy.data.objects.new(name or "Camera", data)


def _attach_to_empty(objects: list[bpy.types.Object], root, scale: float) -> None:
    """モデルの最上位のオブジェクトを配置の Empty の子にする。.meta の globalScale はモデルのルートで掛ける。"""
    if root is None:
        return
    members = set(objects)
    inverse = Matrix.Scale(scale, 4) if math.isfinite(scale) and scale > 0 and scale != 1 else Matrix.Identity(4)
    for obj in objects:
        if obj.parent is None or obj.parent not in members:
            obj.parent = root
            obj.matrix_parent_inverse = inverse


def _duplicate_objects(template: _SceneTemplate, collection) -> list[bpy.types.Object]:
    """テンプレートのオブジェクトを、データ（メッシュ・アーマチュア）を共有したまま複製する。"""
    mapping = {}
    for obj in template.objects:
        copy = obj.copy()
        collection.objects.link(copy)
        mapping[obj] = copy
    for original, copy in mapping.items():
        copy.parent = mapping.get(original.parent)
        copy.matrix_basis = template.basis[original]
        copy.hide_render = template.hide_render[original]
        for modifier in copy.modifiers:
            if getattr(modifier, "object", None) in mapping:
                modifier.object = mapping[modifier.object]
        for constraint in copy.constraints:
            if getattr(constraint, "target", None) in mapping:
                constraint.target = mapping[constraint.target]
    return list(mapping.values())


def _apply_node_transforms(template: _SceneTemplate, objects, overrides, unit_scale: float | None) -> int:
    """モデルの中のノードへの位置・回転・スケールの上書き（古い形式の .meta で名前を引けたもの）を当てる。

    Unity のノード空間の行列 L と、原点に読み込んだ Blender のオブジェクトの行列 N の関係は N = C·L·A
    （C は Unity → Blender の基底、A = diag(-f, f, f)、f は Unity の fileScale = FBX の UnitScaleFactor / 100）。
    Japanese Apartment の FBX（UnitScaleFactor 100 と 1）と Blender 由来の FBX で、Unity 6 が読んだノードの値と
    突き合わせて確かめた（Issue #53）。元のノード値を L = C⁻¹·N·A⁻¹ で求め、上書きの無い成分はその値のまま使う。
    当てるのはモデルの最上位のオブジェクトだけ（入れ子やアーマチュアで変形するものは数えて飛ばす）。
    """
    f = (unit_scale if unit_scale and math.isfinite(unit_scale) and unit_scale > 0 else 1.0) / 100.0
    basis_a = Matrix.Diagonal((-f, f, f, 1.0))
    basis_a_inverse = Matrix.Diagonal((-1.0 / f, 1.0 / f, 1.0 / f, 1.0))
    to_blender, to_unity = Matrix(UNITY_TO_BLENDER), Matrix(BLENDER_TO_UNITY)
    members = set(objects)
    skipped = 0
    for original, obj in zip(template.objects, objects):
        spec = overrides.get(strip_numeric_suffix(obj.name))
        if spec is None:
            continue
        nested = obj.parent in members
        deformed = obj.type == "ARMATURE" or any(m.type == "ARMATURE" for m in getattr(obj, "modifiers", []))
        if nested or deformed:
            skipped += 1
            continue
        location, rotation, scale = (to_unity @ template.basis[original] @ basis_a_inverse).decompose()
        position = [location.x, location.y, location.z]
        quaternion = [rotation.x, rotation.y, rotation.z, rotation.w]  # Unity の並び
        scaling = [scale.x, scale.y, scale.z]
        for values, key in ((position, "position"), (quaternion, "rotation"), (scaling, "scale")):
            for index, value in enumerate(spec.get(key, [])):
                if value is not None and index < len(values):
                    values[index] = value
        local = Matrix(trs(tuple(position), tuple(quaternion), tuple(scaling)))
        obj.matrix_basis = to_blender @ local @ basis_a
    return skipped


def _apply_offsets(objects, root_world, offsets, scale: float, view_layer) -> int:
    """中のノードが上書きで動いたオブジェクトを、そのノードから逆算した位置に置く。アーマチュアで変形するものは数えて飛ばす。

    ``root_world`` は配置のルートの Unity での行列。置いた直後のオブジェクトの行列は「ルートの行列 · globalScale ·
    原点に読み込んだときの行列」なので、そこから原点での行列を求め、逆算したルートの行列を掛け直す。
    """
    def depth(obj) -> int:
        count = 0
        while obj.parent is not None and count < 1000:
            obj, count = obj.parent, count + 1
        return count

    targets = [o for o in objects if strip_numeric_suffix(o.name) in offsets]
    if not targets:
        return 0
    view_layer.update()
    scale_matrix = Matrix.Scale(scale, 4) if math.isfinite(scale) and scale > 0 else Matrix.Identity(4)
    to_origin = (Matrix(unity_to_blender(root_world)) @ scale_matrix).inverted_safe()
    origins = {o: to_origin @ o.matrix_world for o in targets}  # 動かす前にまとめて求める
    skipped = 0
    for obj in sorted(targets, key=depth):
        deformed = obj.parent_type in {"BONE", "ARMATURE"} or any(m.type == "ARMATURE" for m in obj.modifiers)
        if deformed or obj.type == "ARMATURE":
            skipped += 1
            continue
        view_layer.update()
        obj.matrix_world = Matrix(unity_to_blender(offsets[strip_numeric_suffix(obj.name)])) @ scale_matrix @ origins[obj]
    return skipped


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def run_import(
    context,
    filepath: str,
    opts: ImportOptions,
    progress=None,
    prepared: PreparedPackage | None = None,
) -> ImportReport:
    global LAST_REPORT
    package_path = Path(filepath)
    report = ImportReport(package=package_path.name)

    def step(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    step(0.0, "Scanning package")
    if prepared is None:
        prepared = prepare_package(
            filepath, build_shader_table(opts.shader_table_path), import_blend=opts.import_blend
        )
    pkg = prepared.pkg
    for w in prepared.warnings:
        report.warn(w)
    for m in prepared.models:
        if not m.supported:
            report.warn(f"skipped {m.entry.pathname}: {m.skip_reason}")

    groups = [] if opts.unit == UNIT_SCENES else _plan_groups(prepared, opts)
    scenes = _select_scenes(prepared, opts) if opts.unit == UNIT_SCENES else []
    supported_models = {m.guid: m for m in prepared.supported_models}
    selected: dict[str, ModelSummary] = {summary.guid: summary for group in groups for summary, _ in group.models}
    for scene_summary in scenes:
        for placement in scene_summary.placements:
            if placement.model_guid in supported_models:
                selected.setdefault(placement.model_guid, supported_models[placement.model_guid])
    models = list(selected.values())
    if not models:
        reasons = {m.skip_reason for m in prepared.models if not m.supported}
        what = "models"
        if opts.unit == UNIT_PREFABS:
            reasons |= {p.skip_reason for p in prepared.prefabs if not p.supported}
            what = "prefabs"
        elif opts.unit == UNIT_SCENES:
            reasons |= {s.skip_reason for s in prepared.scenes if not s.supported}
            what = "scenes"
        reasons_text = "; ".join(sorted(reasons))
        raise PackageError(f"no importable {what} selected" + (f" ({reasons_text})" if reasons_text else ""))

    unity_mats, normalized = prepared.unity_mats, prepared.normalized

    # --- 必要なテクスチャを決めて展開 ---
    # 全モデルを VRM add-on に任せる場合、.mat 由来のマテリアルは組まないのでテクスチャも読まない
    delegate_all = all(_delegate_to_vrm_addon(m.entry, opts) for m in models)
    needed_tex = set() if delegate_all else set(prepared.referenced_textures)
    if opts.import_unreferenced:
        needed_tex.update(e.guid for e in pkg.textures())
    for g in sorted(prepared.missing_textures):
        report.warn(f"texture {g} is referenced but not included in the package")
    needed_tex -= prepared.missing_textures

    extract_root = resolve_extract_root(opts, package_path)
    report.extract_root = str(extract_root)
    step(0.2, "Extracting files")
    sidecars = [g for m in models for g in _sidecar_guids(pkg, m.entry)]
    paths = pkg.extract(
        [m.guid for m in models] + sidecars + sorted(needed_tex),
        extract_root,
        overwrite=opts.overwrite_extracted,
        progress=lambda f, n: step(0.2 + 0.3 * f, f"Extracting {n}"),
        max_total_size=opts.max_extract_size,
    )

    # --- 画像の読み込み ---
    step(0.5, "Loading textures")
    images: dict[str, bpy.types.Image | None] = {}
    tex_infos: dict[str, TextureImporterInfo] = {}
    for guid in sorted(needed_tex):
        entry = pkg.get(guid)
        info = _texture_info(entry, report.warn)
        tex_infos[guid] = info
        path = paths.get(guid)
        image = load_image(path, info, pack=opts.pack_images) if path else None
        images[guid] = image
        if image is None:
            report.warn(f"could not load texture {entry.pathname} (unsupported format?)")
        else:
            report.images.append(image.name)

    mat_builder.tag_images(images, tex_infos)

    # --- パッケージ用コレクション ---
    scene = context.scene
    collection = bpy.data.collections.new(package_path.stem)
    scene.collection.children.link(collection)
    view_layer = context.view_layer
    prev_active = view_layer.active_layer_collection

    build_opts = mat_builder.MaterialBuildOptions(
        mode=opts.material_mode,
        force_opaque=opts.force_opaque,
        backface_culling=opts.backface_culling,
        use_normal_maps=opts.use_normal_maps,
        use_emission=opts.use_emission,
        store_props=opts.store_props,
    )

    built_by_guid: dict[str, bpy.types.Material] = {}  # 同じ .mat は 1 つの Blender マテリアルを共有
    imported_models: set[str] = set()  # この回で読み込み済みのモデル（prefab ごとに同じモデルを読み直すことがある）

    def import_model(summary: ModelSummary, prefab_table: dict[str, RendererMaterials], target) -> list[bpy.types.Object]:
        """モデルを 1 回読み込んでマテリアルを組み、作られたオブジェクトを返す。"""
        model = summary.entry
        model_info = _model_info(model, report.warn)
        reimport = model.guid in imported_models
        imported_models.add(model.guid)
        before = _snapshot()
        # 同名マテリアルの再利用を使うときだけ一覧を作る（シーンでは何百回も読み込むので、毎回作ると重い）
        existing_materials = {m.name: m for m in bpy.data.materials} if opts.reuse_existing else {}
        delegated = _import_model(context, paths[model.guid], model, opts, target, report)
        new = _new_since(before)
        _adopt_into_collection(new, scene, target)
        if delegated:
            # add-on が読み込んだ（pack 済みの）画像もレポートに載せる
            report.images.extend(i.name for i in new["images"])
        if delegated and not new["objects"]:
            # VRM 0.x の制限付きライセンスでは add-on が確認ダイアログを出し、その場では読み込まない
            report.warn(
                f"VRM add-on did not create any objects for {model.name} "
                "(a license confirmation dialog may be waiting; the model is imported after confirming, "
                "outside of this importer)"
            )
        report.models.append(model.pathname)
        report.objects.extend(o.name for o in new["objects"])

        new_materials: list[bpy.types.Material] = new["materials"]
        fbx_names = [m.name for m in new_materials]
        mesh_objects = [o for o in new["objects"] if o.type == "MESH"]
        object_slots = {
            o.name: [slot.material.name if slot.material else "" for slot in o.material_slots]
            for o in mesh_objects
        }
        # prefab の m_Materials は Unity のサブメッシュ順で、Blender のスロット順とは限らない
        submesh_order = {o.name: _submesh_order(o) for o in mesh_objects}
        resolution = resolve_materials(
            fbx_names, model_info, unity_mats, model.pathname, prefab_table, object_slots, submesh_order
        )
        assignments = slot_assignments(object_slots, prefab_table, unity_mats, submesh_order)

        # 同梱 .blend の KEEP と VRM add-on 委譲では、インポーターが作ったマテリアルをそのまま使う
        keep_materials = delegated or (model.ext == ".blend" and opts.blend_materials == "KEEP")
        remaining: list[bpy.types.Material] = []  # 削除せずに残した、インポーターが作ったマテリアル
        for bmat in new_materials:
            res = resolution[bmat.name]
            mrep = MaterialReport(blender_name=bmat.name, fbx_name=bmat.name, guid=res.guid, method=res.method)
            report.materials.append(mrep)
            if keep_materials:
                remaining.append(bmat)
                mrep.method = "delegated" if delegated else "kept"
                if res.guid:
                    norm = normalized[res.guid]
                    mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
                    if opts.store_props:
                        mat_builder._store_props(bmat, norm)
                continue
            if res.warning:
                mrep.warnings.append(res.warning)
                report.warn(f"material {bmat.name!r}: {res.warning}")

            if opts.reuse_existing:
                base = strip_numeric_suffix(bmat.name)
                existing = existing_materials.get(base)
                if existing is not None and existing is not bmat:
                    _replace_material(new["objects"], bmat, existing)
                    bpy.data.materials.remove(bmat)
                    mrep.method = "reused"
                    mrep.blender_name = existing.name
                    continue

            remaining.append(bmat)
            if res.guid is None:
                mrep.warnings.append("no matching .mat in package; material left as imported")
                report.warn(f"material {bmat.name!r}: no matching .mat in package")
                continue

            shared = built_by_guid.get(res.guid) if reimport else None
            if shared is not None and shared is not bmat:
                # 同じモデルを別の prefab 用に読み直したときは、組み立て済みの同じ .mat のマテリアルを使う
                remaining.pop()
                _replace_material(new["objects"], bmat, shared)
                bpy.data.materials.remove(bmat)
                norm = normalized[res.guid]
                mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
                mrep.method = "shared"
                mrep.blender_name = shared.name
                continue

            _build_and_report(bmat, res.guid, normalized, images, tex_infos, build_opts, pkg, report, mrep)
            built_by_guid.setdefault(res.guid, bmat)

        if not keep_materials:
            # インポーターが作った画像（glTF の埋め込み画像など）は .mat から組み直した時点で不要になる
            for image in new["images"]:
                if image.users == 0:
                    bpy.data.images.remove(image)

        # prefab がスロットごとに別の .mat を指している場合は、そのスロットだけ別マテリアルに差し替える
        if assignments and not keep_materials:
            split = _split_slots_by_prefab(
                new["objects"], assignments, resolution, unity_mats, normalized, built_by_guid,
                images, tex_infos, build_opts, pkg, report,
            )
            if split:
                report.split_slots += split
                # 差し替えで使われなくなった FBX マテリアルは片付ける
                for bmat in remaining:
                    if bmat.users == 0:
                        for mrep in report.materials:
                            if mrep.blender_name == bmat.name and mrep.method != "replaced":
                                mrep.method = "replaced"
                        # 組み立て済みの表からも外す。残すと、同じモデルを読み直したときに削除済みのマテリアルを使ってしまう
                        for guid in [g for g, m in built_by_guid.items() if m == bmat]:
                            del built_by_guid[guid]
                        bpy.data.materials.remove(bmat)
        if opts.outlines:
            added = outline_builder.apply_outlines(new["objects"], opts.outline_width_scale)
            if added:
                report.outlines += added
        return new["objects"]

    model_by_guid = {m.guid: m for m in prepared.models}

    def import_scene(scene_summary: SceneSummary, progress_start: float, progress_span: float) -> None:
        """シーンのモデルを配置どおりに読み込む。同じモデル・同じ割り当ての配置は、メッシュを共有した複製にする。"""
        pathname = scene_summary.pathname
        target = bpy.data.collections.new(scene_summary.name)
        collection.children.link(target)
        target["unity_scene"] = pathname
        target["unity_scene_guid"] = scene_summary.guid
        report.scenes.append(pathname)
        layer_coll = _find_layer_collection(view_layer.layer_collection, target)
        if layer_coll is not None:
            view_layer.active_layer_collection = layer_coll

        hierarchy = prepared.scene_hierarchies[scene_summary.guid]
        active = effective_active(hierarchy)
        empties: dict[int, bpy.types.Object] = {}
        templates: dict[tuple, _SceneTemplate] = {}
        skipped_offsets = 0
        scales: dict[str, float] = {}  # モデルの GUID → .meta の globalScale（配置ごとに .meta を読み直さない）
        unit_scales: dict[str, float | None] = {}  # モデルの GUID → FBX の UnitScaleFactor
        skipped_nodes = 0  # 名前を引けたが当てられなかった、モデルの中のノードへの位置の上書き
        count = len(scene_summary.placements)
        for index, placement in enumerate(scene_summary.placements):
            if index % 25 == 0:
                step(progress_start + progress_span * index / max(count, 1), f"Placing {index}/{count} in {scene_summary.name}")
            summary = supported_models.get(placement.model_guid)
            if summary is None:
                skipped = model_by_guid.get(placement.model_guid)
                if skipped is not None:
                    report.warn(f"scene {pathname}: skipped {skipped.entry.pathname}: {skipped.skip_reason}")
                continue
            root = _scene_empty(hierarchy, placement.root, target, empties, active)
            table = {
                name: RendererMaterials(name, list(r.materials), r.renderer_class, placement.model_guid)
                for name, r in placement.renderers.items()
            }
            key = placement.signature()
            template = templates.get(key)
            if template is None:
                objects = import_model(summary, table, target)
                template = templates[key] = _SceneTemplate.capture(objects)
            else:
                objects = _duplicate_objects(template, target)
                report.objects.extend(o.name for o in objects)
            if summary.guid not in scales:
                scales[summary.guid] = _model_info(summary.entry, report.warn).global_scale
            scale = scales[summary.guid]
            _attach_to_empty(objects, root, scale)
            if placement.node_transforms:
                if summary.guid not in unit_scales:
                    unit_scales[summary.guid] = read_unit_scale(paths[summary.guid]) if summary.entry.ext == ".fbx" else None
                skipped_nodes += _apply_node_transforms(template, objects, placement.node_transforms, unit_scales[summary.guid])
            if placement.offsets:
                skipped_offsets += _apply_offsets(objects, placement.world, placement.offsets, scale, view_layer)
            hidden = {name for name, r in placement.renderers.items() if not r.visible}
            for obj in objects:
                if not placement.active or strip_numeric_suffix(obj.name) in hidden:
                    obj.hide_set(True)
                    obj.hide_render = True

        # --- ライト・カメラ ---
        baked_lights = 0
        light_notes: set[str] = set()
        wanted = ([CLASS_LIGHT] if opts.scene_lights else []) + ([CLASS_CAMERA] if opts.scene_cameras else [])
        for component in scene_components(hierarchy, wanted) if wanted else []:
            node = hierarchy.nodes[component.node]
            if component.node in empties:
                # 同じ GameObject にモデルの配置の Empty があれば、その子にする
                parent, local = empties[component.node], Matrix.Identity(4)
            else:
                parent = _scene_empty(hierarchy, node.parent, target, empties, active) if node.parent is not None else None
                local = Matrix(unity_to_blender(node.local))
            if component.class_id == CLASS_LIGHT:
                values = convert_light(component.body, prepared.pipeline)
                obj = _make_light(component.name, values)
                obj["unity_light"] = _json_text(component.body)
                obj["unity_render_pipeline"] = prepared.pipeline
                baked_lights += values.baked_only
                light_notes.update(values.notes)
                report.lights += 1
            else:
                obj = _make_camera(component.name, convert_camera(component.body))
                obj["unity_camera"] = _json_text(component.body)
                report.cameras += 1
                if scene.camera is None and component.active:
                    scene.camera = obj
            target.objects.link(obj)
            obj.parent = parent
            location, rotation, _ = (local @ Matrix(LIGHT_CAMERA_BASIS)).decompose()
            obj.matrix_basis = Matrix.LocRotScale(location, rotation, None)  # ライト・カメラにはスケールを掛けない
            if not component.active:
                obj.hide_set(True)
                obj.hide_render = True
        if baked_lights:
            report.warn(
                f"scene {pathname}: {baked_lights} light(s) only affect lightmaps in Unity (baked or area lights); "
                "they were imported as real-time lights"
            )
        for note in sorted(light_notes):
            report.warn(f"scene {pathname}: {note}")

        contents = scene_summary.contents
        if contents is None:
            return
        if contents.unresolved_overrides:
            report.warn(
                f"scene {pathname}: {contents.unresolved_overrides} override(s) on objects inside a model "
                "(position, material or visibility) are not read yet"
            )
        if contents.missing_sources:
            report.warn(f"scene {pathname}: {contents.missing_sources} prefab instance(s) refer to assets that are not in the package")
        if contents.other_renderers:
            report.warn(
                f"scene {pathname}: {contents.other_renderers} renderer(s) use meshes outside the package "
                "(such as Unity's built-in primitives) and were skipped"
            )
        if skipped_nodes:
            report.warn(
                f"scene {pathname}: {skipped_nodes} position override(s) on nested or armature-deformed parts inside a model "
                "were not applied"
            )
        if skipped_offsets:
            report.warn(
                f"scene {pathname}: {skipped_offsets} moved part(s) of armature-deformed objects were left at the model's position"
            )

    prefab_collections: list[bpy.types.Collection] = []
    total = sum(len(group.models) for group in groups) + len(scenes)
    done = 0
    try:
        for group in groups:
            target = collection
            if group.prefab is not None:
                prefab = group.prefab
                target = bpy.data.collections.new(prefab.name)
                collection.children.link(target)
                target["unity_prefab"] = prefab.pathname
                target["unity_prefab_guid"] = prefab.guid
                prefab_collections.append(target)
                report.prefabs.append(prefab.pathname)
                for guid in prefab.model_guids:
                    skipped = model_by_guid.get(guid)
                    if skipped is not None and not skipped.supported:
                        report.warn(f"prefab {prefab.pathname}: skipped {skipped.entry.pathname}: {skipped.skip_reason}")
            layer_coll = _find_layer_collection(view_layer.layer_collection, target)
            if layer_coll is not None:
                view_layer.active_layer_collection = layer_coll
            for summary, prefab_table in group.models:
                step(0.55 + 0.4 * done / max(total, 1), f"Importing {summary.entry.name}")
                import_model(summary, prefab_table, target)
                done += 1
        for scene_summary in scenes:
            import_scene(scene_summary, 0.55 + 0.4 * done / max(total, 1), 0.4 / max(total, 1))
            done += 1
        if opts.arrange == "SIDE_BY_SIDE" and len(prefab_collections) > 1:
            _arrange_collections(context, prefab_collections)
    finally:
        if prev_active is not None:
            view_layer.active_layer_collection = prev_active

    collection["unity_package"] = package_path.name
    step(1.0, "Done")
    LAST_REPORT = report
    return report


def _build_and_report(bmat, guid, normalized, images, tex_infos, build_opts, pkg, report, mrep) -> None:
    norm = normalized[guid]
    mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
    try:
        mode, warnings = mat_builder.build_material(bmat, norm, images, tex_infos, build_opts)
    except Exception as exc:  # noqa: BLE001 - 1 マテリアルの失敗で全体を止めない
        mrep.warnings.append(f"node build failed: {exc!r}")
        report.warn(f"material {bmat.name!r}: node build failed: {exc!r}")
        return
    mrep.mode = mode
    mrep.warnings.extend(warnings)
    for w in warnings:
        if w.startswith("shader table entry"):
            report.warn(w)  # シェーダー単位の注意なので 1 回だけ
        else:
            report.warn(f"material {bmat.name!r}: {w}")
    mrep.textures = [
        f"{role}={pkg.get(t.guid).pathname if pkg.get(t.guid) else t.guid}"
        for role, t in (
            ("base", norm.base_color_tex),
            ("normal", norm.normal_tex),
            ("emission", norm.emission_tex),
            ("metallic", norm.metallic_tex),
        )
        if t is not None
    ]


def _split_slots_by_prefab(objects, assignments, resolution, unity_mats, normalized, built_by_guid,
                           images, tex_infos, build_opts, pkg, report) -> int:
    """(オブジェクト, スロット) ごとに prefab の .mat を割り当てる。差し替えたスロット数を返す。"""
    by_name = {o.name: o for o in objects if o.type == "MESH"}
    count = 0
    for (obj_name, index), guid in assignments.items():
        obj = by_name.get(obj_name)
        if obj is None or index >= len(obj.material_slots):
            continue
        slot = obj.material_slots[index]
        current = slot.material
        current_guid = resolution[current.name].guid if current is not None and current.name in resolution else None
        mat = built_by_guid.get(guid)
        if current_guid == guid or (current is not None and current is mat):
            continue
        if mat is None:
            umat = unity_mats[guid]
            mat = bpy.data.materials.new(umat.name or guid[:8])
            mrep = MaterialReport(
                blender_name=mat.name,
                fbx_name=current.name if current is not None else "",
                guid=guid,
                method="prefab-split",
            )
            report.materials.append(mrep)
            _build_and_report(mat, guid, normalized, images, tex_infos, build_opts, pkg, report, mrep)
            built_by_guid[guid] = mat
        slot.material = mat
        count += 1
    return count


def _submesh_order(obj) -> list[int]:
    """Unity のサブメッシュ順（ポリゴンで最初に使われた順）に並べたスロット番号。"""
    polygons = obj.data.polygons
    indices = [0] * len(polygons)
    polygons.foreach_get("material_index", indices)
    return submesh_slot_order(indices, len(obj.material_slots))


def _replace_material(objects, old: bpy.types.Material, new: bpy.types.Material) -> None:
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material == old:
                slot.material = new
