"""FBX 内マテリアル名 → パッケージ内 .mat の解決。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from .material import UnityMaterial
from .meta import ModelImporterInfo, strip_numeric_suffix

__all__ = ["MaterialResolution", "resolve_materials"]

Method = Literal["external", "name", "none"]


@dataclass
class MaterialResolution:
    fbx_name: str
    guid: str | None
    method: Method


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
) -> dict[str, MaterialResolution]:
    """各 FBX マテリアル名に対して .mat の GUID を決める。

    1. ModelImporter の externalObjects（完全一致 → 連番除去）
    2. .mat の m_Name / ファイル名との一致（複数あればモデルと同じフォルダに近いもの）
    3. 解決不能
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
        result[fbx_name] = MaterialResolution(fbx_name, None, "none")
    return result
