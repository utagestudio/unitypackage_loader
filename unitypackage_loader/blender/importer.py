"""インポート全体のオーケストレーション。

``prepare_package`` でパッケージを走査・解析し（モデル選択ダイアログにも使う）、
``run_import`` で実際の展開・FBX 読み込み・マテリアル構築を行う。
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import bpy
from mathutils import Matrix

from ..core.hierarchy import (
    CLASS_CAMERA,
    CLASS_LIGHT,
    Expander,
    Hierarchy,
    RawAsset,
    effective_active,
    parse_asset,
    summarize,
)
from ..core.hierarchy import components as scene_components
from ..core.lights import LIGHT_CAMERA_BASIS, convert_camera, convert_light, detect_pipeline
from ..core.fbx_units import read_unit_scale
from ..core.transform import unity_to_blender
from ..core.mapping import fully_replaced_materials, resolve_materials, slot_assignments, submesh_slot_order
from ..core.material import NormalizedMaterial, UnityMaterial, parse_material
from ..core.meta import ModelImporterInfo, TextureImporterInfo, strip_numeric_suffix
from ..core.package import AssetEntry, PackageError, UnityPackage
from ..core.prefab import RendererMaterials, find_renderer, merge_prefab_tables, resolve_sole_renderer, tables_from_hierarchy
from ..core.profiles import ShaderTable, normalize_material
from ..core.profiles.base import default_table
from ..core.report import ImportReport, MaterialReport
from ..core.scene_import import lod_warning, parts_to_hide, scene_warnings
from ..core.units import (
    UNIT_PREFABS,
    UNIT_SCENES,
    PrefabSummary,
    SceneSummary,
    choice_count,
    summarize_prefabs,
    summarize_scene,
)
from ..ui.preferences import get_prefs
from . import materials as mat_builder
from . import outline as outline_builder
from .scene_objects import (
    SceneTemplate,
    defer_hide,
    apply_collapsed_root,
    apply_node_transforms,
    apply_offsets,
    arrange_collections,
    attach_to_empty,
    duplicate_objects,
    find_layer_collection,
    json_text,
    make_camera,
    make_light,
    move_collection_contents,
    scene_empty,
)
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
    unit: str = "MODELS"  # 読み込む単位: SCENES / PREFABS / MODELS
    model_guids: list[str] | None = None  # 明示的に選ばれたモデル（ダイアログ経由）
    prefab_paths: list[str] | None = None  # 明示的に選ばれた prefab の pathname（ダイアログ経由）
    scene_paths: list[str] | None = None  # 明示的に選ばれたシーンの pathname（ダイアログ経由）
    scene_lights: bool = True  # シーンのライトを読み込む
    scene_cameras: bool = True  # シーンのカメラを読み込む
    hide_lods: bool = True  # LODGroup の遠景用の段（LOD1 以降）を非表示にする（Scenes / Prefabs 単位）
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
COLLADA_UNAVAILABLE_REASON = "Collada (.dae) import is not available in this Blender (it was removed in Blender 5.0)"


@dataclass
class ModelSummary:
    entry: AssetEntry
    material_count: int  # externalObjects に登録されたマテリアル数
    resolved_count: int  # そのうちパッケージ内の .mat に対応付けできた数
    supported: bool
    skip_reason: str = ""  # supported が False の理由
    info: ModelImporterInfo = field(default_factory=ModelImporterInfo)  # .meta の設定（読み込みのたびに解析し直さない。#75）

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
    scene_entries: list[AssetEntry] = field(default_factory=list)  # パッケージ内のシーン（pathname 順。展開は後回し）
    scene_hierarchies: dict[str, Hierarchy] = field(default_factory=dict)  # シーンの GUID → 展開した階層（ensure_scenes で作る）
    pipeline: str = "BUILTIN"  # マテリアルから判定したレンダーパイプライン（ライトの強さの換算に使う）
    _scenes: list[SceneSummary] | None = field(default=None, repr=False)
    _expander: Expander | None = field(default=None, repr=False)  # prefab の展開結果を持つ（シーンの展開で使い回す）

    def ensure_scenes(self) -> list[SceneSummary]:
        """シーンを展開して、読み込む単位 Scenes の候補（pathname 順）を返す。展開は初めて呼ばれたときの 1 回だけ。

        大きなシーンの解析は重いので、Models / Prefabs だけを読む場合には展開しない（#75）。展開中の警告は ``warnings`` に加える。
        """
        if self._scenes is None:
            unsupported = {m.guid: m.skip_reason for m in self.models if not m.supported}
            if self._expander is None:
                model_names, recycle = _model_tables(self.models)
                self._expander = Expander(_prefab_reader(self.pkg, self.warnings.append), model_names, recycle)
            self._scenes, self.scene_hierarchies = _prepare_scenes(
                self.pkg, self._expander, self.models, self.warnings.append, unsupported
            )
        return self._scenes

    @property
    def scenes(self) -> list[SceneSummary]:
        return self.ensure_scenes()

    @property
    def scenes_expanded(self) -> bool:
        return self._scenes is not None

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
    def has_choices(self) -> bool:
        """読み込める候補が 2 つ以上あるか（ダイアログを出すか）。シーンを数えなくても決まるなら、シーンは展開しない。"""
        others = choice_count(self.prefabs, len(self.supported_models))
        if others > 1 or not self.scene_entries:
            return others > 1
        return choice_count(self.prefabs, len(self.supported_models), self.scenes) > 1

    def table_for(self, model_guid: str) -> dict[str, RendererMaterials]:
        """Models 単位でモデルに当てはめる表。そのモデルを使う prefab をパス順に先勝ちで統合したもの。"""
        candidates = sorted(p for p, tables in self.prefab_tables.items() if model_guid in tables)
        return merge_prefab_tables([self.prefab_tables[p][model_guid] for p in candidates])


def build_shader_table(extra_path: str = "") -> ShaderTable:
    """同梱の表に、Preferences で指定した追加の表を重ねる。

    追加の表が見つからないか読めなければ、同梱の表だけで続けて理由を ``warnings`` に残す（``prepare_package`` が
    レポートの警告に写す）。以前は壊れた JSON ですべてのインポートが止まり、見つからないときは黙って無視していた（#78）。
    """
    if not extra_path:
        return default_table()
    path = Path(bpy.path.abspath(extra_path))
    merged = ShaderTable()
    if not path.is_file():
        merged.warnings.append(f"shader table {path} was not found; only the built-in table is used")
        return merged
    try:
        extra = ShaderTable(path)
    except Exception as exc:  # noqa: BLE001 - 補助データなので、同梱の表だけで続ける
        merged.warnings.append(f"could not read shader table {path}: {exc}; only the built-in table is used")
        _log_exception(f"could not read shader table {path}")
        return merged
    merged.by_guid.update(extra.by_guid)
    merged.by_builtin_id.update(extra.by_builtin_id)
    return merged


def _log_exception(message: str) -> None:
    """捕まえて続けた例外の traceback を、verbose ログが有効ならコンソールに出す（警告には要約だけを出す）。"""
    prefs = get_prefs(bpy.context)
    if prefs is None or prefs.verbose_log:
        print(f"[Unitypackage Importer] {message}")
        traceback.print_exc()


def _model_info(entry: AssetEntry, warn: Callable[[str], None]) -> ModelImporterInfo:
    """モデルの .meta を読む。壊れていても（構文エラー・ネスト過多）そのモデルだけ既定値で続ける。"""
    if not entry.meta_text:
        return ModelImporterInfo()
    try:
        return ModelImporterInfo.from_meta(entry.meta_text)
    except Exception as exc:  # noqa: BLE001 - 補助データなので、そのモデルだけ既定値で続ける（#69）
        warn(f"could not parse .meta of {entry.pathname}: {exc}")
        _log_exception(f"could not parse .meta of {entry.pathname}")
        return ModelImporterInfo()


def _texture_info(entry: AssetEntry, warn: Callable[[str], None]) -> TextureImporterInfo:
    if not entry.meta_text:
        return TextureImporterInfo()
    try:
        return TextureImporterInfo.from_meta(entry.meta_text)
    except Exception as exc:  # noqa: BLE001 - 補助データなので、その画像だけ既定値で続ける（#69）
        warn(f"could not parse .meta of {entry.pathname}: {exc}")
        _log_exception(f"could not parse .meta of {entry.pathname}")
        return TextureImporterInfo()


def prepare_package(
    filepath: str, shader_table: ShaderTable | None = None, *, import_blend: bool = False
) -> PreparedPackage:
    """パッケージを走査して解析する。``import_blend`` が False なら同梱 .blend は「読み込まない」扱いにする。"""
    path = Path(filepath)
    pkg = UnityPackage(path)
    pkg.scan()
    table = shader_table or default_table()
    warnings: list[str] = list(pkg.warnings) + list(table.warnings)

    unity_mats: dict[str, UnityMaterial] = {}
    normalized: dict[str, NormalizedMaterial] = {}
    for entry in pkg.materials():
        try:
            umat = parse_material(pkg.read_asset(entry.guid), entry.guid, entry.pathname)
            norm = normalize_material(umat, table)
        except Exception as exc:  # noqa: BLE001 - その .mat だけを外して続ける（#69）
            warnings.append(f"could not parse {entry.pathname}: {exc}")
            _log_exception(f"could not parse {entry.pathname}")
            continue
        unity_mats[entry.guid] = umat
        normalized[entry.guid] = norm

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
        elif entry.ext == ".dae" and not operator_available("wm", "collada_import"):
            skip_reason = COLLADA_UNAVAILABLE_REASON
        models.append(ModelSummary(entry, len(names), resolved, not skip_reason, skip_reason, info))
    if not models:
        raise PackageError("the package contains no model files (.fbx/.obj/.gltf/.glb/.vrm/.dae/.blend)")

    referenced: set[str] = set()
    for norm in normalized.values():
        referenced.update(t.guid for t in norm.texture_refs())
        referenced.update(norm.extra_texture_guids())
    missing = {g for g in referenced if pkg.get(g) is None}

    # prefab は Scenes 単位と同じ展開器で展開する（Prefab Variant・ネスト・上書き・削除・古い形式。#71）。
    # 展開結果はキャッシュされ、あとでシーンを展開するときにも使う
    model_guids = [m.guid for m in models]
    model_names, recycle = _model_tables(models)
    expander = Expander(_prefab_reader(pkg, warnings.append), model_names, recycle)
    prefab_tables: dict[str, dict[str, dict[str, RendererMaterials]]] = {}
    for entry in pkg.prefabs():
        try:
            hierarchy = expander.expand_asset(entry.guid)
            if hierarchy is None:  # 読めなかった（警告は読むときに出している）
                continue
            tables = tables_from_hierarchy(hierarchy, model_names, recycle)
        except Exception as exc:  # noqa: BLE001 - その prefab の割り当てだけを外して続ける（#69）
            warnings.append(f"could not resolve prefab {entry.pathname}: {exc}")
            _log_exception(f"could not resolve prefab {entry.pathname}")
            continue
        if hierarchy.unresolved_material_overrides:  # 位置などの上書きは Models / Prefabs 単位では使わないので数えない
            warnings.append(
                f"prefab {entry.pathname}: {hierarchy.unresolved_material_overrides} material override(s) on objects inside a model "
                "are not read yet; the model's own assignments are used for them"
            )
        if tables:
            prefab_tables[entry.pathname] = tables
    unsupported = {m.guid: m.skip_reason for m in models if not m.supported}
    prefabs = summarize_prefabs(((e.guid, e.pathname) for e in pkg.prefabs()), prefab_tables, model_guids, unsupported)
    return PreparedPackage(
        path, pkg, unity_mats, normalized, models, referenced, missing, prefab_tables, warnings,
        prefabs=prefabs, scene_entries=pkg.scenes(), _expander=expander,
        pipeline=detect_pipeline(n.family for n in normalized.values()),
    )


def _model_tables(models: list[ModelSummary]) -> tuple[dict[str, str], dict[str, dict[int, str]]]:
    """展開器に渡す、モデルの GUID → ルートの名前と、古い形式の .meta の fileIDToRecycleName（中への上書きを名前に結び付ける。#53）。"""
    return {m.guid: Path(m.entry.pathname).stem for m in models}, {m.guid: m.info.recycle_names for m in models}


def _prefab_reader(pkg: UnityPackage, warn: Callable[[str], None]) -> Callable[[str], RawAsset | None]:
    """展開器に渡す読み取り関数。prefab 以外と、読めない prefab は None（元の無いインスタンスとして扱う。#69）。"""
    prefab_guids = {e.guid for e in pkg.prefabs()}

    def read(guid: str) -> RawAsset | None:
        if guid not in prefab_guids:
            return None
        try:
            return parse_asset(pkg.read_asset(guid))
        except Exception as exc:  # noqa: BLE001 - prefab は補助情報なので、その prefab だけを外して続ける
            warn(f"could not parse prefab {pkg.get(guid).pathname}: {exc}")
            _log_exception(f"could not parse prefab {pkg.get(guid).pathname}")
            return None

    return read


def _prepare_scenes(
    pkg: UnityPackage, expander: Expander, models: list[ModelSummary], warn: Callable[[str], None], unsupported: dict[str, str]
) -> tuple[list[SceneSummary], dict[str, Hierarchy]]:
    """シーンを展開して候補にする。prefab の展開結果は prefab の表づくりと共有する（``expander`` のキャッシュ）。"""
    entries = pkg.scenes()
    if not entries:
        return [], {}
    model_names, recycle = _model_tables(models)
    scenes: list[SceneSummary] = []
    hierarchies: dict[str, Hierarchy] = {}
    for entry in entries:
        contents = None
        try:
            hierarchy = expander.expand_raw(parse_asset(pkg.read_asset(entry.guid)))
            contents = summarize(hierarchy, model_names, recycle)
            hierarchies[entry.guid] = hierarchy
        except Exception as exc:  # noqa: BLE001 - 読めないシーンは候補を灰色にし、モデルや prefab の読み込みは続ける（#69）
            warn(f"could not read scene {entry.pathname}: {exc}")
            _log_exception(f"could not read scene {entry.pathname}")
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

_TRACKED = ("objects", "materials", "images", "meshes", "armatures", "actions", "collections", "lights", "cameras")


def _snapshot() -> dict[str, set[str]]:
    return {name: {d.name for d in getattr(bpy.data, name)} for name in _TRACKED}


def _new_since(before: dict[str, set[str]]) -> dict[str, list]:
    result = {}
    for name in _TRACKED:
        coll = getattr(bpy.data, name)
        result[name] = [d for d in coll if d.name not in before[name]]
    return result


def _remove_created(before: dict[str, set[str]]) -> None:
    """``before`` の後に作られたデータブロックを消す（読み込みに失敗したときの片付け）。"""
    new = _new_since(before)
    ids = [d for name in _TRACKED for d in new[name]]
    if ids:
        bpy.data.batch_remove(ids)


def _is_removed(datablock) -> bool:
    try:
        datablock.name
    except ReferenceError:
        return True
    return False


def _record_failure(report: ImportReport, message: str) -> None:
    """モデルやシーン 1 つの読み込みの失敗をレポートに記録する（残りの読み込みは続ける。except の中で呼ぶ）。"""
    message = message.strip()  # Blender の例外の文は末尾に改行が付く
    if message not in report.errors:
        report.errors.append(message)
    _log_exception(message)


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


def operator_available(module: str, name: str) -> bool:
    """``bpy.ops.<module>.<name>`` が登録されているか。

    ``hasattr(bpy.ops.<module>, name)`` はどんな名前でも True になり、C で定義されたオペレーター
    （wm.collada_import など）は ``bpy.types`` にも現れないので、``dir`` で登録済みの名前を見る。
    """
    return name in dir(getattr(bpy.ops, module))


def vrm_addon_available() -> bool:
    """VRM add-on（import_scene.vrm）が登録されているか。"""
    return operator_available("import_scene", "vrm")


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


def _import_fbx(path: Path, opts: ImportOptions) -> None:
    # wm.fbx_import は Blender 4.5 で追加された（最小版は 4.5）。bpy.ops の hasattr は常に True なので判定には使えない
    use_new = opts.fbx_importer in ("AUTO", "NEW")
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


def _import_blend(path: Path, collection: bpy.types.Collection) -> None:
    """同梱 .blend の全オブジェクトを append してコレクションに入れる。"""
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        data_to.objects = list(data_from.objects)
    for obj in data_to.objects:
        if obj is not None:
            collection.objects.link(obj)


def _delegate_to_vrm_addon(model: AssetEntry, opts: ImportOptions) -> bool:
    return model.ext == ".vrm" and opts.use_vrm_addon and vrm_addon_available()


def _import_model(path: Path, model: AssetEntry, opts: ImportOptions, collection, report: ImportReport) -> bool:
    """モデルを読み込む。マテリアルを外部インポーターに任せた（こちらで組み直さない）場合は True。"""
    ext = model.ext
    if ext == ".fbx":
        _import_fbx(path, opts)
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
        _import_blend(path, collection)
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


def _select_scenes(prepared: PreparedPackage, opts: ImportOptions) -> list[SceneSummary]:
    supported = prepared.supported_scenes
    if opts.scene_paths is not None:
        wanted = set(opts.scene_paths)
        return [s for s in supported if s.pathname in wanted]
    if opts.models == "FIRST":
        return supported[:1]
    return supported


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


class _NothingImported(PackageError):
    """選んだモデル・シーンがどれも読み込めなかった（オペレーターが PackageError と同じく本文をそのまま表示する）。"""


def run_import(
    context,
    filepath: str,
    opts: ImportOptions,
    progress=None,
    prepared: PreparedPackage | None = None,
) -> ImportReport:
    """パッケージを読み込み、レポートを返す（``LAST_REPORT`` にも入れる）。

    モデルやシーン 1 つの読み込みの失敗はレポートの ``errors`` に記録して続け、読めた分は残す。
    続けられない失敗と、選んだものが 1 つも読み込めなかったときは、この回で作ったものを片付けてから例外を送る。
    どちらの場合も ``LAST_REPORT`` を更新する（#70）。
    """
    global LAST_REPORT
    report = ImportReport(package=Path(filepath).name)
    before = _snapshot()
    try:
        _run_import(context, filepath, opts, progress, prepared, report)
        if report.errors and not (report.objects or report.lights or report.cameras):
            raise _NothingImported(f"nothing could be imported: {report.errors[0]}")
    except Exception as exc:
        if not isinstance(exc, _NothingImported):
            report.errors.append(f"import failed: {str(exc).strip()}")
        _remove_created(before)
        for items in (report.objects, report.materials, report.images):
            items.clear()  # 片付けたので、Blender には残っていない
        LAST_REPORT = report
        raise
    LAST_REPORT = report
    return report


class _ImportSession:
    """1 回のインポートの状態と手順。

    以前は ``_run_import`` の中の入れ子の関数（``build_model`` / ``import_model`` / ``import_scene``）が、外側の変数を
    20 個取り込んでいた。読み込みの間に共有する状態（展開したファイル、読み込んだ画像、組み立てたマテリアル、
    作業用のコレクションなど）をフィールドにし、手順をメソッドにする（#78）。フィールドは ``run`` の中で順に作る。
    """

    def __init__(self, context, filepath, opts, progress, prepared, report):
        self.context = context
        self.filepath = filepath
        self.opts = opts
        self.progress = progress
        self.prepared = prepared
        self.report = report

    def run(self) -> None:
        package_path = Path(self.filepath)

        self.step(0.0, "Scanning package")
        if self.prepared is None:
            self.prepared = prepare_package(
                self.filepath, build_shader_table(self.opts.shader_table_path), import_blend=self.opts.import_blend
            )
        self.pkg = self.prepared.pkg
        if self.opts.unit == UNIT_SCENES:
            self.prepared.ensure_scenes()  # 展開中の警告もレポートに載せるため、警告を写す前に展開する
        for w in self.prepared.warnings:
            self.report.warn(w)
        for m in self.prepared.models:
            if not m.supported:
                self.report.warn(f"skipped {m.entry.pathname}: {m.skip_reason}")

        groups = [] if self.opts.unit == UNIT_SCENES else _plan_groups(self.prepared, self.opts)
        scenes = _select_scenes(self.prepared, self.opts) if self.opts.unit == UNIT_SCENES else []
        self.supported_models = {m.guid: m for m in self.prepared.supported_models}
        selected: dict[str, ModelSummary] = {summary.guid: summary for group in groups for summary, _ in group.models}
        for scene_summary in scenes:
            for placement in scene_summary.placements:
                if placement.model_guid in self.supported_models:
                    selected.setdefault(placement.model_guid, self.supported_models[placement.model_guid])
        models = list(selected.values())
        if not models:
            reasons = {m.skip_reason for m in self.prepared.models if not m.supported}
            what = "models"
            if self.opts.unit == UNIT_PREFABS:
                reasons |= {p.skip_reason for p in self.prepared.prefabs if not p.supported}
                what = "prefabs"
            elif self.opts.unit == UNIT_SCENES:
                reasons |= {s.skip_reason for s in self.prepared.scenes if not s.supported}
                what = "scenes"
            reasons_text = "; ".join(sorted(reasons))
            raise PackageError(f"no importable {what} selected" + (f" ({reasons_text})" if reasons_text else ""))

        self.unity_mats, self.normalized = self.prepared.unity_mats, self.prepared.normalized

        # --- 必要なテクスチャを決めて展開 ---
        # 全モデルを VRM add-on に任せる場合、.mat 由来のマテリアルは組まないのでテクスチャも読まない
        delegate_all = all(_delegate_to_vrm_addon(m.entry, self.opts) for m in models)
        needed_tex = set() if delegate_all else set(self.prepared.referenced_textures)
        if self.opts.import_unreferenced:
            needed_tex.update(e.guid for e in self.pkg.textures())
        for g in sorted(self.prepared.missing_textures):
            self.report.warn(f"texture {g} is referenced but not included in the package")
        needed_tex -= self.prepared.missing_textures

        extract_root = resolve_extract_root(self.opts, package_path)
        self.report.extract_root = str(extract_root)
        self.step(0.2, "Extracting files")
        sidecars = [g for m in models for g in _sidecar_guids(self.pkg, m.entry)]
        written: set[str] = set()  # 実際に書き出した（展開し直した）ファイル
        self.paths = self.pkg.extract(
            [m.guid for m in models] + sidecars + sorted(needed_tex),
            extract_root,
            overwrite=self.opts.overwrite_extracted,
            progress=lambda f, n: self.step(0.2 + 0.3 * f, f"Extracting {n}"),
            max_total_size=self.opts.max_extract_size,
            written=written,
        )

        # --- 画像の読み込み ---
        self.step(0.5, "Loading textures")
        self.images: dict[str, bpy.types.Image | None] = {}
        self.tex_infos: dict[str, TextureImporterInfo] = {}
        for guid in sorted(needed_tex):
            entry = self.pkg.get(guid)
            info = _texture_info(entry, self.report.warn)
            self.tex_infos[guid] = info
            path = self.paths.get(guid)
            image = load_image(path, info, pack=self.opts.pack_images, refresh=guid in written) if path else None
            self.images[guid] = image
            if image is None:
                self.report.warn(f"could not load texture {entry.pathname} (unsupported format?)")
            else:
                self.report.images.append(image.name)

        mat_builder.tag_images(self.images, self.tex_infos)

        # --- パッケージ用コレクション ---
        self.scene = self.context.scene
        self.collection = bpy.data.collections.new(package_path.stem)
        self.scene.collection.children.link(self.collection)
        self.staging = bpy.data.collections.new(f"{package_path.stem} (importing)")  # 読み込み中だけ使う作業用のコレクション
        self.scene.collection.children.link(self.staging)
        self.hidden_objects: list[bpy.types.Object] = []  # 読み込みの後で hide_set するもの
        view_layer = self.context.view_layer
        prev_active = view_layer.active_layer_collection

        self.build_opts = mat_builder.MaterialBuildOptions(
            mode=self.opts.material_mode,
            force_opaque=self.opts.force_opaque,
            backface_culling=self.opts.backface_culling,
            use_normal_maps=self.opts.use_normal_maps,
            use_emission=self.opts.use_emission,
            store_props=self.opts.store_props,
        )

        # 同じ .mat は、この回の読み込みの中で 1 つの Blender マテリアルを共有する（別のモデル同士、同じモデルの読み直し、
        # 1 つのモデルの複数のスロット、prefab のスロット分割のどれでも。#77）
        self.built_by_guid: dict[str, bpy.types.Material] = {}
        self.imported_models: set[str] = set()  # この回で読み込み済みのモデル（prefab ごとに同じモデルを読み直すことがある）
        self.failed_models: set[str] = set()  # 読み込みに失敗したモデル（同じモデルを何度も試さない）

        self.model_by_guid = {m.guid: m for m in self.prepared.models}

        prefab_collections: list[bpy.types.Collection] = []
        total = sum(len(group.models) for group in groups) + len(scenes)
        done = 0
        # インポーターのオペレーターは呼ぶたびにビューレイヤーの中身を評価し直すので、読み込み済みのものが増えるほど 1 回が重くなる
        # （Japanese Street の Day_Showcase では 1 回 70 ms。3000 オブジェクトで 180 ms、ビューレイヤーから外すと 5 ms）。
        # パッケージのコレクションは読み込みが終わるまでビューレイヤーから外し、モデルは空の作業用コレクションに読み込んでから移す（#75）
        package_layer = find_layer_collection(view_layer.layer_collection, self.collection)
        staging_layer = find_layer_collection(view_layer.layer_collection, self.staging)
        if staging_layer is not None:
            view_layer.active_layer_collection = staging_layer
        if package_layer is not None:
            package_layer.exclude = True
        try:
            for group in groups:
                target = self.collection
                if group.prefab is not None:
                    prefab = group.prefab
                    target = bpy.data.collections.new(prefab.name)
                    self.collection.children.link(target)
                    target["unity_prefab"] = prefab.pathname
                    target["unity_prefab_guid"] = prefab.guid
                    prefab_collections.append(target)
                    self.report.prefabs.append(prefab.pathname)
                    for guid in prefab.model_guids:
                        skipped = self.model_by_guid.get(guid)
                        if skipped is not None and not skipped.supported:
                            self.report.warn(f"prefab {prefab.pathname}: skipped {skipped.entry.pathname}: {skipped.skip_reason}")
                hidden_lods = 0
                for summary, prefab_table in group.models:
                    self.step(0.55 + 0.4 * done / max(total, 1), f"Importing {summary.entry.name}")
                    done += 1
                    if summary.guid in self.failed_models:
                        continue
                    try:
                        objects = self.import_model(summary, prefab_table, target)
                    except Exception as exc:  # noqa: BLE001 - そのモデルだけを外して続ける（#70）
                        _record_failure(self.report, f"could not import {summary.entry.pathname}: {exc}")
                        continue
                    # Models 単位の表は prefab をまたいで統合したものなので、LOD を隠すのは prefab 単位のときだけ
                    if group.prefab is not None:
                        hidden_lods += self.hide_prefab_lods(objects, prefab_table)
                if hidden_lods and group.prefab is not None:
                    self.report.warn(f"prefab {group.prefab.pathname}: {lod_warning(hidden_lods)}")
            for scene_summary in scenes:
                try:
                    self.import_scene(scene_summary, 0.55 + 0.4 * done / max(total, 1), 0.4 / max(total, 1))
                except Exception as exc:  # noqa: BLE001 - そのシーンの残りだけを外して続ける（#70）
                    _record_failure(self.report, f"could not import scene {scene_summary.pathname}: {exc}")
                done += 1
        finally:
            if package_layer is not None:
                package_layer.exclude = False
            bpy.data.collections.remove(self.staging)
            if prev_active is not None:
                view_layer.active_layer_collection = prev_active
        for obj in self.hidden_objects:
            if not _is_removed(obj):
                obj.hide_set(True)
        if self.opts.arrange == "SIDE_BY_SIDE" and len(prefab_collections) > 1:
            arrange_collections(self.context, prefab_collections)

        self.collection["unity_package"] = package_path.name
        self.step(1.0, "Done")

    def step(self, fraction: float, message: str) -> None:
        if self.progress is not None:
            self.progress(fraction, message)

    def build_model(self, summary: ModelSummary, prefab_table: dict[str, RendererMaterials], target, before) -> list[bpy.types.Object]:
        """モデルを 1 回読み込んでマテリアルを組み、作られたオブジェクトを返す。"""
        model = summary.entry
        model_info = summary.info
        self.imported_models.add(model.guid)
        # 同名マテリアルの再利用を使うときだけ一覧を作る（シーンでは何百回も読み込むので、毎回作ると重い）
        existing_materials = {m.name: m for m in bpy.data.materials} if self.opts.reuse_existing else {}
        delegated = _import_model(self.paths[model.guid], model, self.opts, target, self.report)
        new = _new_since(before)
        _adopt_into_collection(new, self.scene, target)
        if delegated:
            # add-on が読み込んだ（pack 済みの）画像もレポートに載せる
            self.report.images.extend(i.name for i in new["images"])
        if delegated and not new["objects"]:
            # VRM 0.x の制限付きライセンスでは add-on が確認ダイアログを出し、その場では読み込まない
            self.report.warn(
                f"VRM add-on did not create any objects for {model.name} "
                "(a license confirmation dialog may be waiting; the model is imported after confirming, "
                "outside of this importer)"
            )
        self.report.models.append(model.pathname)
        self.report.objects.extend(o.name for o in new["objects"])

        new_materials: list[bpy.types.Material] = new["materials"]
        fbx_names = [m.name for m in new_materials]
        mesh_objects = [o for o in new["objects"] if o.type == "MESH"]
        # 名前を引けないモデルの中へのマテリアルの上書きは、メッシュが 1 つのときだけそれに当てる（#109）
        prefab_table, unread = resolve_sole_renderer(prefab_table or {}, [o.name for o in mesh_objects])
        if unread:
            self.report.warn(
                f"{model.name}: {unread} material override(s) on an object inside the model are not read yet "
                f"(the model has {len(mesh_objects)} meshes, so the overridden one is unknown); "
                "the model's own assignments are used for them"
            )
        object_slots = {
            o.name: [slot.material.name if slot.material else "" for slot in o.material_slots]
            for o in mesh_objects
        }
        # prefab の m_Materials は Unity のサブメッシュ順で、Blender のスロット順とは限らない
        submesh_order = {o.name: _submesh_order(o) for o in mesh_objects}
        resolution = resolve_materials(
            fbx_names, model_info, self.unity_mats, model.pathname, prefab_table, object_slots, submesh_order
        )
        assignments = slot_assignments(object_slots, prefab_table, self.unity_mats, submesh_order)
        # マテリアルを 1 つも持たないモデルは、prefab の割り当てしか手掛かりが無い（#102）
        for obj_name, slots in object_slots.items():
            if not slots and (obj_name, 0) not in assignments:
                self.report.warn(
                    f"{model.name}: mesh {obj_name!r} has no material slots and no prefab assignment; "
                    "it is left without a material"
                )
        # prefab がすべてのスロットで別の .mat に差し替えるマテリアルは、組み立てても捨てるだけなので組まない
        replaced = fully_replaced_materials(object_slots, assignments, resolution)

        # 同梱 .blend の KEEP と VRM add-on 委譲では、インポーターが作ったマテリアルをそのまま使う
        keep_materials = delegated or (model.ext == ".blend" and self.opts.blend_materials == "KEEP")
        remaining: list[bpy.types.Material] = []  # 削除せずに残した、インポーターが作ったマテリアル
        for bmat in new_materials:
            res = resolution[bmat.name]
            mrep = MaterialReport(blender_name=bmat.name, fbx_name=bmat.name, guid=res.guid, method=res.method)
            self.report.materials.append(mrep)
            if keep_materials:
                remaining.append(bmat)
                mrep.method = "delegated" if delegated else "kept"
                if res.guid:
                    norm = self.normalized[res.guid]
                    mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
                    if self.opts.store_props:
                        mat_builder.store_props(bmat, norm)
                continue
            if bmat.name in replaced:
                if res.guid:
                    norm = self.normalized[res.guid]
                    mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
                remaining.append(bmat)  # 差し替えの後、使われなくなったところで "replaced" にして消す
                continue
            if res.warning:
                mrep.warnings.append(res.warning)
                self.report.warn(f"material {bmat.name!r}: {res.warning}")

            # ファイルに既にある同名のマテリアルを使う設定なら、それを先に見る。無ければ、この回で組み立て済みのものを共有する
            if self.opts.reuse_existing:
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
                self.report.warn(f"material {bmat.name!r}: no matching .mat in package")
                continue

            shared = self.built_by_guid.get(res.guid)
            if shared is not None and shared is not bmat and _named_after_mat(bmat, shared, self.unity_mats[res.guid]):
                # 共有するマテリアルの名前は、なるべく .mat の名前にする。先に組んだものが FBX 側の別の名前（prefab で
                # 解決したものなど）なら、.mat と同じ名前のこちらを組み、先のものの使用箇所をこちらへ付け替える
                _build_and_report(bmat, res.guid, self.normalized, self.images, self.tex_infos, self.build_opts, self.pkg, self.report, mrep)
                old_name = shared.name
                shared.user_remap(bmat)
                for row in self.report.materials:
                    if row is not mrep and row.blender_name == old_name:
                        row.method, row.blender_name = "shared", bmat.name
                if shared in remaining:
                    remaining.remove(shared)
                bpy.data.materials.remove(shared)
                self.built_by_guid[res.guid] = bmat
                continue
            if shared is not None and shared is not bmat:
                remaining.pop()
                _replace_material(new["objects"], bmat, shared)
                bpy.data.materials.remove(bmat)
                norm = self.normalized[res.guid]
                mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
                mrep.method = "shared"
                mrep.blender_name = shared.name
                continue

            _build_and_report(bmat, res.guid, self.normalized, self.images, self.tex_infos, self.build_opts, self.pkg, self.report, mrep)
            self.built_by_guid.setdefault(res.guid, bmat)

        if not keep_materials:
            # インポーターが作った画像（glTF の埋め込み画像など）は .mat から組み直した時点で不要になる
            for image in new["images"]:
                if image.users == 0:
                    bpy.data.images.remove(image)

        # prefab がスロットごとに別の .mat を指している場合は、そのスロットだけ別マテリアルに差し替える
        if assignments and not keep_materials:
            split = _split_slots_by_prefab(
                new["objects"], assignments, resolution, self.unity_mats, self.normalized, self.built_by_guid,
                self.images, self.tex_infos, self.build_opts, self.pkg, self.report,
            )
            if split:
                self.report.split_slots += split
                # 差し替えで使われなくなった FBX マテリアルは片付ける
                for bmat in remaining:
                    if bmat.users == 0:
                        for mrep in self.report.materials:
                            if mrep.blender_name == bmat.name and mrep.method != "replaced":
                                mrep.method = "replaced"
                        # 組み立て済みの表からも外す。残すと、同じモデルを読み直したときに削除済みのマテリアルを使ってしまう
                        for guid in [g for g, m in self.built_by_guid.items() if m == bmat]:
                            del self.built_by_guid[guid]
                        bpy.data.materials.remove(bmat)
        if self.opts.outlines:
            added = outline_builder.apply_outlines(new["objects"], self.opts.outline_width_scale)
            if added:
                self.report.outlines += added
        return new["objects"]

    def import_model(self, summary: ModelSummary, prefab_table: dict[str, RendererMaterials], target) -> list[bpy.types.Object]:
        """モデルを 1 回読み込む。途中で失敗したら、そのモデルで作ったものとレポートの行を消してから例外を送る（#70）。

        読み込みは作業用のコレクションで行い、終わったら ``target`` へ移す（#75）。
        """
        before = _snapshot()
        rows = (self.report.models, self.report.objects, self.report.materials, self.report.images)
        counts = [len(items) for items in rows]
        known = summary.guid in self.imported_models
        try:
            objects = self.build_model(summary, prefab_table, self.staging, before)
            move_collection_contents(self.staging, target)
            return objects
        except Exception:
            _remove_created(before)
            for items, count in zip(rows, counts):
                del items[count:]
            if not known:
                self.imported_models.discard(summary.guid)
            for guid in [g for g, m in self.built_by_guid.items() if _is_removed(m)]:
                del self.built_by_guid[guid]
            self.failed_models.add(summary.guid)
            raise

    def hide_prefab_lods(self, objects: list[bpy.types.Object], prefab_table: dict[str, RendererMaterials]) -> int:
        """prefab の LODGroup で遠景用だったメッシュを隠し、隠した数を返す（#104）。

        段はどのオブジェクトにも ``unity_lod`` として残すので、オプションを切っていても LOD0 かどうかは分かる。
        """
        hidden = 0
        for obj in objects:
            if obj.type != "MESH":
                continue
            rm = find_renderer(prefab_table, obj.name)
            if rm is None:
                continue
            obj["unity_lod"] = rm.lod_level
            if rm.lod_level and self.opts.hide_lods:
                defer_hide(obj, self.hidden_objects)
                hidden += 1
        return hidden

    def import_scene(self, scene_summary: SceneSummary, progress_start: float, progress_span: float) -> None:
        """シーンのモデルを配置どおりに読み込む。同じモデル・同じ割り当ての配置は、メッシュを共有した複製にする。"""
        pathname = scene_summary.pathname
        target = bpy.data.collections.new(scene_summary.name)
        self.collection.children.link(target)
        target["unity_scene"] = pathname
        target["unity_scene_guid"] = scene_summary.guid
        self.report.scenes.append(pathname)

        hierarchy = self.prepared.scene_hierarchies[scene_summary.guid]
        active = effective_active(hierarchy)
        empties: dict[int, bpy.types.Object] = {}
        templates: dict[tuple, SceneTemplate] = {}
        skipped_offsets = 0
        unit_scales: dict[str, float | None] = {}  # モデルの GUID → FBX の UnitScaleFactor
        skipped_nodes = 0  # 名前を引けたが当てられなかった、モデルの中のノードへの位置の上書き
        hidden_unused = 0  # Unity の prefab・シーンが使っていないので隠した FBX の部品
        hidden_lods = 0  # LODGroup の遠景用の段として隠したオブジェクト
        count = len(scene_summary.placements)
        for index, placement in enumerate(scene_summary.placements):
            if index % 25 == 0:
                self.step(progress_start + progress_span * index / max(count, 1), f"Placing {index}/{count} in {scene_summary.name}")
            summary = self.supported_models.get(placement.model_guid)
            if summary is None:
                skipped = self.model_by_guid.get(placement.model_guid)
                if skipped is not None:
                    self.report.warn(f"scene {pathname}: skipped {skipped.entry.pathname}: {skipped.skip_reason}")
                continue
            if summary.guid in self.failed_models:
                continue  # 読み込みに失敗したモデルの配置は飛ばす（エラーは最初の 1 回だけ記録する）
            root = scene_empty(hierarchy, placement.root, target, empties, active, self.hidden_objects)
            table = {
                name: RendererMaterials(name, list(r.materials), r.renderer_class, placement.model_guid)
                for name, r in placement.renderers.items()
            }
            key = placement.signature()
            template = templates.get(key)
            if template is None:
                try:
                    objects = self.import_model(summary, table, target)
                except Exception as exc:  # noqa: BLE001 - そのモデルの配置だけを外して続ける（#70）
                    _record_failure(self.report, f"scene {pathname}: could not import {summary.entry.pathname}: {exc}")
                    continue
                template = templates[key] = SceneTemplate.capture(objects)
            else:
                objects = duplicate_objects(template, target)
                self.report.objects.extend(o.name for o in objects)
            scale = summary.info.global_scale
            attach_to_empty(objects, root, scale)
            if placement.node_transforms:
                if summary.guid not in unit_scales:
                    unit_scales[summary.guid] = read_unit_scale(self.paths[summary.guid]) if summary.entry.ext == ".fbx" else None
                overrides = placement.node_transforms
                if placement.root_node:  # 1 メッシュの FBX のモデルの PrefabInstance（#99）
                    overrides = {k: v for k, v in overrides.items() if k != placement.root_node}
                    placed = apply_collapsed_root(
                        template, objects, root, placement.root_node,
                        placement.node_transforms[placement.root_node], unit_scales[summary.guid],
                    )
                    skipped_nodes += 0 if placed else 1
                if overrides:
                    skipped_nodes += apply_node_transforms(template, objects, overrides, unit_scales[summary.guid])
            if placement.offsets:
                skipped_offsets += apply_offsets(objects, placement.world, placement.offsets, scale)
            hidden = parts_to_hide(
                hierarchy, placement, [(o.name, o.type == "MESH") for o in objects], self.opts.hide_lods
            )
            for obj in objects:
                if obj.name in hidden.lods:
                    obj["unity_lod"] = hidden.lods[obj.name]
                if obj.name in hidden.names:
                    defer_hide(obj, self.hidden_objects)
            hidden_unused += hidden.unused
            hidden_lods += hidden.hidden_lods

        # --- ライト・カメラ ---
        baked_lights = 0
        light_notes: set[str] = set()
        wanted = ([CLASS_LIGHT] if self.opts.scene_lights else []) + ([CLASS_CAMERA] if self.opts.scene_cameras else [])
        for component in scene_components(hierarchy, wanted) if wanted else []:
            node = hierarchy.nodes[component.node]
            if component.node in empties:
                # 同じ GameObject にモデルの配置の Empty があれば、その子にする
                parent, local = empties[component.node], Matrix.Identity(4)
            else:
                parent = scene_empty(hierarchy, node.parent, target, empties, active, self.hidden_objects) if node.parent is not None else None
                local = Matrix(unity_to_blender(node.local))
            if component.class_id == CLASS_LIGHT:
                values = convert_light(component.body, self.prepared.pipeline)
                obj = make_light(component.name, values)
                obj["unity_light"] = json_text(component.body)
                obj["unity_render_pipeline"] = self.prepared.pipeline
                baked_lights += values.baked_only
                light_notes.update(values.notes)
                self.report.lights += 1
            else:
                obj = make_camera(component.name, convert_camera(component.body))
                obj["unity_camera"] = json_text(component.body)
                self.report.cameras += 1
                if self.scene.camera is None and component.active:
                    self.scene.camera = obj
            target.objects.link(obj)
            obj.parent = parent
            location, rotation, _ = (local @ Matrix(LIGHT_CAMERA_BASIS)).decompose()
            obj.matrix_basis = Matrix.LocRotScale(location, rotation, None)  # ライト・カメラにはスケールを掛けない
            if not component.active:
                defer_hide(obj, self.hidden_objects)
        for message in scene_warnings(
            pathname, scene_summary.contents, baked_lights=baked_lights, light_notes=light_notes,
            hidden_unused=hidden_unused, hidden_lods=hidden_lods,
            skipped_nodes=skipped_nodes, skipped_offsets=skipped_offsets,
        ):
            self.report.warn(message)


def _run_import(
    context,
    filepath: str,
    opts: ImportOptions,
    progress,
    prepared: PreparedPackage | None,
    report: ImportReport,
) -> None:
    _ImportSession(context, filepath, opts, progress, prepared, report).run()


def _build_and_report(bmat, guid, normalized, images, tex_infos, build_opts, pkg, report, mrep) -> None:
    norm = normalized[guid]
    mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
    try:
        mode, warnings = mat_builder.build_material(bmat, norm, images, tex_infos, build_opts)
    except Exception as exc:  # noqa: BLE001 - 1 マテリアルの失敗で全体を止めない
        mrep.warnings.append(f"node build failed: {exc!r}")
        report.warn(f"material {bmat.name!r}: node build failed: {exc!r}")
        _log_exception(f"node build failed for material {bmat.name!r}")
        return
    mrep.mode = mode
    mrep.warnings.extend(warnings)
    for w in warnings:
        if w.startswith(("shader table entry", "shader approximation:")):
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
    """(オブジェクト, スロット) ごとに prefab の .mat を割り当てる。割り当てたスロット数を返す。

    モデルがマテリアルを持たないとスロットは 0 個になるが、Unity では Renderer の ``m_Materials[0]`` で描かれる。
    このときはスロット 0 を作ってから割り当てる（#102）。
    """
    by_name = {o.name: o for o in objects if o.type == "MESH"}
    count = 0
    for (obj_name, index), guid in assignments.items():
        obj = by_name.get(obj_name)
        if obj is None:
            continue
        if index >= len(obj.material_slots):
            if index != 0:  # index が 0 で範囲外なら、スロットが 1 つも無いメッシュ
                continue
            obj.data.materials.append(None)
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


def _named_after_mat(candidate: bpy.types.Material, current: bpy.types.Material, umat: UnityMaterial) -> bool:
    """共有しているマテリアルより、``candidate`` のほうが .mat の名前に合うか（連番は外して比べる）。"""
    name = umat.name
    return bool(name) and strip_numeric_suffix(candidate.name) == name and strip_numeric_suffix(current.name) != name


def _replace_material(objects, old: bpy.types.Material, new: bpy.types.Material) -> None:
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material == old:
                slot.material = new
