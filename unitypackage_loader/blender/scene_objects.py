"""シーンの読み込みで、Blender のオブジェクトを作り・置くための関数。

Empty・ライト・カメラの作成、読み込んだモデルの複製、行列の補正、非表示、コレクションの移動と並べ方。
読み込みの流れは ``importer.py``、bpy に依存しない判定は ``core/scene_import.py``。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import bpy
from mathutils import Matrix, Vector

from ..core.arrange import arrange_offsets
from ..core.hierarchy import Hierarchy
from ..core.lights import BlenderCamera, BlenderLight
from ..core.meta import strip_numeric_suffix
from ..core.transform import BLENDER_TO_UNITY, UNITY_TO_BLENDER, trs, unity_to_blender
from ..core.unity_yaml import UnityRef


def find_layer_collection(layer_coll, target):
    if layer_coll.collection == target:
        return layer_coll
    for child in layer_coll.children:
        found = find_layer_collection(child, target)
        if found is not None:
            return found
    return None


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


def arrange_collections(context, collections: list[bpy.types.Collection]) -> None:
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


@dataclass
class SceneTemplate:
    """シーンで最初に読み込んだモデルのオブジェクトと、原点にあったときの状態（複製と位置の補正に使う）。"""

    objects: list[bpy.types.Object]
    basis: dict[bpy.types.Object, Matrix]
    hide_render: dict[bpy.types.Object, bool]

    @classmethod
    def capture(cls, objects: list[bpy.types.Object]) -> SceneTemplate:
        # matrix_world は depsgraph の評価が要るので持たない（数千回の読み込みでシーン全体を評価し直すと重い）
        return cls(list(objects), {o: o.matrix_basis.copy() for o in objects}, {o: o.hide_render for o in objects})


def scene_empty(
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
            defer_hide(empty, hidden)
        empties[node_key] = empty
        parent = empty
    return empties.get(key, parent)


def json_text(body: dict) -> str:
    """コンポーネントの中身をカスタムプロパティ用の JSON にする（参照は fileID / guid の辞書にする）。"""

    def default(value):
        if isinstance(value, UnityRef):
            return {"fileID": value.file_id, "guid": value.guid, "type": value.type}
        return str(value)

    return json.dumps(body, ensure_ascii=False, default=default)


def make_light(name: str, values: BlenderLight) -> bpy.types.Object:
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


def make_camera(name: str, values: BlenderCamera) -> bpy.types.Object:
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


def attach_to_empty(objects: list[bpy.types.Object], root, scale: float) -> None:
    """モデルの最上位のオブジェクトを配置の Empty の子にする。.meta の globalScale はモデルのルートで掛ける。"""
    if root is None:
        return
    members = set(objects)
    inverse = Matrix.Scale(scale, 4) if math.isfinite(scale) and scale > 0 and scale != 1 else Matrix.Identity(4)
    for obj in objects:
        if obj.parent is None or obj.parent not in members:
            obj.parent = root
            obj.matrix_parent_inverse = inverse


def duplicate_objects(template: SceneTemplate, collection) -> list[bpy.types.Object]:
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


def apply_node_transforms(template: SceneTemplate, objects, overrides, unit_scale: float | None) -> int:
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


def defer_hide(obj: bpy.types.Object, hidden: list) -> None:
    """レンダリングからはすぐ外し、ビューポートの非表示は ``hidden`` に積んで読み込みの後で当てる。

    読み込み中のコレクションはビューレイヤーから外していて、そこにあるオブジェクトには ``hide_set`` を使えない。
    """
    obj.hide_render = True
    hidden.append(obj)


def move_collection_contents(source: bpy.types.Collection, target: bpy.types.Collection) -> None:
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


def apply_offsets(objects, root_world, offsets, scale: float) -> int:
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
