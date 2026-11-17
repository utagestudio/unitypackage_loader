"""インポート全体のオーケストレーション。

``prepare_package``（``prepare.py``）でパッケージを走査・解析し（モデル選択ダイアログにも使う）、``run_import`` で
実際の展開・モデルの読み込み・マテリアル構築を行う。1 回の読み込みの手順は ``_ImportSession``、その中のマテリアルは
``SessionMaterials``（``session_materials.py``）、シーン 1 つの配置は ``SceneImport``（``scene_placement.py``）。
以前はこのファイル 1 つ（約 1,900 行）だったので、役割ごとに分けた（#198）。ほかのモジュールとツールが使う名前は、
今までどおりここから import できる。
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import bpy
from mathutils import Matrix

from ..core.decals import Decal, decal_from_body
from ..core.exposure import exposure_settings, global_volumes, scene_exposure
from ..core.hierarchy import CLASS_CAMERA, CLASS_MONO_BEHAVIOUR, PartState, effective_active, world_matrices
from ..core.hierarchy import components as scene_components
from ..core.lights import PIPELINE_HDRP
from ..core.mapping import expected_materials, material_textures
from ..core.meta import strip_numeric_suffix
from ..core.model_ids import PartMatch, PartMatcher, fbx_node_names
from ..core.package import AssetEntry, PackageError, UnityPackage, default_nested_package, merge_nested_package
from ..core.prefab import PrefabAnalysis, RendererMaterials, find_renderer, has_pending_renderers, resolve_model_renderers, unused_parts
from ..core.report import ImportReport
from ..core.scene_import import PlacementStats, parts_to_remove, prefab_warnings
from ..core.transform import unity_to_blender
from ..core.unity_yaml import parse_documents
from ..core.units import UNIT_PREFABS, UNIT_SCENES, PrefabSummary, SceneSummary
from . import outline as outline_builder
from .log import record_failure, warn_skipped
from .model_import import (
    adopt_into_collection,
    delegate_to_vrm_addon,
    import_model_file,
    is_removed,
    model_objects,
    new_since,
    operator_available,
    remove_created,
    sidecar_guids,
    snapshot,
    vrm_addon_available,
)
from .options import ImportOptions
from .prepare import (
    BLEND_DISABLED_REASON,
    COLLADA_UNAVAILABLE_REASON,
    SUPPORTED_MODEL_EXTS,
    ModelSummary,
    PreparedPackage,
    build_shader_table,
    prepare_package,
)
from .scene_objects import arrange_collections, defer_hide, find_layer_collection, move_collection_contents, remove_parts
from .scene_placement import SceneImport
from .session_materials import SessionMaterials
from .terrain import SessionTerrains, layer_textures
from .unity_meshes import SessionMeshes, make_mesh_object

__all__ = [
    "BLEND_DISABLED_REASON",
    "COLLADA_UNAVAILABLE_REASON",
    "LAST_REPORT",
    "SUPPORTED_MODEL_EXTS",
    "ImportOptions",
    "ModelSummary",
    "PreparedPackage",
    "build_shader_table",
    "cache_root",
    "operator_available",
    "prepare_package",
    "resolve_extract_root",
    "run_import",
    "vrm_addon_available",
]

_ROOT_PACKAGE = __package__.rsplit(".", 1)[0]  # bl_ext.<repo>.unitypackage_loader


# 直近のインポート結果（N パネル表示用）
LAST_REPORT: ImportReport | None = None


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
# 入れ子のパッケージ（#217）
# ---------------------------------------------------------------------------

def extract_nested_package(pkg: UnityPackage, entry: AssetEntry, opts: ImportOptions, package_path: Path) -> Path:
    """入れ子のパッケージを外側の展開先に書き出し、そのパスを返す（同じものが書き出してあれば使い回す）。"""
    root = resolve_extract_root(opts, package_path)
    paths = pkg.extract([entry.guid], root, overwrite=opts.overwrite_extracted, max_total_size=opts.max_extract_size)
    if entry.guid not in paths:
        raise PackageError(f"could not extract {entry.pathname}")
    opts.package_label = f"{package_path.stem} ({Path(entry.pathname).stem})"
    inner = Path(paths[entry.guid])
    # 外側にもアセットがあれば、中身と合わせたパッケージにする（中身の prefab が外側のメッシュを使うことがある）
    merged = merge_nested_package(pkg, inner, inner.with_name(f"{inner.stem} + outer.unitypackage"))
    return merged or inner


# ---------------------------------------------------------------------------
# 読み込むものの選択
# ---------------------------------------------------------------------------


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
    analysis: PrefabAnalysis = field(default_factory=PrefabAnalysis)  # prefab の解析結果（Models 単位なら空）


def _plan_groups(prepared: PreparedPackage, opts: ImportOptions) -> list[_ImportGroup]:
    if opts.unit != UNIT_PREFABS:
        return [_ImportGroup(None, [(m, prepared.table_for(m.guid)) for m in _select_models(prepared, opts)])]
    supported = {m.guid: m for m in prepared.supported_models}
    groups = []
    for prefab in _select_prefabs(prepared, opts):
        analysis = prepared.prefab_analyses.get(prefab.pathname, PrefabAnalysis())
        tables = analysis.tables
        models = [(supported[g], tables[g]) for g in prefab.model_guids if g in supported and g in tables]
        if models or analysis.meshes:
            groups.append(_ImportGroup(prefab, models, analysis))
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
    before = snapshot()
    try:
        _ImportSession(context, filepath, opts, progress, prepared, report).run()
        if report.errors and not (report.objects or report.lights or report.cameras):
            raise _NothingImported(f"nothing could be imported: {report.errors[0]}")
    except Exception as exc:
        if not isinstance(exc, _NothingImported):
            report.errors.append(f"import failed: {str(exc).strip()}")
        remove_created(before)
        for items in (report.objects, report.materials, report.images):
            items.clear()  # 片付けたので、Blender には残っていない
        LAST_REPORT = report
        raise
    LAST_REPORT = report
    return report


class _ImportSession:
    """1 回のインポートの状態と手順。

    以前は ``run_import`` から呼ぶ関数の中の入れ子の関数（``build_model`` / ``import_model`` / ``import_scene``）が、外側の変数を
    20 個取り込んでいた。読み込みの間に共有する状態（展開したファイル、読み込んだ画像、組み立てたマテリアル、
    作業用のコレクションなど）をフィールドにし、手順をメソッドにする（#78）。フィールドは ``__init__`` で宣言し、
    パッケージの解析や展開の結果は ``run`` の中で入れる（#198）。
    """

    def __init__(self, context, filepath, opts, progress, prepared, report):
        self.context = context
        self.filepath = filepath
        self.package_path = Path(filepath)
        self.opts = opts
        self.progress = progress
        self.prepared: PreparedPackage | None = prepared
        self.report: ImportReport = report
        # 以下は run の中で、パッケージを解析した後に入れる
        self.pkg: UnityPackage | None = None
        self.materials: SessionMaterials | None = None  # この回で組むマテリアルと、読み込んだ画像
        self.supported_models: dict[str, ModelSummary] = {}
        self.model_by_guid: dict[str, ModelSummary] = {}
        self.extract_root: Path | None = None
        self.paths: dict[str, Path] = {}  # 展開したファイルの GUID → パス
        self.scene = context.scene
        self.collection: bpy.types.Collection | None = None  # パッケージ用のコレクション
        self.staging: bpy.types.Collection | None = None  # 読み込み中だけ使う作業用のコレクション
        self.hidden_objects: list[bpy.types.Object] = []  # 読み込みの後で hide_set するもの
        self.imported_models: set[str] = set()  # この回で読み込み済みのモデル（prefab ごとに同じモデルを読み直すことがある）
        self.node_names: dict[str, Counter | None] = {}  # モデルの GUID → FBX のノードの名前と数（読めなければ None。#31）
        # 直前の import_model で、中のオブジェクトの fileID を計算したもの（表示の上書きと削除を当てる。#172）
        self.last_matcher: PartMatcher | None = None
        self.failed_models: set[str] = set()  # 読み込みに失敗したモデル（同じモデルを何度も試さない）
        self.primitive_meshes: dict[str, bpy.types.Mesh] = {}  # 組み込みメッシュの種類 → この回で作ったメッシュ（#190）
        self.decal_meshes: dict[tuple, bpy.types.Mesh] = {}  # デカールの形 → この回で作った板（#165）
        self.prefab_collections: list[bpy.types.Collection] = []  # 読み込む単位 Prefabs で prefab ごとに作ったコレクション
        self._scene_frames: dict[str, tuple[dict, dict]] = {}  # シーンの GUID → (ワールド行列, アクティブか)。scene_frames
        self.meshes = SessionMeshes(self)  # パッケージの Unity の Mesh から作ったメッシュ（#216）
        self.terrains = SessionTerrains(self)  # TerrainData ごとに作った Terrain のメッシュとマテリアル（#205）
        self.terrain_written: set[str] = set()  # この回で展開し直した TerrainData（アルファマップの PNG も書き直す）

    def run(self) -> None:
        self.step(0.0, "Scanning package")
        self.analyze()
        groups = [] if self.opts.unit == UNIT_SCENES else _plan_groups(self.prepared, self.opts)
        scenes = _select_scenes(self.prepared, self.opts) if self.opts.unit == UNIT_SCENES else []
        models = self.models_to_import(groups, scenes)
        needed_tex, written = self.extract(groups, scenes, models)
        self.step(0.5, "Loading textures")
        self.materials.load_textures(needed_tex, self.paths, written)

        self.collection = bpy.data.collections.new(self.opts.package_label or self.package_path.stem)
        self.scene.collection.children.link(self.collection)
        self.staging = bpy.data.collections.new(f"{self.package_path.stem} (importing)")
        self.scene.collection.children.link(self.staging)
        total = sum(len(group.models) for group in groups) + len(scenes)
        done = 0
        with self.collections_out_of_view_layer():
            for group in groups:
                self.import_group(group, done, total)
                done += len(group.models)
            for scene_summary in scenes:
                try:
                    self.import_scene(scene_summary, 0.55 + 0.4 * done / max(total, 1), 0.4 / max(total, 1))
                except Exception as exc:  # noqa: BLE001 - そのシーンの残りだけを外して続ける（#70）
                    record_failure(self.report, f"could not import scene {scene_summary.pathname}: {exc}")
                done += 1
        self.fill_missed_textures()
        self.materials.warn_slotless()
        for obj in self.hidden_objects:
            if not is_removed(obj):
                obj.hide_set(True)
        if self.opts.arrange == "SIDE_BY_SIDE" and len(self.prefab_collections) > 1:
            arrange_collections(self.context, self.prefab_collections)

        self.collection["unity_package"] = self.package_path.name
        self.step(1.0, "Done")

    def analyze(self) -> None:
        """パッケージを解析し（ダイアログで解析済みなら使う）、解析中の警告と読み込めないモデルをレポートに写す。"""
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
        self.materials = SessionMaterials(self.pkg, self.prepared.unity_mats, self.prepared.normalized, self.opts, self.report)
        self.supported_models = {m.guid: m for m in self.prepared.supported_models}
        self.model_by_guid = {m.guid: m for m in self.prepared.models}

    def models_to_import(self, groups: list[_ImportGroup], scenes: list[SceneSummary]) -> list[ModelSummary]:
        """読み込む prefab・シーンが使うモデル。1 つも無ければ、読み込めない理由を添えて PackageError を送る。"""
        selected: dict[str, ModelSummary] = {summary.guid: summary for group in groups for summary, _ in group.models}
        for scene_summary in scenes:
            for placement in scene_summary.placements:
                if placement.model_guid in self.supported_models:
                    selected.setdefault(placement.model_guid, self.supported_models[placement.model_guid])
        has_meshes = any(p.mesh_guid for s in scenes if s.contents is not None for p in s.contents.primitives) or any(
            group.analysis.meshes for group in groups
        )
        if selected or has_meshes or any(self.scene_terrain_data(scenes)):
            return list(selected.values())
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

    def extract(
        self, groups: list[_ImportGroup], scenes: list[SceneSummary], models: list[ModelSummary]
    ) -> tuple[set[str], set[str]]:
        """モデルと付随ファイル、必要なテクスチャを展開する。(読み込むテクスチャの GUID, 実際に書き出したファイルの GUID) を返す。

        テクスチャは、読み込むモデル・prefab・シーンで割り当てる .mat のものだけにする（#164）。予測から漏れたものは、
        組み立てで足りなかったところで覚えておき、最後にまとめて展開して組み直す（``fill_missed_textures``）。
        """
        needed_tex = material_textures(self.expected_materials(groups, scenes), self.prepared.normalized)
        if self.opts.import_unreferenced:
            needed_tex.update(e.guid for e in self.pkg.textures())
        missing = {g for g in needed_tex if self.pkg.get(g) is None}
        for g in sorted(missing):
            self.report.warn(f"texture {g} is referenced but not included in the package")
        needed_tex -= missing

        self.extract_root = resolve_extract_root(self.opts, self.package_path)
        self.report.extract_root = str(self.extract_root)
        self.step(0.2, "Extracting files")
        sidecars = [g for m in models for g in sidecar_guids(self.pkg, m.entry)]
        terrain_data = self.scene_terrain_data(scenes)
        if terrain_data:
            # レイヤーのテクスチャは TerrainData を読むまで分からないので、パッケージの .terrainlayer のものを先に展開する
            needed_tex.update(g for g in layer_textures(self.pkg) if self.pkg.get(g) is not None)
        written: set[str] = set()  # 実際に書き出した（展開し直した）ファイル
        self.paths = self.pkg.extract(
            [m.guid for m in models] + sidecars + sorted(terrain_data) + sorted(needed_tex),
            self.extract_root,
            overwrite=self.opts.overwrite_extracted,
            progress=lambda f, n: self.step(0.2 + 0.3 * f, f"Extracting {n}"),
            max_total_size=self.opts.max_extract_size,
            written=written,
        )
        self.terrain_written = written & terrain_data
        return needed_tex, written

    def scene_terrain_data(self, scenes: list[SceneSummary]) -> set[str]:
        """読み込むシーンの Terrain が使う、パッケージにある TerrainData の GUID（Terrain を読み込まない設定なら空）。"""
        if self.opts.terrain_points is None:
            return set()
        return {
            g for s in scenes if s.contents is not None for g in s.contents.terrain_data if self.pkg.get(g) is not None
        }

    @contextmanager
    def collections_out_of_view_layer(self):
        """読み込みの間、パッケージのコレクションをビューレイヤーから外し、作業用のコレクションをアクティブにする（#75）。

        インポーターのオペレーターは呼ぶたびにビューレイヤーの中身を評価し直すので、読み込み済みのものが増えるほど 1 回が重くなる
        （Japanese Street の Day_Showcase では 1 回 70 ms。3000 オブジェクトで 180 ms、ビューレイヤーから外すと 5 ms）。
        モデルは空の作業用コレクションに読み込んでから移す。終わったら（失敗しても）元に戻し、作業用のコレクションを消す。
        """
        view_layer = self.context.view_layer
        prev_active = view_layer.active_layer_collection
        package_layer = find_layer_collection(view_layer.layer_collection, self.collection)
        staging_layer = find_layer_collection(view_layer.layer_collection, self.staging)
        if staging_layer is not None:
            view_layer.active_layer_collection = staging_layer
        if package_layer is not None:
            package_layer.exclude = True
        try:
            yield
        finally:
            if package_layer is not None:
                package_layer.exclude = False
            bpy.data.collections.remove(self.staging)
            if prev_active is not None:
                view_layer.active_layer_collection = prev_active

    def prefab_collection(self, prefab: PrefabSummary) -> bpy.types.Collection:
        """読み込む単位 Prefabs で、prefab 1 つ分のコレクションを作る。読み込めないモデルはここで警告する。"""
        target = bpy.data.collections.new(prefab.name)
        self.collection.children.link(target)
        target["unity_prefab"] = prefab.pathname
        target["unity_prefab_guid"] = prefab.guid
        self.prefab_collections.append(target)
        self.report.prefabs.append(prefab.pathname)
        for guid in prefab.model_guids:
            skipped = self.model_by_guid.get(guid)
            if skipped is not None and not skipped.supported:
                self.report.warn(f"prefab {prefab.pathname}: skipped {skipped.entry.pathname}: {skipped.skip_reason}")
        return target

    def import_group(self, group: _ImportGroup, done: int, total: int) -> None:
        """1 つのコレクションにまとめるモデルを読み込む（読み込む単位 Prefabs なら prefab 1 つ、Models ならすべてのモデル）。

        prefab では、prefab が使っていない部品・中から消した部品・表示されていない部品・遠景用の LOD を隠すか消す。
        Models 単位の表は prefab をまたいで統合したものなので、これは prefab 単位のときだけ行う。
        """
        prefab = group.prefab
        target = self.prefab_collection(prefab) if prefab is not None else self.collection
        stats = PlacementStats()
        for index, (summary, prefab_table) in enumerate(group.models):
            self.step(0.55 + 0.4 * (done + index) / max(total, 1), f"Importing {summary.entry.name}")
            if summary.guid in self.failed_models:
                continue
            parts = group.analysis.parts.get(summary.guid, {})
            try:
                objects = self.import_model(summary, prefab_table, target, with_parts=bool(parts))
            except Exception as exc:  # noqa: BLE001 - そのモデルだけを外して続ける（#70）
                record_failure(self.report, f"could not import {summary.entry.pathname}: {exc}")
                continue
            if prefab is not None:
                self.apply_prefab_visibility(summary, prefab_table, group.analysis, parts, objects, stats)
        if prefab is not None and group.analysis.meshes:
            self.place_prefab_meshes(group.analysis.meshes, target)
        if prefab is not None:
            for message in prefab_warnings(stats):
                self.report.warn(f"prefab {prefab.pathname}: {message}")

    def place_prefab_meshes(self, meshes, target: bpy.types.Collection) -> None:
        """prefab の中の Unity の Mesh を使う Renderer を、prefab のルートを原点にした位置に置く（#216）。"""
        self.meshes.prefetch({p.mesh_guid for p, _ in meshes})
        for primitive, world in meshes:
            mesh = self.meshes.mesh(primitive.mesh_guid, primitive.mesh_file_id)
            if mesh is None:
                continue
            materials = [self.materials.material_for([g]) for g in primitive.materials]
            obj = make_mesh_object(mesh.name, mesh, materials)
            target.objects.link(obj)
            obj.matrix_world = Matrix(unity_to_blender(world))
            self.report.objects.append(obj.name)
            if not primitive.visible:
                defer_hide(obj, self.hidden_objects)

    def apply_prefab_visibility(
        self, summary: ModelSummary, prefab_table: dict[str, RendererMaterials], analysis: PrefabAnalysis,
        parts: dict[str, PartState], objects: list[bpy.types.Object], stats: PlacementStats,
    ) -> None:
        """prefab に合わせて、読み込んだモデルの部品を隠すか消す。"""
        match = self.last_matcher.match(parts) if parts and self.last_matcher else PartMatch()
        stats.unmatched_parts += match.unmatched
        # prefab が使っていない部品と、prefab が中から消した部品（#172）は Unity に無い
        unused = set(match.removed)
        if summary.guid not in analysis.whole_models:
            unused |= unused_parts(prefab_table, [(o.name, o.type == "MESH") for o in objects])
        if self.opts.skip_unused_parts:
            objects, removed = self.remove_unused(objects, unused)
            stats.removed_unused += removed
        stats.hidden_unused += self.hide_named(objects, unused)
        self.hide_named(objects, match.hidden)  # 非アクティブ・無効にしたもの（Unity と同じく数えない）
        if summary.guid in analysis.inactive_models:  # prefab の中の非アクティブな GameObject の下にだけ置いたモデル（#177）
            self.hide_named(objects, {o.name for o in objects})
        stats.hidden_lods += self.hide_prefab_rows(objects, prefab_table)

    def expected_materials(self, groups: list[_ImportGroup], scenes: list[SceneSummary]) -> set[str]:
        """読み込む前に分かる、この回で組む .mat の GUID（#164）。

        モデル自身の割り当て（externalObjects と、走査で読んだ FBX のマテリアル名）と、prefab・シーンの表の割り当てと、
        シーンの組み込みメッシュのマテリアル。マテリアルを組まないモデル（VRM add-on に任せるもの、同梱 .blend の KEEP）は外す。
        """
        found: set[str] = set()
        own: dict[str, bool] = {}  # モデルの GUID → マテリアルを組むか（組むなら自身の割り当ては加え済み）

        def builds(summary: ModelSummary) -> bool:
            if summary.guid not in own:
                entry = summary.entry
                keep = delegate_to_vrm_addon(entry, self.opts) or (entry.ext == ".blend" and self.opts.blend_materials == "KEEP")
                own[summary.guid] = not keep
                if not keep:
                    found.update(expected_materials(entry.fbx_materials, summary.info, self.prepared.unity_mats, entry.pathname))
            return own[summary.guid]

        for group in groups:
            for summary, table in group.models:
                if builds(summary):
                    found.update(g for row in (table or {}).values() for g in row.materials if g)
            found.update(g for p, _ in group.analysis.meshes for g in p.materials if g)
        for scene_summary in scenes:
            for placement in scene_summary.placements:
                summary = self.supported_models.get(placement.model_guid)
                if summary is not None and builds(summary):
                    found.update(g for r in placement.renderers.values() for g in r.materials if g)
            if scene_summary.contents is not None:
                found.update(g for p in scene_summary.contents.primitives for g in p.materials if g)
            if self.opts.scene_decals:
                found.update(decal.material for _, decal in self.scene_decals(scene_summary))
        return found & self.prepared.unity_mats.keys()

    def scene_frames(self, scene_guid: str) -> tuple[dict, dict]:
        """シーンの全 Node のワールド行列と、祖先を含めてアクティブか。シーンごとに 1 度だけ求める。

        デカール（読み込む前の予測と配置）とライト・カメラで ``components`` を何度も呼ぶので、そのたびに求め直さないようにする。
        """
        if scene_guid not in self._scene_frames:
            hierarchy = self.prepared.scene_hierarchies[scene_guid]
            self._scene_frames[scene_guid] = (world_matrices(hierarchy), effective_active(hierarchy))
        return self._scene_frames[scene_guid]

    def scene_decals(self, scene_summary: SceneSummary) -> list[tuple[object, Decal]]:
        """シーンの DecalProjector（コンポーネントと、読めた中身）。マテリアルがパッケージに無いものも含める。"""
        hierarchy = self.prepared.scene_hierarchies.get(scene_summary.guid)
        if hierarchy is None:
            return []
        found = []
        worlds, active = self.scene_frames(scene_summary.guid)
        for component in scene_components(hierarchy, [CLASS_MONO_BEHAVIOUR], worlds, active):
            decal = decal_from_body(component.body)
            if decal is not None:
                found.append((component, decal))
        return found

    def fill_missed_textures(self) -> None:
        """組み立てで足りなかったテクスチャ（読み込む前の予測から漏れたもの）を展開して読み込み、そのマテリアルを組み直す（#164）。"""
        pending, missed = self.materials.take_pending()
        if not pending:
            return
        self.step(0.95, "Loading textures")
        written: set[str] = set()
        try:
            self.paths.update(
                self.pkg.extract(
                    missed, self.extract_root, overwrite=self.opts.overwrite_extracted,
                    max_total_size=self.opts.max_extract_size, written=written,
                )
            )
        except PackageError as exc:
            self.report.warn(f"could not extract textures: {exc}")
        self.materials.load_textures(set(missed), self.paths, written)
        self.materials.rebuild_pending(pending)

    def step(self, fraction: float, message: str) -> None:
        if self.progress is not None:
            self.progress(fraction, message)

    def build_model(
        self, summary: ModelSummary, prefab_table: dict[str, RendererMaterials], target, before, with_parts: bool = False
    ) -> list[bpy.types.Object]:
        """モデルを 1 回読み込んでマテリアルを組み、作られたオブジェクトを返す。

        名前を引けない Renderer の行がある表か、``with_parts`` なら、中のオブジェクトの fileID を計算して
        ``last_matcher`` に置く（#31 / #172）。
        """
        model = summary.entry
        self.imported_models.add(model.guid)
        # 同名マテリアルの再利用を使うときだけ一覧を作る（シーンでは何百回も読み込むので、毎回作ると重い）
        existing_materials = {m.name: m for m in bpy.data.materials} if self.opts.reuse_existing else {}
        delegated = import_model_file(self.paths[model.guid], model, self.opts, target, self.report)
        new = new_since(before)
        adopt_into_collection(new, self.scene, target)
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

        prefab_table = self.match_model_parts(model, prefab_table or {}, new["objects"], before, with_parts)
        self.materials.assign_materials(summary, prefab_table, new, delegated, existing_materials)
        if self.opts.outlines:
            added = outline_builder.apply_outlines(new["objects"], self.opts.outline_width_scale)
            if added:
                self.report.outlines += added
        return new["objects"]

    def match_model_parts(
        self, model: AssetEntry, prefab_table: dict[str, RendererMaterials], objects, before, with_parts: bool
    ) -> dict[str, RendererMaterials]:
        """名前を引けないモデルの中への上書きを、読み込んだオブジェクトに当てる（#31 / #172）。

        対象の fileID を、読み込んだオブジェクトの名前と親子関係から計算した fileID と突き合わせる。計算は ``last_matcher`` に置き、
        マテリアルの上書きの行（``pending_renderer``）はオブジェクトの名前の行に付け替えた表を返す。メッシュが 1 つだけなら
        それに当てる（#109）。
        """
        pending = has_pending_renderers(prefab_table)
        if not (pending or with_parts):
            return prefab_table
        if model.guid not in self.node_names:
            self.node_names[model.guid] = fbx_node_names(self.paths[model.guid])
        taken = {strip_numeric_suffix(name) for name in before["objects"]}
        self.last_matcher = PartMatcher(model_objects(objects), native=self.node_names[model.guid], taken=taken)
        if not pending:
            return prefab_table
        mesh_names = [o.name for o in objects if o.type == "MESH"]
        prefab_table, unread = resolve_model_renderers(prefab_table, self.last_matcher.renderer_ids, mesh_names)
        if unread:
            self.report.warn(
                f"{model.name}: {unread} material override(s) on objects inside the model could not be matched "
                "to an imported object (the object is not in the model, or its name is not unique); "
                "the model's own assignments are used for them"
            )
        return prefab_table

    def import_model(
        self, summary: ModelSummary, prefab_table: dict[str, RendererMaterials], target, with_parts: bool = False
    ) -> list[bpy.types.Object]:
        """モデルを 1 回読み込む。途中で失敗したら、そのモデルで作ったものとレポートの行を消してから例外を送る（#70）。

        読み込みは作業用のコレクションで行い、終わったら ``target`` へ移す（#75）。``with_parts`` なら、表示の上書きと削除を
        当てるための ``last_matcher`` も作る（#172）。
        """
        self.last_matcher = None
        before = snapshot()
        rows = (self.report.models, self.report.objects, self.report.materials, self.report.images)
        counts = [len(items) for items in rows]
        known = summary.guid in self.imported_models
        try:
            objects = self.build_model(summary, prefab_table, self.staging, before, with_parts)
            move_collection_contents(self.staging, target)
            return objects
        except Exception:
            remove_created(before)
            for items, count in zip(rows, counts):
                del items[count:]
            if not known:
                self.imported_models.discard(summary.guid)
            self.materials.forget_removed()
            self.failed_models.add(summary.guid)
            raise

    def hide_prefab_rows(self, objects: list[bpy.types.Object], prefab_table: dict[str, RendererMaterials]) -> int:
        """prefab で表示されていない Renderer（非アクティブな GameObject の下か、無効な Renderer。#177）と、LODGroup で
        遠景用だったメッシュ（#104）を隠し、遠景用として隠した数を返す（表示されていないものは Unity と同じく数えない）。

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
            if not rm.visible:
                defer_hide(obj, self.hidden_objects)
            elif rm.lod_level and self.opts.hide_lods:
                defer_hide(obj, self.hidden_objects)
                hidden += 1
        return hidden

    def remove_unused(self, objects: list[bpy.types.Object], unused: set[str]) -> tuple[list[bpy.types.Object], int]:
        """使われていない部品を消し、(残ったオブジェクト, 消した使われていないメッシュの数) を返す（#152）。

        子孫に残すものがある部品は消さずに残す（隠すのは呼ぶ側）。中身が無くなった Empty・アーマチュアも消す。
        """
        if not unused:
            return objects, 0
        removed = parts_to_remove([(o.name, o.parent.name if o.parent else None, o.type) for o in objects], unused)
        if not removed:
            return objects, 0
        self.report.objects[:] = [name for name in self.report.objects if name not in removed]
        return remove_parts(objects, removed), len(removed & unused)

    def hide_named(self, objects: list[bpy.types.Object], names: set[str]) -> int:
        """``names`` のオブジェクトを隠し、隠した数を返す（prefab が使っていない部品。#151 / #172）。

        使っていない部品を読み込まない設定でも、子孫に残すものがある部品は消さずに残るので、ここで隠す。
        """
        hidden = 0
        for obj in objects:
            if obj.name in names:
                defer_hide(obj, self.hidden_objects)
                hidden += 1
        return hidden

    def light_scale(self, scene_summary: SceneSummary, target: bpy.types.Collection) -> float:
        """HDRP のシーンのグローバルな Volume の露出から、そのシーンの HDRP のライトに掛ける倍率を決める（#130）。

        Blender のシーンの露出は変えない。シーンごとに決めるので、パッケージやパイプラインの違うシーンを混ぜても崩れない。
        決められなければ 1。
        """
        if not (self.opts.scene_exposure and self.prepared.pipeline == PIPELINE_HDRP):
            return 1.0
        pathname = scene_summary.pathname
        try:
            documents = parse_documents(self.pkg.read_text(scene_summary.guid))
            volumes = global_volumes(documents)
            profiles = self.pkg.read_assets(v.profile_guid for v in volumes)
            camera = next((d.body for d in documents if d.class_id == CLASS_CAMERA), None)
            result = None
            for volume in volumes:  # priority の高い順
                data = profiles.get(volume.profile_guid)
                settings = exposure_settings(parse_documents(data.decode("utf-8", "replace"))) if data else None
                result = scene_exposure(settings, camera) if settings else None
                if result is not None:
                    break
        except Exception as exc:  # noqa: BLE001 - 露出は補助的な設定なので、読めなければ倍率 1 で続ける（#69）
            warn_skipped(self.report.warn, f"scene {pathname}: could not read the HDRP exposure", exc)
            return 1.0
        if result is None:
            return 1.0
        target["unity_exposure_ev100"] = result.ev100
        target["unity_exposure_mode"] = result.mode
        target["unity_light_scale"] = result.light_scale
        if result.approximate:
            self.report.warn(
                f"scene {pathname}: HDRP {result.mode} exposure is approximated with its upper limit (EV100 {result.ev100:g})"
            )
        self.report.exposures.append(
            f"{scene_summary.name}: HDRP {result.mode} exposure EV100 {result.ev100:g}, lights ×{result.light_scale:.3g}"
        )
        return result.light_scale

    def import_scene(self, scene_summary: SceneSummary, progress_start: float, progress_span: float) -> None:
        """シーンのモデルを配置どおりに読み込む。同じモデル・同じ割り当ての配置は、メッシュを共有した複製にする。"""
        SceneImport(self, scene_summary).run(progress_start, progress_span)
