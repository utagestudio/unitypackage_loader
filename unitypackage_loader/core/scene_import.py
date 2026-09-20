"""シーンの読み込みで使う、bpy に依存しない判定と文言。blender/importer.py から使う。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from .hierarchy import Hierarchy, ModelPlacement, SceneContents
from .meta import strip_numeric_suffix
from .unity_ids import mesh_file_id

__all__ = ["HiddenParts", "lod_warning", "parts_to_hide", "scene_warnings"]


class HiddenParts(NamedTuple):
    """配置で読み込んだオブジェクトのうち、隠すものと、その理由の内訳。"""

    names: set[str]  # 隠すオブジェクトの名前
    unused: int  # そのうち「Unity で使われていない部品」として隠した数（#58）
    lods: dict[str, int]  # LODGroup の段が分かったオブジェクトの名前 → 段（隠さない LOD0 も含む。#104）
    hidden_lods: int = 0  # そのうち遠景用 LOD として隠した数


def parts_to_hide(
    h: Hierarchy, placement: ModelPlacement, objects: Iterable[tuple[str, bool]], hide_lods: bool = True
) -> HiddenParts:
    """配置で読み込んだオブジェクトのうち隠すものと、LODGroup の段。

    ``objects`` は (Blender のオブジェクト名, メッシュか)。

    - 配置が非アクティブならすべて、Renderer が無効ならそのオブジェクトを隠す
    - 展開した prefab・シーンの Renderer から作った配置では、Unity にあるのは表の Renderer だけなので、FBX にしかない
      メッシュ（prefab が使っていない LOD や別のノード）も隠す（#58）。FBX 由来の名前に「.002」が付いていることもあるので、
      表の名前は連番を外した形でも照合し、名前で引けなければ Renderer のメッシュの fileID をオブジェクト名のハッシュと照合する
      （表で名前を引けない新しい形式のモデル）
    - ``hide_lods`` なら、LODGroup の遠景用の段（LOD1 以降）の Renderer を隠す（#104）
    """
    from_renderers = placement.root in h.nodes and h.nodes[placement.root].model_guid is None
    disabled = {name for name, r in placement.renderers.items() if not r.visible}
    used = set(placement.renderers) | {strip_numeric_suffix(n) for n in placement.renderers}
    mesh_ids = {r.mesh_file_id for r in placement.renderers.values() if r.mesh_file_id}
    levels = {name: r.lod_level for name, r in placement.renderers.items()}
    by_mesh_id = {r.mesh_file_id: r.lod_level for r in placement.renderers.values() if r.mesh_file_id}
    hide: set[str] = set()
    lods: dict[str, int] = {}
    unused_count = lod_count = 0
    for obj_name, is_mesh in objects:
        name = strip_numeric_suffix(obj_name)
        unused = (
            from_renderers and is_mesh and obj_name not in used and name not in used
            and mesh_file_id(obj_name) not in mesh_ids and mesh_file_id(name) not in mesh_ids
        )
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
        if hidden_unused:
            messages.append(
                f"{hidden_unused} object(s) from model files are not used by the Unity prefabs or scene "
                "and were hidden (they stay in the file; unhide them if a renamed part was hidden by mistake)"
            )
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


def lod_warning(hidden_lods: int) -> str:
    """遠景用 LOD を隠したときの文言（Scenes / Prefabs のどちらからも使う）。"""
    return (
        f"{hidden_lods} object(s) are lower LOD levels of a Unity LODGroup and were hidden "
        "(they stay in the file; their LOD level is in the 'unity_lod' custom property)"
    )
