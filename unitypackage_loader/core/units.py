"""インポートダイアログで選ぶ「読み込む単位」（Scenes / Prefabs / Models）の候補づくり。bpy 非依存。

Scenes / Prefabs はパッケージ内のシーン・prefab をそのまま候補にする。推測で候補から外すことはせず、読み込めないもの
（パッケージ内のモデルを使っていない、使うモデルがすべて読み込めない形式）は理由を付けて残す。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .hierarchy import ModelPlacement, SceneContents
from .prefab import RendererMaterials

UNIT_SCENES = "SCENES"
UNIT_PREFABS = "PREFABS"
UNIT_MODELS = "MODELS"
UNITS = (UNIT_SCENES, UNIT_PREFABS, UNIT_MODELS)  # ダイアログのタブの並び
_DEFAULT_ORDER = (UNIT_PREFABS, UNIT_MODELS, UNIT_SCENES)  # 前回の単位が使えないときに既定にする順

NO_MESH_REASON = "no mesh in package"
NO_SCENE_MESH_REASON = "no meshes from this package"
UNREADABLE_SCENE_REASON = "could not read the scene"


@dataclass
class PrefabSummary:
    guid: str
    pathname: str
    model_guids: list[str] = field(default_factory=list)  # この prefab の Renderer が使うパッケージ内のモデル（モデルの並び順）
    material_guids: list[str] = field(default_factory=list)  # Renderer が割り当てる .mat（重複なし、出てきた順）
    supported: bool = True
    skip_reason: str = ""

    @property
    def name(self) -> str:
        """拡張子を除いたファイル名（コレクション名に使う）。"""
        base = self.pathname.rsplit("/", 1)[-1]
        return base.rsplit(".", 1)[0] if "." in base else base


def summarize_prefabs(
    prefabs: Iterable[tuple[str, str]],
    prefab_tables: Mapping[str, Mapping[str, Mapping[str, RendererMaterials]]],
    model_order: Sequence[str],
    unsupported_models: Mapping[str, str],
) -> list[PrefabSummary]:
    """prefab の (GUID, pathname) から候補を作る（pathname 順）。

    ``prefab_tables`` は pathname → モデル GUID → GameObject 名の表、``model_order`` はモデル GUID の並び、
    ``unsupported_models`` は読み込めないモデルの GUID → 理由。
    """
    result: list[PrefabSummary] = []
    for guid, pathname in sorted(prefabs, key=lambda p: p[1]):
        tables = prefab_tables.get(pathname, {})
        summary = PrefabSummary(guid, pathname, [g for g in model_order if g in tables])
        seen: set[str] = set()
        for model_guid in summary.model_guids:
            for rm in tables[model_guid].values():
                for mat in rm.materials:
                    if mat and mat not in seen:
                        seen.add(mat)
                        summary.material_guids.append(mat)
        if not summary.model_guids:
            summary.supported, summary.skip_reason = False, NO_MESH_REASON
        elif all(g in unsupported_models for g in summary.model_guids):
            summary.supported, summary.skip_reason = False, unsupported_models[summary.model_guids[0]]
        result.append(summary)
    return result


@dataclass
class SceneSummary:
    guid: str
    pathname: str
    contents: SceneContents | None = None  # 読めなかったシーンは None
    supported: bool = True
    skip_reason: str = ""

    @property
    def name(self) -> str:
        return PrefabSummary(self.guid, self.pathname).name

    @property
    def placements(self) -> list[ModelPlacement]:
        return self.contents.placements if self.contents is not None else []


def summarize_scene(
    guid: str, pathname: str, contents: SceneContents | None, unsupported_models: Mapping[str, str]
) -> SceneSummary:
    """シーンの候補を作る。``contents`` が None なら読めなかったシーンとして理由を付ける。"""
    summary = SceneSummary(guid, pathname, contents)
    if contents is None:
        summary.supported, summary.skip_reason = False, UNREADABLE_SCENE_REASON
    elif not contents.placements:
        summary.supported, summary.skip_reason = False, NO_SCENE_MESH_REASON
    elif all(g in unsupported_models for g in contents.model_guids):
        summary.supported, summary.skip_reason = False, unsupported_models[contents.model_guids[0]]
    return summary


def available_units(prefabs: Sequence[PrefabSummary], model_count: int, scenes: Sequence[SceneSummary] = ()) -> list[str]:
    """ダイアログに出す単位（タブの並び）。候補が 1 つも無い単位は出さない（読み込めない候補だけでも、あれば出す）。"""
    present = {UNIT_SCENES: bool(scenes), UNIT_PREFABS: bool(prefabs), UNIT_MODELS: model_count > 0}
    return [unit for unit in UNITS if present[unit]]


def choice_count(
    prefabs: Sequence[PrefabSummary], supported_model_count: int, scenes: Sequence[SceneSummary] = ()
) -> int:
    """読み込める候補の総数。1 以下ならダイアログを出さずに読み込む。"""
    return sum(1 for p in prefabs if p.supported) + supported_model_count + sum(1 for s in scenes if s.supported)


def default_unit(
    prefabs: Sequence[PrefabSummary],
    supported_model_count: int,
    last_unit: str = "",
    scenes: Sequence[SceneSummary] = (),
) -> str:
    """ダイアログを開いたときの単位。前回の単位に読み込める候補があればそれ、無ければ Prefabs → Models → Scenes の順。"""
    readable = {
        UNIT_SCENES: any(s.supported for s in scenes),
        UNIT_PREFABS: any(p.supported for p in prefabs),
        UNIT_MODELS: supported_model_count > 0,
    }
    if readable.get(last_unit):
        return last_unit
    for unit in _DEFAULT_ORDER:
        if readable[unit]:
            return unit
    present = available_units(prefabs, supported_model_count, scenes)
    return present[0] if present else UNIT_MODELS
