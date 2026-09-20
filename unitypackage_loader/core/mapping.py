"""FBX 内マテリアル名 → パッケージ内 .mat の解決。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from .material import UnityMaterial
from .meta import ModelImporterInfo, strip_numeric_suffix
from .prefab import RendererMaterials
from .unity_ids import mesh_file_id

__all__ = ["MaterialResolution", "resolve_materials", "slot_assignments", "submesh_slot_order"]

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
    submesh_order: dict[str, list[int]] | None = None,
) -> dict[str, MaterialResolution]:
    """各 FBX マテリアル名に対して .mat の GUID を決める。

    1. ModelImporter の externalObjects（完全一致 → 連番除去）
    2. .mat の m_Name / ファイル名との一致（複数あればモデルと同じフォルダに近いもの）
    3. prefab の Renderer.m_Materials（``object_slots`` = Blender 上の {オブジェクト名: スロット順の
       マテリアル名} を使い、同名 GameObject の同じサブメッシュに入っている .mat を採用。
       サブメッシュとスロットの対応は ``submesh_order``、無ければスロット順）
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
        prefab_guid, warning = _from_prefab(fbx_name, prefab_table, object_slots, materials, submesh_order)
        if prefab_guid:
            result[fbx_name] = MaterialResolution(fbx_name, prefab_guid, "prefab", warning)
            continue
        result[fbx_name] = MaterialResolution(fbx_name, None, "none")
    return result


def submesh_slot_order(material_indices: Iterable[int], slot_count: int) -> list[int]:
    """Unity のサブメッシュ順に並べた Blender のスロット番号。

    Unity の FBX インポーターはポリゴン列で最初に使われた順にサブメッシュを作り、ポリゴンの無いマテリアルは
    サブメッシュにしない。Blender のスロット順（FBX 内のマテリアル順）とは一致しないことがあるので、
    Renderer.m_Materials[i] はこの並びの i 番目のスロットに対応させる。
    """
    order: list[int] = []
    seen: set[int] = set()
    for index in material_indices:
        if index in seen or not 0 <= index < slot_count:
            continue
        seen.add(index)
        order.append(index)
        if len(order) == slot_count:
            break
    return order


def _prefab_slots(
    obj_name: str,
    slot_count: int,
    prefab_table: dict[str, RendererMaterials],
    submesh_order: dict[str, list[int]] | None,
) -> list[tuple[int, str | None]]:
    """オブジェクトの (Blender スロット番号, prefab がそのサブメッシュに指す .mat GUID) の組。

    表は GameObject 名で引く（完全一致 → 連番を外した形）。prefab で GameObject の名前を変えてあって引けなければ、
    オブジェクト名から求めたメッシュの fileID を、表の Renderer のメッシュ参照と照合する（Scenes 単位と同じ。#71）。

    モデルがマテリアルを 1 つも持たないとスロットは 0 個になるが、Unity ではサブメッシュが 1 つあって Renderer の
    ``m_Materials[0]`` で描かれる。この場合は先頭の .mat をスロット 0 の組として返す（スロットは読み込む側で作る。#102）。
    """
    rm = prefab_table.get(obj_name) or prefab_table.get(strip_numeric_suffix(obj_name))
    if rm is None:
        ids = {mesh_file_id(obj_name), mesh_file_id(strip_numeric_suffix(obj_name))}
        rm = next((r for r in prefab_table.values() if r.mesh_file_id and r.mesh_file_id in ids), None)
    if rm is None:
        return []
    if slot_count <= 0:
        return [(0, rm.materials[0])] if rm.materials else []
    order = submesh_order.get(obj_name) if submesh_order is not None else None
    if order is None:
        order = list(range(slot_count))
    return [(slot, guid) for slot, guid in zip(order, rm.materials) if 0 <= slot < slot_count]


def _from_prefab(
    fbx_name: str,
    prefab_table: dict[str, RendererMaterials] | None,
    object_slots: dict[str, list[str]] | None,
    materials: dict[str, UnityMaterial],
    submesh_order: dict[str, list[int]] | None = None,
) -> tuple[str | None, str | None]:
    if not prefab_table or not object_slots:
        return None, None
    votes: dict[str, int] = {}
    for obj_name, slots in object_slots.items():
        for index, guid in _prefab_slots(obj_name, len(slots), prefab_table, submesh_order):
            # スロットの無いメッシュはスロット 0 の組が返るので、名前と突き合わせる相手がいない
            if index >= len(slots) or slots[index] != fbx_name:
                continue
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


def fully_replaced_materials(
    object_slots: dict[str, list[str]],
    assignments: dict[tuple[str, int], str],
    resolution: dict[str, "MaterialResolution"],
) -> set[str]:
    """prefab の割り当てで、使われているすべてのスロットが別の .mat に差し替わるマテリアルの名前。

    こうしたマテリアルは組み立てても差し替えで捨てるだけなので、組み立てを省ける（#77）。
    どのメッシュのスロットにも使われていないマテリアルは含めない。
    """
    all_replaced: dict[str, bool] = {}
    for obj_name, slots in object_slots.items():
        for index, name in enumerate(slots):
            if not name:
                continue
            target = assignments.get((obj_name, index))
            current = resolution.get(name)
            replaced = target is not None and (current is None or current.guid != target)
            all_replaced[name] = all_replaced.get(name, True) and replaced
    return {name for name, replaced in all_replaced.items() if replaced}


def slot_assignments(
    object_slots: dict[str, list[str]],
    prefab_table: dict[str, RendererMaterials] | None,
    materials: dict[str, UnityMaterial],
    submesh_order: dict[str, list[int]] | None = None,
) -> dict[tuple[str, int], str]:
    """prefab が (オブジェクト名, スロット番号) ごとに指す .mat GUID。パッケージ内に無い GUID は除く。

    m_Materials の並びは Unity のサブメッシュ順なので、``submesh_order``（{オブジェクト名: サブメッシュ順の
    スロット番号}、``submesh_slot_order`` の結果）でスロットに読み替える。無いオブジェクトはスロット順とみなす。

    スロットが 0 個のメッシュ（マテリアルを持たないモデル）には、まだ無いスロット 0 への割り当てが入る（#102）。
    """
    result: dict[tuple[str, int], str] = {}
    if not prefab_table:
        return result
    for obj_name, slots in object_slots.items():
        for index, guid in _prefab_slots(obj_name, len(slots), prefab_table, submesh_order):
            if guid and guid in materials:
                result[(obj_name, index)] = guid
    return result
