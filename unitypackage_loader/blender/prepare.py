"""パッケージの事前解析。走査して .mat・モデル・prefab を解析し、モデル選択ダイアログと読み込みの両方で使う ``PreparedPackage`` を作る。

シーンの展開は重いので、読み込む単位 Scenes を選んだとき（``PreparedPackage.ensure_scenes``）まで遅らせる（#75）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import bpy

from ..core.hierarchy import Expander, Hierarchy, ModelCatalog, RawAsset, parse_asset
from ..core.lights import decide_pipeline, pipelines_in_assets
from ..core.mapping import material_textures, resolve_materials
from ..core.material import NormalizedMaterial, UnityMaterial, parse_material
from ..core.meta import ModelImporterInfo, TextureImporterInfo
from ..core.package import AssetEntry, PackageError, UnityPackage
from ..core.prefab import PrefabAnalysis, RendererMaterials, analyze_prefab, merge_prefab_tables
from ..core.profiles import ShaderTable, normalize_material
from ..core.profiles.base import default_table
from ..core.shader_graph import UVTransform, apply_uv_transforms, texture_uv_transforms
from ..core.shader_source import SHADER_SOURCE_EXTS, declared_properties
from ..core.units import PrefabSummary, SceneSummary, choice_count, summarize_prefabs, summarize_scene
from .log import log_exception, warn_skipped
from .model_import import operator_available

# 読み込めるモデル形式
SUPPORTED_MODEL_EXTS = frozenset({".fbx", ".obj", ".gltf", ".glb", ".vrm", ".dae", ".blend"})

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
    # prefab の MeshCollider だけが使い、prefab の Renderer も配置も使っていない（当たり判定用のメッシュ。選択ダイアログの
    # Models で、既定のチェックを外して理由を出す。読み込めないわけではないので supported は変えない。#166）
    collider_only: bool = False

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
    # prefab の pathname → その prefab の解析結果（モデルごとの表など）。モデルを使う prefab だけ
    prefab_analyses: dict[str, PrefabAnalysis] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    prefabs: list[PrefabSummary] = field(default_factory=list)  # 読み込む単位 Prefabs の候補（pathname 順）
    scene_entries: list[AssetEntry] = field(default_factory=list)  # パッケージ内のシーン（pathname 順。展開は後回し）
    scene_hierarchies: dict[str, Hierarchy] = field(default_factory=dict)  # シーンの GUID → 展開した階層（ensure_scenes で作る）
    pipeline: str = "BUILTIN"  # レンダーパイプライン（ライトの強さの換算に使う。決め方は core/lights.decide_pipeline）
    pipeline_source: str = "default"  # lights（ライト・カメラの追加データ）/ materials / default
    catalog: ModelCatalog = field(default_factory=ModelCatalog)  # 展開と配置に使うモデルの表（_catalog）
    _scenes: list[SceneSummary] | None = field(default=None, repr=False)
    _expander: Expander | None = field(default=None, repr=False)  # prefab の展開結果を持つ（シーンの展開で使い回す）

    def ensure_scenes(self) -> list[SceneSummary]:
        """シーンを展開して、読み込む単位 Scenes の候補（pathname 順）を返す。展開は初めて呼ばれたときの 1 回だけ。

        大きなシーンの解析は重いので、Models / Prefabs だけを読む場合には展開しない（#75）。展開中の警告は ``warnings`` に加える。
        """
        if self._scenes is None:
            unsupported = {m.guid: m.skip_reason for m in self.models if not m.supported}
            if self._expander is None:
                self._expander = self.catalog.expander(_prefab_reader(self.pkg, self.warnings.append))
            self._scenes, self.scene_hierarchies = _prepare_scenes(
                self.pkg, self._expander, self.catalog, self.warnings.append, unsupported
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
        candidates = sorted(p for p, analysis in self.prefab_analyses.items() if model_guid in analysis.tables)
        return merge_prefab_tables([self.prefab_analyses[p].tables[model_guid] for p in candidates])


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
        log_exception(f"could not read shader table {path}")
        return merged
    merged.by_guid.update(extra.by_guid)
    merged.by_builtin_id.update(extra.by_builtin_id)
    return merged


def _model_info(entry: AssetEntry, warn: Callable[[str], None]) -> ModelImporterInfo:
    """モデルの .meta を読む。壊れていても（構文エラー・ネスト過多）そのモデルだけ既定値で続ける。"""
    if not entry.meta_text:
        return ModelImporterInfo()
    try:
        return ModelImporterInfo.from_meta(entry.meta_text)
    except Exception as exc:  # noqa: BLE001 - 補助データなので、そのモデルだけ既定値で続ける（#69）
        warn_skipped(warn, f"could not parse .meta of {entry.pathname}", exc)
        return ModelImporterInfo()


def texture_info(entry: AssetEntry, warn: Callable[[str], None]) -> TextureImporterInfo:
    if not entry.meta_text:
        return TextureImporterInfo()
    try:
        return TextureImporterInfo.from_meta(entry.meta_text)
    except Exception as exc:  # noqa: BLE001 - 補助データなので、その画像だけ既定値で続ける（#69）
        warn_skipped(warn, f"could not parse .meta of {entry.pathname}", exc)
        return TextureImporterInfo()


class _ShaderDeclarations:
    """パッケージに入っているシェーダーの定義が宣言するプロパティ名（GUID ごとに 1 度だけ読む。#118）。

    Shader Graph なら、テクスチャを読む UV の Tiling & Offset も読む（``uv_transforms``。#193）。
    """

    def __init__(self, pkg: UnityPackage, warn: Callable[[str], None]):
        self._pkg = pkg
        self._warn = warn
        self._cache: dict[str, frozenset[str] | None] = {}
        self._transforms: dict[str, dict[str, UVTransform]] = {}

    def uv_transforms(self, guid: str | None) -> dict[str, UVTransform]:
        if not guid:
            return {}
        if guid not in self._transforms:
            self._transforms[guid] = self._read_transforms(guid)
        return self._transforms[guid]

    def _read_transforms(self, guid: str) -> dict[str, UVTransform]:
        entry = self._pkg.get(guid)
        if entry is None or not entry.has_asset or entry.ext != ".shadergraph":
            return {}

        def subgraph(sub_guid: str) -> str | None:
            sub = self._pkg.get(sub_guid)
            if sub is None or not sub.has_asset or sub.ext != ".shadersubgraph":
                return None
            return self._pkg.read_text(sub_guid)

        try:
            return texture_uv_transforms(self._pkg.read_text(guid), subgraph)
        except Exception as exc:  # noqa: BLE001 - 補助データなので、テクスチャの Tiling をそのまま使って続ける（#69）
            warn_skipped(self._warn, f"could not read the texture UVs of {entry.pathname}", exc)
            return {}

    def get(self, guid: str | None) -> frozenset[str] | None:
        if not guid:
            return None
        if guid not in self._cache:
            self._cache[guid] = self._read(guid)
        return self._cache[guid]

    def _read(self, guid: str) -> frozenset[str] | None:
        entry = self._pkg.get(guid)
        if entry is None or not entry.has_asset or entry.ext not in SHADER_SOURCE_EXTS:
            return None
        try:
            return declared_properties(self._pkg.read_asset(guid), entry.ext) or None
        except Exception as exc:  # noqa: BLE001 - 補助データなので、宣言を使わずに続ける（#69）
            warn_skipped(self._warn, f"could not read the properties of {entry.pathname}", exc)
            return None


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
    declared = _ShaderDeclarations(pkg, warnings.append)
    for entry in pkg.materials():
        try:
            umat = parse_material(pkg.read_asset(entry.guid), entry.guid, entry.pathname)
            # Shader Graph がグラフの中で UV を変えるテクスチャは、その値を Tiling / Offset として読む（#193）
            umat = apply_uv_transforms(umat, declared.uv_transforms(umat.shader_guid))
            norm = normalize_material(umat, table, declared.get(umat.shader_guid))
        except Exception as exc:  # noqa: BLE001 - その .mat だけを外して続ける（#69）
            warn_skipped(warnings.append, f"could not parse {entry.pathname}", exc)
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

    referenced = material_textures(normalized, normalized)
    missing = {g for g in referenced if pkg.get(g) is None}

    # prefab は Scenes 単位と同じ展開器で展開する（Prefab Variant・ネスト・上書き・削除・古い形式。#71）。
    # 展開結果はキャッシュされ、あとでシーンを展開するときにも使う
    model_guids = [m.guid for m in models]
    catalog = _catalog(models)
    collider_meshes: set[str] = set()
    expander = catalog.expander(_prefab_reader(pkg, warnings.append, collider_meshes))
    used_by_prefabs: set[str] = set()  # prefab の Renderer か配置が使うモデル
    prefab_analyses: dict[str, PrefabAnalysis] = {}
    for entry in pkg.prefabs():
        try:
            hierarchy = expander.expand_asset(entry.guid)
            if hierarchy is None:  # 読めなかった（警告は読むときに出している）
                continue
            placed = catalog.placements(hierarchy)
            analysis = analyze_prefab(hierarchy, placed)
            used_by_prefabs.update(p.model_guid for p in placed)
        except Exception as exc:  # noqa: BLE001 - その prefab の割り当てだけを外して続ける（#69）
            warn_skipped(warnings.append, f"could not resolve prefab {entry.pathname}", exc)
            continue
        if hierarchy.unresolved_material_overrides:  # 位置などの上書きは Models / Prefabs 単位では使わないので数えない
            warnings.append(
                f"prefab {entry.pathname}: {hierarchy.unresolved_material_overrides} material override(s) on objects inside a model "
                "are not read yet; the model's own assignments are used for them"
            )
        if analysis.tables:
            prefab_analyses[entry.pathname] = analysis
    for m in models:
        m.collider_only = m.guid in collider_meshes and m.guid not in used_by_prefabs
    unsupported = {m.guid: m.skip_reason for m in models if not m.supported}
    # シェーダー表で分かったマテリアルだけを手掛かりにする。指紋で URP / HDRP と読んだ表に無いシェーダーは使わない（#116）。
    # シーン・prefab のライト・カメラに URP / HDRP の追加データがあれば、そちらを優先する（#120）
    pipeline = decide_pipeline(
        (n.family for n in normalized.values() if n.shader_name),
        pipelines_in_assets(_asset_bytes(pkg, pkg.scenes() + pkg.prefabs())),
    )
    if pipeline.warning:
        warnings.append(pipeline.warning)
    prefab_tables = {pathname: analysis.tables for pathname, analysis in prefab_analyses.items()}
    prefabs = summarize_prefabs(((e.guid, e.pathname) for e in pkg.prefabs()), prefab_tables, model_guids, unsupported)
    return PreparedPackage(
        path, pkg, unity_mats, normalized, models, referenced, missing,
        prefab_analyses=prefab_analyses, warnings=warnings, prefabs=prefabs, scene_entries=pkg.scenes(), _expander=expander,
        pipeline=pipeline.pipeline,
        pipeline_source=pipeline.source,
        catalog=catalog,
    )


def _asset_bytes(pkg: UnityPackage, entries: list[AssetEntry]):
    """走査でメモリに読んだアセットの中身。手掛かりを探すだけなので、キャッシュに無いもののために再走査はしない。"""
    for entry in entries:
        data = pkg.cached_asset(entry.guid)
        if data is not None:
            yield data


def _catalog(models: list[ModelSummary]) -> ModelCatalog:
    """展開と配置に使うモデルの表。

    ルートの名前はファイル名から拡張子を除いたもの。古い形式の .meta の fileIDToRecycleName は中への上書きを名前に
    結び付けるのに使う（#53）。FBX のノードが 1 つだけのモデル（表の無い 1 メッシュの FBX。#181）と、最上位のノードが
    1 つだけのモデル（Unity が畳むノード。#185）は、走査のときに FBX から読んだノード名。
    """
    return ModelCatalog(
        names={m.guid: Path(m.entry.pathname).stem for m in models},
        recycle_names={m.guid: m.info.recycle_names for m in models},
        sole_nodes={m.guid: m.entry.fbx_sole_node for m in models if m.entry.fbx_sole_node},
        top_nodes={m.guid: m.entry.fbx_top_node for m in models if m.entry.fbx_top_node},
    )


def _prefab_reader(
    pkg: UnityPackage, warn: Callable[[str], None], collider_meshes: set[str] | None = None
) -> Callable[[str], RawAsset | None]:
    """展開器に渡す読み取り関数。prefab 以外と、読めない prefab は None（元の無いインスタンスとして扱う。#69）。

    ``collider_meshes`` を渡すと、読んだ prefab の MeshCollider が指すアセットの GUID を加える（#166）。
    """
    prefab_guids = {e.guid for e in pkg.prefabs()}

    def read(guid: str) -> RawAsset | None:
        if guid not in prefab_guids:
            return None
        try:
            raw = parse_asset(pkg.read_asset(guid))
            if collider_meshes is not None:
                collider_meshes.update(g.lower() for g in raw.collider_meshes)
            return raw
        except Exception as exc:  # noqa: BLE001 - prefab は補助情報なので、その prefab だけを外して続ける
            warn_skipped(warn, f"could not parse prefab {pkg.get(guid).pathname}", exc)
            return None

    return read


def _prepare_scenes(
    pkg: UnityPackage, expander: Expander, catalog: ModelCatalog, warn: Callable[[str], None], unsupported: dict[str, str]
) -> tuple[list[SceneSummary], dict[str, Hierarchy]]:
    """シーンを展開して候補にする。prefab の展開結果は prefab の表づくりと共有する（``expander`` のキャッシュ）。"""
    entries = pkg.scenes()
    if not entries:
        return [], {}
    scenes: list[SceneSummary] = []
    hierarchies: dict[str, Hierarchy] = {}
    for entry in entries:
        contents = None
        try:
            hierarchy = expander.expand_raw(parse_asset(pkg.read_asset(entry.guid)))
            contents = catalog.summarize(hierarchy)
            hierarchies[entry.guid] = hierarchy
        except Exception as exc:  # noqa: BLE001 - 読めないシーンは候補を灰色にし、モデルや prefab の読み込みは続ける（#69）
            warn_skipped(warn, f"could not read scene {entry.pathname}", exc)
        scenes.append(summarize_scene(entry.guid, entry.pathname, contents, unsupported))
    return scenes, hierarchies
