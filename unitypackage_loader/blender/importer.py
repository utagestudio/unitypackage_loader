"""インポート全体のオーケストレーション。

``prepare_package`` でパッケージを走査・解析し（モデル選択ダイアログにも使う）、
``run_import`` で実際の展開・FBX 読み込み・マテリアル構築を行う。
"""

from __future__ import annotations

import json
import math
import traceback
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
    RawAsset,
    effective_active,
    parse_asset,
    summarize,
)
from ..core.hierarchy import components as scene_components
from ..core.lights import LIGHT_CAMERA_BASIS, BlenderCamera, BlenderLight, convert_camera, convert_light, detect_pipeline
from ..core.unity_yaml import UnityRef
from ..core.fbx_units import read_unit_scale
from ..core.unity_ids import mesh_file_id
from ..core.transform import BLENDER_TO_UNITY, UNITY_TO_BLENDER, trs, unity_to_blender
from ..core.mapping import fully_replaced_materials, resolve_materials, slot_assignments, submesh_slot_order
from ..core.material import NormalizedMaterial, UnityMaterial, parse_material
from ..core.meta import ModelImporterInfo, TextureImporterInfo, strip_numeric_suffix
from ..core.package import AssetEntry, PackageError, UnityPackage
from ..core.prefab import RendererMaterials, merge_prefab_tables, tables_from_hierarchy
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
from ..ui.preferences import get_prefs
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
    unit: str = "MODELS"  # 読み込む単位: SCENES / PREFABS / MODELS
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


def _scene_empty(
    hierarchy: Hierarchy, key: int, collection, empties: dict[int, bpy.types.Object], active: dict[int, bool], hidden: list
):
    """Node と、まだ作っていない祖先の Empty を作り、Node の Empty を返す（Unity の親子関係を再現する）。

    非アクティブな Node の Empty は ``hidden`` に加える（読み込み中はビューレイヤーに無いので、後で ``hide_set`` する）。
    """
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
            _hide(empty, hidden)
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
    if values.use_temperature:
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


def _by_object_name(table: dict, name: str):
    """オブジェクト名で表を引く。完全一致 → 連番を外した形の順（元の名前が数字で終わる部品を連番と取り違えない）。"""
    if name in table:
        return table[name]
    return table.get(strip_numeric_suffix(name))


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
        spec = _by_object_name(overrides, obj.name)
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


def _hide(obj: bpy.types.Object, hidden: list) -> None:
    """レンダリングからはすぐ外し、ビューポートの非表示は ``hidden`` に積んで読み込みの後で当てる。

    読み込み中のコレクションはビューレイヤーから外していて、そこにあるオブジェクトには ``hide_set`` を使えない。
    """
    obj.hide_render = True
    hidden.append(obj)


def _move_collection_contents(source: bpy.types.Collection, target: bpy.types.Collection) -> None:
    """``source`` のオブジェクトと子コレクションを ``target`` へ移す（先にリンクしてから外す）。"""
    for obj in list(source.objects):
        if obj.name not in target.objects:
            target.objects.link(obj)
        source.objects.unlink(obj)
    for child in list(source.children):
        if child.name not in target.children:
            target.children.link(child)
        source.children.unlink(child)


def _world_matrix(obj: bpy.types.Object | None) -> Matrix | None:
    """評価を待たずに、親をたどって ``matrix_world`` を求める（コンストレイントは見ない）。

    読み込み中のコレクションはビューレイヤーから外していて評価されないため。ボーンなどオブジェクト以外を親にするものが
    途中にあれば None。``obj`` が None なら単位行列。
    """
    matrix = Matrix.Identity(4)
    count = 0
    while obj is not None and count < 1000:
        if obj.parent is None:
            return obj.matrix_basis @ matrix
        if obj.parent_type != "OBJECT":
            return None
        matrix = obj.matrix_parent_inverse @ obj.matrix_basis @ matrix
        obj, count = obj.parent, count + 1
    return matrix if obj is None else None


def _apply_offsets(objects, root_world, offsets, scale: float) -> int:
    """中のノードが上書きで動いたオブジェクトを、そのノードから逆算した位置に置く。アーマチュアで変形するものは数えて飛ばす。

    ``root_world`` は配置のルートの Unity での行列。置いた直後のオブジェクトの行列は「ルートの行列 · globalScale ·
    原点に読み込んだときの行列」なので、そこから原点での行列を求め、逆算したルートの行列を掛け直す。
    行列は親をたどって計算で求める（以前はオブジェクトごとに ``view_layer.update()`` を呼んでいた。#75）。
    """
    def depth(obj) -> int:
        count = 0
        while obj.parent is not None and count < 1000:
            obj, count = obj.parent, count + 1
        return count

    targets = [o for o in objects if _by_object_name(offsets, o.name) is not None]
    if not targets:
        return 0
    scale_matrix = Matrix.Scale(scale, 4) if math.isfinite(scale) and scale > 0 else Matrix.Identity(4)
    to_origin = (Matrix(unity_to_blender(root_world)) @ scale_matrix).inverted_safe()
    worlds = {o: _world_matrix(o) for o in targets}  # 動かす前にまとめて求める
    skipped = 0
    for obj in sorted(targets, key=depth):
        deformed = obj.parent_type in {"BONE", "ARMATURE"} or any(m.type == "ARMATURE" for m in obj.modifiers)
        parent_world = _world_matrix(obj.parent)  # 親を先に動かしているので、ここで求め直す
        if deformed or obj.type == "ARMATURE" or worlds[obj] is None or parent_world is None:
            skipped += 1
            continue
        world = Matrix(unity_to_blender(_by_object_name(offsets, obj.name))) @ scale_matrix @ to_origin @ worlds[obj]
        frame = parent_world @ obj.matrix_parent_inverse if obj.parent is not None else Matrix.Identity(4)
        obj.matrix_basis = frame.inverted_safe() @ world
    return skipped


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


def _run_import(
    context,
    filepath: str,
    opts: ImportOptions,
    progress,
    prepared: PreparedPackage | None,
    report: ImportReport,
) -> None:
    package_path = Path(filepath)

    def step(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    step(0.0, "Scanning package")
    if prepared is None:
        prepared = prepare_package(
            filepath, build_shader_table(opts.shader_table_path), import_blend=opts.import_blend
        )
    pkg = prepared.pkg
    if opts.unit == UNIT_SCENES:
        prepared.ensure_scenes()  # 展開中の警告もレポートに載せるため、警告を写す前に展開する
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
    written: set[str] = set()  # 実際に書き出した（展開し直した）ファイル
    paths = pkg.extract(
        [m.guid for m in models] + sidecars + sorted(needed_tex),
        extract_root,
        overwrite=opts.overwrite_extracted,
        progress=lambda f, n: step(0.2 + 0.3 * f, f"Extracting {n}"),
        max_total_size=opts.max_extract_size,
        written=written,
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
        image = load_image(path, info, pack=opts.pack_images, refresh=guid in written) if path else None
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
    staging = bpy.data.collections.new(f"{package_path.stem} (importing)")  # 読み込み中だけ使う作業用のコレクション
    scene.collection.children.link(staging)
    hidden_objects: list[bpy.types.Object] = []  # 読み込みの後で hide_set するもの
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

    # 同じ .mat は、この回の読み込みの中で 1 つの Blender マテリアルを共有する（別のモデル同士、同じモデルの読み直し、
    # 1 つのモデルの複数のスロット、prefab のスロット分割のどれでも。#77）
    built_by_guid: dict[str, bpy.types.Material] = {}
    imported_models: set[str] = set()  # この回で読み込み済みのモデル（prefab ごとに同じモデルを読み直すことがある）
    failed_models: set[str] = set()  # 読み込みに失敗したモデル（同じモデルを何度も試さない）

    def build_model(summary: ModelSummary, prefab_table: dict[str, RendererMaterials], target, before) -> list[bpy.types.Object]:
        """モデルを 1 回読み込んでマテリアルを組み、作られたオブジェクトを返す。"""
        model = summary.entry
        model_info = summary.info
        imported_models.add(model.guid)
        # 同名マテリアルの再利用を使うときだけ一覧を作る（シーンでは何百回も読み込むので、毎回作ると重い）
        existing_materials = {m.name: m for m in bpy.data.materials} if opts.reuse_existing else {}
        delegated = _import_model(paths[model.guid], model, opts, target, report)
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
        # prefab がすべてのスロットで別の .mat に差し替えるマテリアルは、組み立てても捨てるだけなので組まない
        replaced = fully_replaced_materials(object_slots, assignments, resolution)

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
                        mat_builder.store_props(bmat, norm)
                continue
            if bmat.name in replaced:
                if res.guid:
                    norm = normalized[res.guid]
                    mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
                remaining.append(bmat)  # 差し替えの後、使われなくなったところで "replaced" にして消す
                continue
            if res.warning:
                mrep.warnings.append(res.warning)
                report.warn(f"material {bmat.name!r}: {res.warning}")

            # ファイルに既にある同名のマテリアルを使う設定なら、それを先に見る。無ければ、この回で組み立て済みのものを共有する
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

            shared = built_by_guid.get(res.guid)
            if shared is not None and shared is not bmat and _named_after_mat(bmat, shared, unity_mats[res.guid]):
                # 共有するマテリアルの名前は、なるべく .mat の名前にする。先に組んだものが FBX 側の別の名前（prefab で
                # 解決したものなど）なら、.mat と同じ名前のこちらを組み、先のものの使用箇所をこちらへ付け替える
                _build_and_report(bmat, res.guid, normalized, images, tex_infos, build_opts, pkg, report, mrep)
                old_name = shared.name
                shared.user_remap(bmat)
                for row in report.materials:
                    if row is not mrep and row.blender_name == old_name:
                        row.method, row.blender_name = "shared", bmat.name
                if shared in remaining:
                    remaining.remove(shared)
                bpy.data.materials.remove(shared)
                built_by_guid[res.guid] = bmat
                continue
            if shared is not None and shared is not bmat:
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

    def import_model(summary: ModelSummary, prefab_table: dict[str, RendererMaterials], target) -> list[bpy.types.Object]:
        """モデルを 1 回読み込む。途中で失敗したら、そのモデルで作ったものとレポートの行を消してから例外を送る（#70）。

        読み込みは作業用のコレクションで行い、終わったら ``target`` へ移す（#75）。
        """
        before = _snapshot()
        rows = (report.models, report.objects, report.materials, report.images)
        counts = [len(items) for items in rows]
        known = summary.guid in imported_models
        try:
            objects = build_model(summary, prefab_table, staging, before)
            _move_collection_contents(staging, target)
            return objects
        except Exception:
            _remove_created(before)
            for items, count in zip(rows, counts):
                del items[count:]
            if not known:
                imported_models.discard(summary.guid)
            for guid in [g for g, m in built_by_guid.items() if _is_removed(m)]:
                del built_by_guid[guid]
            failed_models.add(summary.guid)
            raise

    model_by_guid = {m.guid: m for m in prepared.models}

    def import_scene(scene_summary: SceneSummary, progress_start: float, progress_span: float) -> None:
        """シーンのモデルを配置どおりに読み込む。同じモデル・同じ割り当ての配置は、メッシュを共有した複製にする。"""
        pathname = scene_summary.pathname
        target = bpy.data.collections.new(scene_summary.name)
        collection.children.link(target)
        target["unity_scene"] = pathname
        target["unity_scene_guid"] = scene_summary.guid
        report.scenes.append(pathname)

        hierarchy = prepared.scene_hierarchies[scene_summary.guid]
        active = effective_active(hierarchy)
        empties: dict[int, bpy.types.Object] = {}
        templates: dict[tuple, _SceneTemplate] = {}
        skipped_offsets = 0
        unit_scales: dict[str, float | None] = {}  # モデルの GUID → FBX の UnitScaleFactor
        skipped_nodes = 0  # 名前を引けたが当てられなかった、モデルの中のノードへの位置の上書き
        hidden_unused = 0  # Unity の prefab・シーンが使っていないので隠した FBX の部品
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
            if summary.guid in failed_models:
                continue  # 読み込みに失敗したモデルの配置は飛ばす（エラーは最初の 1 回だけ記録する）
            root = _scene_empty(hierarchy, placement.root, target, empties, active, hidden_objects)
            table = {
                name: RendererMaterials(name, list(r.materials), r.renderer_class, placement.model_guid)
                for name, r in placement.renderers.items()
            }
            key = placement.signature()
            template = templates.get(key)
            if template is None:
                try:
                    objects = import_model(summary, table, target)
                except Exception as exc:  # noqa: BLE001 - そのモデルの配置だけを外して続ける（#70）
                    _record_failure(report, f"scene {pathname}: could not import {summary.entry.pathname}: {exc}")
                    continue
                template = templates[key] = _SceneTemplate.capture(objects)
            else:
                objects = _duplicate_objects(template, target)
                report.objects.extend(o.name for o in objects)
            scale = summary.info.global_scale
            _attach_to_empty(objects, root, scale)
            if placement.node_transforms:
                if summary.guid not in unit_scales:
                    unit_scales[summary.guid] = read_unit_scale(paths[summary.guid]) if summary.entry.ext == ".fbx" else None
                skipped_nodes += _apply_node_transforms(template, objects, placement.node_transforms, unit_scales[summary.guid])
            if placement.offsets:
                skipped_offsets += _apply_offsets(objects, placement.world, placement.offsets, scale)
            hidden = {name for name, r in placement.renderers.items() if not r.visible}
            # 展開した prefab・シーンの Renderer から作った配置では、Unity にあるのは表の Renderer だけ。
            # FBX にしかない部品（prefab が使っていない LOD や別のノード）は隠す（#58）。FBX 由来の名前に「.002」が
            # 付いていることもあるので、表の名前は連番を外した形でも照合する
            from_renderers = placement.root in hierarchy.nodes and hierarchy.nodes[placement.root].model_guid is None
            used = set(placement.renderers) | {strip_numeric_suffix(n) for n in placement.renderers}
            # 表で名前を引けないモデル（新しい形式）は、Renderer のメッシュの fileID をオブジェクト名のハッシュと照合する
            mesh_ids = {r.mesh_file_id for r in placement.renderers.values() if r.mesh_file_id}
            for obj in objects:
                name = strip_numeric_suffix(obj.name)
                unused = (
                    from_renderers and obj.type == "MESH" and obj.name not in used and name not in used
                    and mesh_file_id(obj.name) not in mesh_ids and mesh_file_id(name) not in mesh_ids
                )
                if not placement.active or name in hidden or unused:
                    _hide(obj, hidden_objects)
                if unused and placement.active:
                    hidden_unused += 1

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
                parent = _scene_empty(hierarchy, node.parent, target, empties, active, hidden_objects) if node.parent is not None else None
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
                _hide(obj, hidden_objects)
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
        if hidden_unused:
            report.warn(
                f"scene {pathname}: {hidden_unused} object(s) from model files are not used by the Unity prefabs or scene "
                "and were hidden (they stay in the file; unhide them if a renamed part was hidden by mistake)"
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
    # インポーターのオペレーターは呼ぶたびにビューレイヤーの中身を評価し直すので、読み込み済みのものが増えるほど 1 回が重くなる
    # （Japanese Street の Day_Showcase では 1 回 70 ms。3000 オブジェクトで 180 ms、ビューレイヤーから外すと 5 ms）。
    # パッケージのコレクションは読み込みが終わるまでビューレイヤーから外し、モデルは空の作業用コレクションに読み込んでから移す（#75）
    package_layer = _find_layer_collection(view_layer.layer_collection, collection)
    staging_layer = _find_layer_collection(view_layer.layer_collection, staging)
    if staging_layer is not None:
        view_layer.active_layer_collection = staging_layer
    if package_layer is not None:
        package_layer.exclude = True
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
            for summary, prefab_table in group.models:
                step(0.55 + 0.4 * done / max(total, 1), f"Importing {summary.entry.name}")
                done += 1
                if summary.guid in failed_models:
                    continue
                try:
                    import_model(summary, prefab_table, target)
                except Exception as exc:  # noqa: BLE001 - そのモデルだけを外して続ける（#70）
                    _record_failure(report, f"could not import {summary.entry.pathname}: {exc}")
        for scene_summary in scenes:
            try:
                import_scene(scene_summary, 0.55 + 0.4 * done / max(total, 1), 0.4 / max(total, 1))
            except Exception as exc:  # noqa: BLE001 - そのシーンの残りだけを外して続ける（#70）
                _record_failure(report, f"could not import scene {scene_summary.pathname}: {exc}")
            done += 1
    finally:
        if package_layer is not None:
            package_layer.exclude = False
        bpy.data.collections.remove(staging)
        if prev_active is not None:
            view_layer.active_layer_collection = prev_active
    for obj in hidden_objects:
        if not _is_removed(obj):
            obj.hide_set(True)
    if opts.arrange == "SIDE_BY_SIDE" and len(prefab_collections) > 1:
        _arrange_collections(context, prefab_collections)

    collection["unity_package"] = package_path.name
    step(1.0, "Done")


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
