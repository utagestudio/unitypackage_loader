"""シーンの読み込みで使う、bpy に依存しない判定と文言。blender/importer.py から使う。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from .hierarchy import Hierarchy, ModelPlacement, SceneContents, built_from_renderers
from .meta import strip_numeric_suffix
from .unity_ids import mesh_file_id

__all__ = [
    "HiddenParts",
    "lod_warning",
    "parts_to_hide",
    "parts_to_remove",
    "prefab_warnings",
    "removed_warning",
    "scene_warnings",
    "unused_in_placement",
    "unused_warning",
]


class HiddenParts(NamedTuple):
    """配置で読み込んだオブジェクトのうち、隠すものと、その理由の内訳。"""

    names: set[str]  # 隠すオブジェクトの名前
    unused: int  # そのうち「Unity で使われていない部品」として隠した数（#58）
    lods: dict[str, int]  # LODGroup の段が分かったオブジェクトの名前 → 段（隠さない LOD0 も含む。#104）
    hidden_lods: int = 0  # そのうち遠景用 LOD として隠した数


def unused_in_placement(h: Hierarchy, placement: ModelPlacement, objects: Iterable[tuple[str, bool]]) -> set[str]:
    """配置で読み込んだメッシュのうち、Unity で使われていないもの（#58）。``objects`` は (Blender のオブジェクト名, メッシュか)。

    展開した prefab・シーンの Renderer から作った配置では、Unity にあるのは表の Renderer だけなので、FBX にしかない
    メッシュ（prefab が使っていない LOD や別のノード）は使われていない。FBX 由来の名前に「.002」が付いていることもあるので、
    表の名前は連番を外した形でも照合し、名前で引けなければ Renderer のメッシュの fileID をオブジェクト名のハッシュと照合する
    （表で名前を引けない新しい形式のモデル）。FBX を丸ごと置いた配置では空。
    """
    if not built_from_renderers(h, placement):
        return set()
    used = set(placement.renderers) | {strip_numeric_suffix(n) for n in placement.renderers}
    mesh_ids = {r.mesh_file_id for r in placement.renderers.values() if r.mesh_file_id}
    result: set[str] = set()
    for obj_name, is_mesh in objects:
        name = strip_numeric_suffix(obj_name)
        if (
            is_mesh and obj_name not in used and name not in used
            and mesh_file_id(obj_name) not in mesh_ids and mesh_file_id(name) not in mesh_ids
        ):
            result.add(obj_name)
    return result


def parts_to_remove(objects: Iterable[tuple[str, str | None, str]], unused: set[str]) -> set[str]:
    """使われていない部品を読み込まないとき（#152）に、消すオブジェクトの名前。

    ``objects`` は (Blender のオブジェクト名, 親の名前, オブジェクトの種類)。``unused`` は使われていないメッシュ。

    - 使われていないメッシュは、子孫に残すものが無ければ消す。残すものがあれば、親子関係と位置を保つため残す
      （隠すのは今までどおり ``parts_to_hide`` が行う）
    - Empty とアーマチュアは、子がすべて消えたら消す（使われていない部品の入れ物だったもの）。もともと子の無いものは残す
    """
    objects = list(objects)
    kinds = {name: kind for name, _, kind in objects}
    children: dict[str, list[str]] = {}
    for name, parent, _ in objects:
        if parent in kinds:
            children.setdefault(parent, []).append(name)
    removed: set[str] = set()

    def visit(name: str) -> bool:
        kids = children.get(name, [])
        all_gone = all([visit(kid) for kid in kids])  # 子はすべてたどる（短絡させない）
        gone = all_gone and (name in unused or (bool(kids) and kinds[name] in ("EMPTY", "ARMATURE")))
        if gone:
            removed.add(name)
        return gone

    for name, parent, _ in objects:
        if parent not in kinds:
            visit(name)
    return removed


def parts_to_hide(
    h: Hierarchy, placement: ModelPlacement, objects: Iterable[tuple[str, bool]], hide_lods: bool = True
) -> HiddenParts:
    """配置で読み込んだオブジェクトのうち隠すものと、LODGroup の段。

    ``objects`` は (Blender のオブジェクト名, メッシュか)。

    - 配置が非アクティブならすべて、Renderer が無効ならそのオブジェクトを隠す
    - Unity で使われていないメッシュ（``unused_in_placement``）も隠す（#58）
    - ``hide_lods`` なら、LODGroup の遠景用の段（LOD1 以降）の Renderer を隠す（#104）
    """
    objects = list(objects)
    unused_names = unused_in_placement(h, placement, objects)
    disabled = {name for name, r in placement.renderers.items() if not r.visible}
    levels = {name: r.lod_level for name, r in placement.renderers.items()}
    by_mesh_id = {r.mesh_file_id: r.lod_level for r in placement.renderers.values() if r.mesh_file_id}
    hide: set[str] = set()
    lods: dict[str, int] = {}
    unused_count = lod_count = 0
    for obj_name, is_mesh in objects:
        name = strip_numeric_suffix(obj_name)
        unused = obj_name in unused_names
        level = levels.get(obj_name, levels.get(name))
        if level is None and is_mesh:
            level = by_mesh_id.get(mesh_file_id(obj_name), by_mesh_id.get(mesh_file_id(name)))
        if level is not None:
            lods[obj_name] = level
        lod_hidden = bool(hide_lods and level)
        if not placement.active or name in disabled or unused or lod_hidden:
            hide.add(obj_name)
        # 数えるのは、その理由だけで隠れたもの（非アクティブな配置や無効な Renderer は、そちらの扱い）
        if placement.active and name not in disabled:
            unused_count += int(unused)
            lod_count += int(lod_hidden and not unused)
    return HiddenParts(hide, unused_count, lods, lod_count)


def scene_warnings(
    pathname: str,
    contents: SceneContents | None,
    *,
    baked_lights: int = 0,
    light_notes: Iterable[str] = (),
    hidden_unused: int = 0,
    removed_unused: int = 0,
    hidden_lods: int = 0,
    skipped_nodes: int = 0,
    skipped_offsets: int = 0,
) -> list[str]:
    """シーン 1 つを読み込んだ後にレポートへ出す警告（読めなかったシーンの ``contents`` は None）。"""
    prefix = f"scene {pathname}: "
    messages: list[str] = []
    if baked_lights:
        messages.append(
            f"{baked_lights} light(s) only affect lightmaps in Unity (baked or area lights); "
            "they were imported as real-time lights"
        )
    messages.extend(sorted(light_notes))
    if contents is not None:
        if contents.unresolved_overrides:
            messages.append(
                f"{contents.unresolved_overrides} override(s) on objects inside a model "
                "(position, material or visibility) are not read yet"
            )
        if contents.missing_sources:
            messages.append(f"{contents.missing_sources} prefab instance(s) refer to assets that are not in the package")
        if contents.other_renderers:
            messages.append(
                f"{contents.other_renderers} renderer(s) use meshes outside the package "
                "(such as Unity's built-in primitives) and were skipped"
            )
        if removed_unused:
            messages.append(removed_warning(removed_unused))
        if hidden_unused:
            messages.append(unused_warning(hidden_unused))
        if hidden_lods:
            messages.append(lod_warning(hidden_lods))
        if skipped_nodes:
            messages.append(
                f"{skipped_nodes} position override(s) on nested or armature-deformed parts inside a model "
                "were not applied"
            )
        if skipped_offsets:
            messages.append(
                f"{skipped_offsets} moved part(s) of armature-deformed objects were left at the model's position"
            )
    return [prefix + m for m in messages]


def prefab_warnings(*, hidden_unused: int = 0, removed_unused: int = 0, hidden_lods: int = 0) -> list[str]:
    """prefab 1 つを読み込んだ後にレポートへ出す警告（呼ぶ側が「prefab <pathname>: 」を付ける）。"""
    messages: list[str] = []
    if removed_unused:
        messages.append(removed_warning(removed_unused))
    if hidden_unused:
        messages.append(unused_warning(hidden_unused))
    if hidden_lods:
        messages.append(lod_warning(hidden_lods))
    return messages


def unused_warning(hidden_unused: int) -> str:
    """使われていない FBX の部品を隠したときの文言（Scenes / Prefabs のどちらからも使う。#58 / #151）。"""
    return (
        f"{hidden_unused} object(s) from model files are not used by the Unity prefabs or scene "
        "and were hidden (they stay in the file; unhide them if a renamed part was hidden by mistake)"
    )


def removed_warning(removed_unused: int) -> str:
    """使われていない FBX の部品を読み込まなかったときの文言（#152）。"""
    return (
        f"{removed_unused} object(s) from model files are not used by the Unity prefabs or scene "
        "and were not imported (turn off 'Skip Unused Parts' to import them hidden)"
    )


def lod_warning(hidden_lods: int) -> str:
    """遠景用 LOD を隠したときの文言（Scenes / Prefabs のどちらからも使う）。"""
    return (
        f"{hidden_lods} object(s) are lower LOD levels of a Unity LODGroup and were hidden "
        "(they stay in the file; their LOD level is in the 'unity_lod' custom property)"
    )
