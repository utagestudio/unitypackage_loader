"""モーションだけのモデルのアーマチュアを、同じ骨格のものどうしで 1 体にまとめる（#276。Models 単位）。

モーション集のパッケージは、モーションごとに FBX が分かれていて、読み込むとアーマチュアがモーションの数だけ並ぶ。
メッシュが無く、アーマチュアが 1 体で、NLA のトラックを持つモデルを、骨格と置き場所が同じものどうしでまとめ、
全部のトラックを 1 体に載せる。スキンの無い FBX はファイルの既定の姿勢をレストにするので、レストの違いはキーを変換して吸収する。
"""

from __future__ import annotations

import re

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from .fbx_motion import TAKE_MODEL_PROP, TAKE_PROP
from .model_import import MODEL_GUID_PROP
from .nla import copy_track

# keyframe_points の interpolation に foreach_set で渡す LINEAR の値
_LINEAR = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value


# 骨格・配置が同じとみなす行列の差
_TOLERANCE = 1e-4


def _skeleton(armature: bpy.types.Object) -> tuple:
    """ボーンの名前と親の組（骨格が同じかの判定に使う）。"""
    return tuple(sorted((b.name, b.parent.name if b.parent else "") for b in armature.data.bones))


def _close(a, b, tolerance: float = _TOLERANCE) -> bool:
    return all(abs(x - y) <= tolerance for ra, rb in zip(a, b) for x, y in zip(ra, rb))


def _placement(armature: bpy.types.Object) -> Matrix:
    """アーマチュアを置いた場所（親のワールドの行列）。アーマチュア自身の Transform は、FBX のルートのボーンがオブジェクトになった
    もの（Everyday Motion Pack の Bip001）だとファイルの最初のフレームの値で、モーションごとに違うので比べない。"""
    return armature.parent.matrix_world.copy() if armature.parent else Matrix.Identity(4)


def _relative_rest(bone) -> Matrix:
    return bone.parent.matrix_local.inverted_safe() @ bone.matrix_local if bone.parent else bone.matrix_local.copy()


def motion_rigs(objects: list[bpy.types.Object]) -> dict[str, bpy.types.Object]:
    """モデルの GUID → モーションだけのモデルのアーマチュア（メッシュが無く、アーマチュアが 1 体で、NLA のトラックを持つもの）。"""
    by_model: dict[str, list[bpy.types.Object]] = {}
    for obj in objects:
        guid = obj.get(MODEL_GUID_PROP)
        if guid:
            by_model.setdefault(str(guid), []).append(obj)
    rigs = {}
    for guid, members in by_model.items():
        if any(o.type not in ("ARMATURE", "EMPTY") for o in members):
            continue
        armatures = [o for o in members if o.type == "ARMATURE"]
        if len(armatures) == 1 and armatures[0].animation_data is not None and armatures[0].animation_data.nla_tracks:
            rigs[guid] = armatures[0]
    return rigs


def merge_motion_rigs(objects: list[bpy.types.Object], model_order: list[str]) -> list[bpy.types.Object]:
    """同じ骨格のモーションだけのアーマチュアを 1 体にまとめ、消すべきオブジェクトを返す。

    骨格（ボーンの名前と親子）と、アーマチュアのワールドの行列が同じものをまとめる。骨格は、ボーンの多いほうに少ないほうが
    含まれていればよい（武器を持たせるボーンが無いモーションなど。無いボーンはレストのまま）。残すのはボーンがいちばん多く、
    その中で先に読んだモデルのアーマチュア。スキンの無い FBX は、ファイルの既定の姿勢を
    レストにするので、レスト姿勢はファイルごとに違う。そのため、残すアーマチュアのレストで同じ姿勢になるようにキーを変換した
    Action を作って、トラックを写す（``retarget_action``）。消すのは、まとめたモデルの全オブジェクト（アーマチュアと、その上の Empty）。
    """
    rigs = motion_rigs(objects)
    order = {guid: i for i, guid in enumerate(model_order)}
    groups: list[list[tuple[str, bpy.types.Object]]] = []
    ranked = sorted(rigs.items(), key=lambda item: (-len(item[1].data.bones), order.get(item[0], len(order))))
    for guid, armature in ranked:
        for group in groups:
            first = group[0][1]
            if set(_skeleton(armature)) <= set(_skeleton(first)) and _close(_placement(first), _placement(armature)):
                group.append((guid, armature))
                break
        else:
            groups.append([(guid, armature)])
    removed_models: set[str] = set()
    converted: dict[tuple[int, int], bpy.types.Action] = {}
    for members in groups:
        if len(members) < 2:
            continue
        keeper = members[0][1]
        for guid, armature in members[1:]:
            for track in armature.animation_data.nla_tracks:
                for strip in track.strips:
                    key = (strip.action.as_pointer(), keeper.as_pointer())
                    if key not in converted:
                        converted[key] = retarget_action(strip.action, strip.action_slot, armature, keeper)
                    copy_track(keeper.animation_data, track, strip, converted[key])
            removed_models.add(guid)
    return [o for o in objects if str(o.get(MODEL_GUID_PROP, "")) in removed_models]


_BONE_PATH = re.compile(r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]\.(location|rotation_quaternion|rotation_euler|scale)$')


def retarget_action(action: bpy.types.Action, slot, source: bpy.types.Object, target: bpy.types.Object) -> bpy.types.Action:
    """``source`` のレストで作られた ``action`` を、``target`` のレストで同じ姿勢になる Action にする。

    ボーンのローカルの行列 B（レストからの差分）は、親からの相対のレスト R と合わせて R·B が姿勢を決める。レストが R' のボーンで
    同じ姿勢にするには B' = R'⁻¹·R·B。R が R' と同じボーンのカーブはそのまま写し、違うボーンはキーの時刻ごとに求め直す
    （補間は LINEAR。FBX のモーションはフレームごとにキーがあるので、見た目は変わらない）。

    オブジェクトの Transform のカーブは絶対値なのでそのまま使う。動かさないチャンネルは ``source`` の値を一定のキーで足す
    （残すアーマチュアの値のままにしない）。
    """
    corrections: dict[str, Matrix] = {}
    for bone in source.data.bones:
        other = target.data.bones.get(bone.name)
        if other is None:
            continue
        correction = _relative_rest(other).inverted_safe() @ _relative_rest(bone)
        if not _close(correction, Matrix.Identity(4), 1e-5):
            corrections[bone.name] = correction
    same_basis = _close(source.matrix_basis, target.matrix_basis, _TOLERANCE) and source.rotation_mode == target.rotation_mode
    if not corrections and same_basis:
        return action
    new = action.copy()
    new.name = action.name
    for key in (TAKE_PROP, TAKE_MODEL_PROP):
        if key in action:
            new[key] = action[key]
    for layer in new.layers:
        for strip in layer.strips:
            for channelbag in strip.channelbags:
                _retarget_channelbag(channelbag, corrections, target)
                if not same_basis and channelbag.slot.target_id_type == "OBJECT":
                    _hold_object_transform(channelbag, source, target)
    return new


def _hold_object_transform(channelbag, source: bpy.types.Object, target: bpy.types.Object) -> None:
    """``channelbag`` が動かさないオブジェクトの Transform のチャンネルに、``source`` の値の一定のキーを足す。"""
    location, rotation, scale = source.matrix_basis.decompose()
    mode = target.rotation_mode
    if mode == "QUATERNION":
        rotation_path, rotation_values = "rotation_quaternion", tuple(rotation)
    elif mode == "AXIS_ANGLE":
        axis, angle = rotation.to_axis_angle()
        rotation_path, rotation_values = "rotation_axis_angle", (angle, *axis)
    else:
        rotation_path, rotation_values = "rotation_euler", tuple(rotation.to_euler(mode))
    existing = {(fc.data_path, fc.array_index) for fc in channelbag.fcurves}
    animated = {path for path, _ in existing}
    for path, values in (("location", tuple(location)), (rotation_path, rotation_values), ("scale", tuple(scale))):
        if path == rotation_path and animated & {"rotation_quaternion", "rotation_euler", "rotation_axis_angle"}:
            continue
        for index, value in enumerate(values):
            if (path, index) in existing:
                continue
            fcurve = channelbag.fcurves.new(path, index=index)
            fcurve.keyframe_points.insert(1.0, value)


def _retarget_channelbag(channelbag, corrections: dict[str, Matrix], target: bpy.types.Object) -> None:
    by_bone: dict[str, dict[str, dict[int, bpy.types.FCurve]]] = {}
    for fcurve in channelbag.fcurves:
        match = _BONE_PATH.match(fcurve.data_path)
        if match and match.group(1).replace('\\"', '"') in corrections:
            by_bone.setdefault(match.group(1), {}).setdefault(match.group(2), {})[fcurve.array_index] = fcurve
    for escaped, channels in by_bone.items():
        name = escaped.replace('\\"', '"')
        correction = corrections[name]
        pose_bone = target.pose.bones.get(name)
        mode = pose_bone.rotation_mode if pose_bone is not None else "QUATERNION"
        times = sorted({k.co.x for curves in channels.values() for fc in curves.values() for k in fc.keyframe_points})
        if not times:
            continue

        def value(prop: str, index: int, frame: float, default: float) -> float:
            fcurve = channels.get(prop, {}).get(index)
            return fcurve.evaluate(frame) if fcurve is not None else default

        samples = []
        previous = None
        for frame in times:
            location = Vector([value("location", i, frame, 0.0) for i in range(3)])
            scale = Vector([value("scale", i, frame, 1.0) for i in range(3)])
            if "rotation_quaternion" in channels:
                rotation = Quaternion([value("rotation_quaternion", i, frame, 1.0 if i == 0 else 0.0) for i in range(4)])
            else:
                order = mode if mode not in ("QUATERNION", "AXIS_ANGLE") else "XYZ"
                rotation = Euler([value("rotation_euler", i, frame, 0.0) for i in range(3)], order).to_quaternion()
            basis = correction @ Matrix.LocRotScale(location, rotation, scale)
            loc, rot, sca = basis.decompose()
            if previous is not None and previous.dot(rot) < 0:
                rot.negate()
            previous = rot
            samples.append((frame, loc, rot, sca))
        for curves in channels.values():
            for fcurve in curves.values():
                channelbag.fcurves.remove(fcurve)
        path = f'pose.bones["{escaped}"]'
        euler_order = mode if mode not in ("QUATERNION", "AXIS_ANGLE") else None
        columns = [("location", 3, lambda s, i: s[1][i]), ("scale", 3, lambda s, i: s[3][i])]
        if euler_order is None:
            columns.append(("rotation_quaternion", 4, lambda s, i: s[2][i]))
        else:
            eulers = []
            last = None
            for sample in samples:
                euler = sample[2].to_euler(euler_order, last) if last is not None else sample[2].to_euler(euler_order)
                eulers.append(euler)
                last = euler
            lookup = {id(s): e for s, e in zip(samples, eulers)}
            columns.append(("rotation_euler", 3, lambda s, i: lookup[id(s)][i]))
        for prop, size, get in columns:
            for index in range(size):
                fcurve = channelbag.fcurves.new(f"{path}.{prop}", index=index)
                fcurve.group = channelbag.groups.get(name) or channelbag.groups.new(name)
                points = fcurve.keyframe_points
                points.add(len(samples))
                flat = []
                for sample in samples:
                    flat += [sample[0], get(sample, index)]
                points.foreach_set("co", flat)
                points.foreach_set("interpolation", [_LINEAR] * len(samples))
                fcurve.update()
