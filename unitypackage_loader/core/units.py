"""インポートダイアログで選ぶ「読み込む単位」（Prefabs / Models）の候補づくり。bpy 非依存。

Prefabs はパッケージ内の prefab をそのまま候補にする。推測で候補から外すことはせず、読み込めない prefab
（パッケージ内のモデルを使う Renderer が無い、使うモデルがすべて読み込めない形式）は理由を付けて残す。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .prefab import RendererMaterials

UNIT_PREFABS = "PREFABS"
UNIT_MODELS = "MODELS"
UNITS = (UNIT_PREFABS, UNIT_MODELS)

NO_MESH_REASON = "no mesh in package"


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


def available_units(prefabs: Sequence[PrefabSummary], model_count: int) -> list[str]:
    """ダイアログに出す単位。候補が 1 つも無い単位は出さない（読み込めない候補だけでも、あれば出す）。"""
    units = []
    if prefabs:
        units.append(UNIT_PREFABS)
    if model_count:
        units.append(UNIT_MODELS)
    return units


def choice_count(prefabs: Sequence[PrefabSummary], supported_model_count: int) -> int:
    """読み込める候補の総数。1 以下ならダイアログを出さずに読み込む。"""
    return sum(1 for p in prefabs if p.supported) + supported_model_count


def default_unit(prefabs: Sequence[PrefabSummary], supported_model_count: int, last_unit: str = "") -> str:
    """ダイアログを開いたときの単位。前回の単位に読み込める候補があればそれ、無ければ Prefabs → Models の順。"""
    readable = {UNIT_PREFABS: any(p.supported for p in prefabs), UNIT_MODELS: supported_model_count > 0}
    if readable.get(last_unit):
        return last_unit
    for unit in UNITS:
        if readable[unit]:
            return unit
    return UNIT_PREFABS if prefabs else UNIT_MODELS
