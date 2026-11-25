"""モデル（FBX など）の中のアニメーションのテイクを、Unity のクリップの設定どおりに NLA に並べる（#276）。

Blender の FBX インポーターは、テイクごとにテイクの名前の Action を作り、最初のテイクをアクティブな Action にする。
Unity はモデルの .meta の ``clipAnimations`` で、テイクから名前・範囲を決めてクリップを切り出す（無ければテイクごとに全体）。
ここでは読み込んだ直後に、クリップごとに範囲を絞った NLA ストリップを、クリップの名前のトラックに置く。

- 同じテイクの Action を範囲違いのストリップで共有する（クリップごとに Action を複製しない）。
- 状態を切り替えるだけの短いクリップ（T ポーズなど。``core.animation.STATE_CLIP_MAX_LENGTH`` 以下）は置かない。
- どのクリップにも使われないテイクの Action は消す。アクティブな Action は外す（読み込んだ直後の見た目を、アニメーションを
  読まないときと同じにする）。
- 同じモデルを何度も読む（シーンの配置ごと）ときは、最初に読んだときの Action を使い回し、新しく作られた分は消す。
- モーションだけのモデル（メッシュが無く、アーマチュアが 1 体）が同じ骨格で並ぶとき（Models 単位）は、アーマチュアを 1 体にまとめて
  全部のトラックを載せる（``merge_motion_rigs``）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from ..core.animation import STATE_CLIP_MAX_LENGTH
from ..core.meta import ModelClip, ModelImporterInfo, strip_numeric_suffix
from .model_import import FBX_NODE_PROP, MODEL_GUID_PROP, is_removed

# Blender の FBX インポーターの anim_offset の既定値（FBX のフレーム 0 を Blender のフレーム 1 に置く）
ANIM_OFFSET = 1.0
# Action に残す、元のテイクの名前とモデルの GUID
TAKE_PROP = "unity_take"
TAKE_MODEL_PROP = "unity_take_model"

MODE_MUTED = "MUTED"
MODE_ACTIVE = "ACTIVE"
MODE_NONE = "NONE"

# keyframe_points の interpolation に foreach_set で渡す LINEAR の値
_LINEAR = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value


@dataclass
class MotionStats:
    """モデルのテイクから作ったクリップの集計（レポートに書く）。"""

    clips: list[str] = field(default_factory=list)  # 置いたクリップ（"モデルのパス: クリップ名"）
    state: int = 0  # 状態を切り替えるだけで置かなかったクリップ
    missing_takes: list[str] = field(default_factory=list)  # .meta のクリップのテイクが見つからなかったもの
    merged_rigs: int = 0  # 1 体にまとめて消したアーマチュア

    def report_lines(self):
        if self.state:
            yield f"skipped {self.state} model animation clip(s) that only switch a state (0.1 s or shorter, e.g. a T-pose)"
        if self.missing_takes:
            yield f"{len(self.missing_takes)} model animation clip(s) refer to a take that was not found in the model"
        if self.merged_rigs:
            yield f"merged {self.merged_rigs} motion-only armature(s) with the same skeleton into one"


@dataclass(frozen=True)
class _Clip:
    name: str
    action: bpy.types.Action
    start: float  # Blender のフレーム（Action の中の範囲）
    end: float
    loop: bool = False


def _slot_owner(slot, objects: list[bpy.types.Object]):
    """スロットの対象（オブジェクトかシェイプキー）を、読み込んだオブジェクトから探す。"""
    name = slot.identifier[2:]
    if slot.target_id_type == "OBJECT":
        for obj in objects:
            if obj.get(FBX_NODE_PROP, obj.name) == name or obj.name == name:
                return obj
        return None
    if slot.target_id_type == "KEY":
        for obj in objects:
            key = getattr(obj.data, "shape_keys", None) if obj.data is not None else None
            if key is not None and key.name == name:
                return key
    return None


def _users(action: bpy.types.Action, objects: list[bpy.types.Object]) -> list[tuple[bpy.types.ID, bpy.types.ActionSlot]]:
    users = []
    for slot in action.slots:
        owner = _slot_owner(slot, objects)
        if owner is None and len(action.slots) == 1:
            # スロットの名前が付け替わっていても、アニメーションを持つオブジェクトが 1 つなら、それとみなす
            animated = [o for o in objects if o.animation_data is not None and o.animation_data.action == action]
            owner = animated[0] if len(animated) == 1 else None
        if owner is not None:
            users.append((owner, slot))
    return users


def _take_of(action: bpy.types.Action) -> str:
    return str(action.get(TAKE_PROP) or strip_numeric_suffix(action.name))


def _take_name(action_name: str, known: set[str]) -> str:
    """Action の名前から元のテイクの名前を求める。.meta のテイク名と完全一致すればそのまま、無ければ Blender の連番を外す。"""
    return action_name if action_name in known else strip_numeric_suffix(action_name)


class MotionBuilder:
    """読み込みの間、モデルのテイクから NLA を作る（モデルを読み込むたびに ``arrange`` を呼ぶ）。"""

    def __init__(self, scene: bpy.types.Scene, mode: str):
        self.scene = scene
        self.mode = mode
        self.stats = MotionStats()
        # モデルの GUID → テイクの名前 → 最初に読んだときの Action（2 回目以降に使い回す）
        self._actions: dict[str, dict[str, bpy.types.Action]] = {}

    def arrange(self, model_guid: str, pathname: str, info: ModelImporterInfo | None,
                objects: list[bpy.types.Object], actions: list[bpy.types.Action]) -> None:
        if not actions:
            return
        if info is not None and not info.import_animation:
            # Unity でアニメーションを取り込まない設定のモデル（Unity にクリップが無い）
            for obj in objects:
                if obj.animation_data is not None:
                    obj.animation_data.action = None
            bpy.data.batch_remove(actions)
            return
        known = {c.take for c in info.clips} if info is not None else set()
        takes = {a.as_pointer(): _take_name(a.name, known) for a in actions}
        cached = self._actions.get(model_guid)
        if cached is not None and any(is_removed(a) for a in cached.values()):
            cached = None  # 前の読み込みが失敗して片付けで消えた
        first_time = cached is None
        if first_time:
            cached = self._actions[model_guid] = {}
            for action in actions:
                action[TAKE_PROP] = takes[action.as_pointer()]
                action[TAKE_MODEL_PROP] = model_guid
                cached.setdefault(takes[action.as_pointer()], action)
        # 新しい Action の (対象, スロットの識別子) を、テイクの名前ごとに控える（使い回すときは、元の Action の同じ識別子のスロットを使う）
        targets: dict[str, list[tuple[bpy.types.ID, str]]] = {}
        for action in actions:
            targets.setdefault(takes[action.as_pointer()], []).extend((owner, slot.identifier) for owner, slot in _users(action, objects))
        for obj in objects:
            key = getattr(obj.data, "shape_keys", None) if obj.data is not None else None
            for owner in (obj, key):
                if owner is not None and owner.animation_data is not None:
                    owner.animation_data.action = None
        clips = self._clips(info, cached, count=first_time)
        used: set[str] = set()
        for clip in clips:
            take = _take_of(clip.action)
            for owner, identifier in targets.get(take, []):
                slot = clip.action.slots.get(identifier)
                if slot is None:
                    continue
                self._add_track(owner, clip, slot)
                used.add(take)
            if first_time:
                self.stats.clips.append(f"{pathname}: {clip.name}")
        removed = [a for a in actions if not (first_time and _take_of(a) in used and cached.get(_take_of(a)) == a)]
        if first_time:
            # どのクリップにも使われなかったテイクは、使い回しの表からも外す
            for take in [t for t in cached if t not in used]:
                del cached[take]
        if removed:
            bpy.data.batch_remove(removed)

    def _clips(self, info: ModelImporterInfo | None, cached: dict[str, bpy.types.Action], count: bool = True) -> list[_Clip]:
        """置くクリップ。``count`` なら、外したものをレポートに数える（同じモデルの 2 回目以降の読み込みでは数えない）。"""
        fps = self.scene.render.fps / (self.scene.render.fps_base or 1.0)
        settings: list[ModelClip] = list(info.clips) if info is not None else []
        clips: list[_Clip] = []
        if not settings:
            for take, action in cached.items():
                start, end = action.frame_range
                settings.append(ModelClip(take, take, start - ANIM_OFFSET, end - ANIM_OFFSET))
        for setting in settings:
            action = cached.get(setting.take)
            if action is None:
                if count:
                    self.stats.missing_takes.append(setting.name)
                continue
            if (setting.last_frame - setting.first_frame) / max(fps, 1e-6) <= STATE_CLIP_MAX_LENGTH:
                if count:
                    self.stats.state += 1
                continue
            clips.append(_Clip(setting.name, action, setting.first_frame + ANIM_OFFSET, setting.last_frame + ANIM_OFFSET, setting.loop))
        return clips

    def _add_track(self, owner: bpy.types.ID, clip: _Clip, slot) -> None:
        animation_data = owner.animation_data or owner.animation_data_create()
        active = self.mode == MODE_ACTIVE and not any(not t.mute for t in animation_data.nla_tracks)
        track = animation_data.nla_tracks.new()
        track.name = clip.name
        strip = track.strips.new(clip.name, int(self.scene.frame_start), clip.action)
        if strip.action_slot != slot:
            strip.action_slot = slot
        set_strip_range(strip, clip.start, clip.end)
        if clip.loop:
            loop_strip(strip, self.scene)
        track.mute = not active


def loop_strip(strip, scene: bpy.types.Scene) -> None:
    """ループするクリップ（Unity の Loop Time）のストリップを、シーンのフレーム範囲の終わりまで繰り返す。

    NLA はストリップの範囲の外を評価しない（Action の Cycles モディファイアーは範囲の中でしか働かない）ので、繰り返しの回数で表す。
    """
    length = strip.action_frame_end - strip.action_frame_start
    if length <= 0:
        return
    span = scene.frame_end - strip.frame_start + 1
    strip.repeat = float(max(1, math.ceil(span / length)))


def set_strip_range(strip, start: float, end: float) -> None:
    """ストリップが使う Action の範囲を変え、ストリップの長さをそれに合わせる（置く位置は変えない）。"""
    begin = strip.frame_start
    if start >= strip.action_frame_end:
        strip.action_frame_end = end
        strip.action_frame_start = start
    else:
        strip.action_frame_start = start
        strip.action_frame_end = end
    strip.frame_start = begin
    strip.frame_end = begin + (end - start) * strip.scale * strip.repeat


# ---------------------------------------------------------------------------
# モーションだけのモデルのアーマチュアをまとめる
# ---------------------------------------------------------------------------

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


def copy_track(animation_data, track, strip, action: bpy.types.Action) -> None:
    """``strip`` と同じ範囲・名前のトラックを ``animation_data`` に足す（Action は ``action`` に差し替える）。"""
    has_active = any(not t.mute for t in animation_data.nla_tracks)
    new_track = animation_data.nla_tracks.new()
    new_track.name = track.name
    new_strip = new_track.strips.new(strip.name, int(strip.frame_start), action)
    slot = action.slots.get(strip.action_slot.identifier) if strip.action_slot is not None else None
    if slot is not None and new_strip.action_slot != slot:
        new_strip.action_slot = slot
    set_strip_range(new_strip, strip.action_frame_start, strip.action_frame_end)
    new_strip.repeat = strip.repeat
    new_track.mute = track.mute or has_active


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
