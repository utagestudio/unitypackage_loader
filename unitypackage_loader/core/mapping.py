"""FBX 内マテリアル名 → パッケージ内 .mat の解決。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from .material import UnityMaterial
from .meta import ModelImporterInfo, strip_numeric_suffix
from .prefab import RendererMaterials

__all__ = ["MaterialResolution", "resolve_materials", "slot_assignments"]

Method = Literal["external", "name", "prefab", "none"]


@dataclass
class MaterialResolution:
    fbx_name: str
    guid: str | None
    method: Method
    warning: str | None = None


def _common_prefix_len(a: str, b: str) -> int:
    pa, pb = PurePosixPath(a).parts, PurePosixPath(b).parts
    n = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        n += 1
    return n


def resolve_materials(
    fbx_material_names: list[str],
    model_info: ModelImporterInfo | None,
    materials: dict[str, UnityMaterial],
    model_pathname: str = "",
    prefab_table: dict[str, RendererMaterials] | None = None,
    object_slots: dict[str, list[str]] | None = None,
) -> dict[str, MaterialResolution]:
    """各 FBX マテリアル名に対して .mat の GUID を決める。

    1. ModelImporter の externalObjects（完全一致 → 連番除去）
    2. .mat の m_Name / ファイル名との一致（複数あればモデルと同じフォルダに近いもの）
    3. prefab の Renderer.m_Materials（``object_slots`` = Blender 上の {オブジェクト名: スロット順の
       マテリアル名} を使い、同名 GameObject の同じスロットに入っている .mat を採用）
    4. 解決不能
    """
    by_name: dict[str, list[UnityMaterial]] = {}
    for mat in materials.values():
        keys = {mat.name, PurePosixPath(mat.pathname).stem} - {""}
        for key in keys:
            by_name.setdefault(key, []).append(mat)

    result: dict[str, MaterialResolution] = {}
    for fbx_name in fbx_material_names:
        guid = model_info.resolve_material(fbx_name) if model_info else None
        if guid and guid in materials:
            result[fbx_name] = MaterialResolution(fbx_name, guid, "external")
            continue
        base = strip_numeric_suffix(fbx_name)
        candidates = by_name.get(fbx_name) or by_name.get(base) or []
        if candidates:
            best = max(candidates, key=lambda m: _common_prefix_len(m.pathname, model_pathname))
            result[fbx_name] = MaterialResolution(fbx_name, best.guid, "name")
            continue
        prefab_guid, warning = _from_prefab(fbx_name, prefab_table, object_slots, materials)
        if prefab_guid:
            result[fbx_name] = MaterialResolution(fbx_name, prefab_guid, "prefab", warning)
            continue
        result[fbx_name] = MaterialResolution(fbx_name, None, "none")
    return result


def _from_prefab(
    fbx_name: str,
    prefab_table: dict[str, RendererMaterials] | None,
    object_slots: dict[str, list[str]] | None,
    materials: dict[str, UnityMaterial],
) -> tuple[str | None, str | None]:
    if not prefab_table or not object_slots:
        return None, None
    votes: dict[str, int] = {}
    for obj_name, slots in object_slots.items():
        rm = prefab_table.get(obj_name) or prefab_table.get(strip_numeric_suffix(obj_name))
        if rm is None:
            continue
        for index, slot_name in enumerate(slots):
            if slot_name != fbx_name or index >= len(rm.materials):
                continue
            guid = rm.materials[index]
            if guid and guid in materials:
                votes[guid] = votes.get(guid, 0) + 1
    if not votes:
        return None, None
    best = max(votes, key=votes.get)
    warning = None
    if len(votes) > 1:
        warning = (
            f"prefab assigns {len(votes)} different materials to slots sharing {fbx_name!r}; "
            "slots are split per prefab assignment"
        )
    return best, warning


def slot_assignments(
    object_slots: dict[str, list[str]],
    prefab_table: dict[str, RendererMaterials] | None,
    materials: dict[str, UnityMaterial],
) -> dict[tuple[str, int], str]:
    """prefab が (オブジェクト名, スロット番号) ごとに指す .mat GUID。パッケージ内に無い GUID は除く。"""
    result: dict[tuple[str, int], str] = {}
    if not prefab_table:
        return result
    for obj_name, slots in object_slots.items():
        rm = prefab_table.get(obj_name) or prefab_table.get(strip_numeric_suffix(obj_name))
        if rm is None:
            continue
        for index in range(min(len(slots), len(rm.materials))):
            guid = rm.materials[index]
            if guid and guid in materials:
                result[(obj_name, index)] = guid
    return result
