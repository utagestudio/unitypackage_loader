"""パッケージの AnimationClip（.anim）を Action にして、読み込んだオブジェクトの NLA にミュートで並べる（#209）。

クリップの解析とパスの照合は ``core/animation.py`` と ``core/animation_targets.py``。ここでは次を行う。

- クリップごとに Action を 1 つ作り、対象（オブジェクト・シェイプキー）ごとにスロットを作る。
- シェイプキーの値（Unity の 0〜100 → 0〜1）と表示の切り替え（``hide_viewport`` / ``hide_render``）は、キーと接線を
  そのままベジェ（階段は CONSTANT）にする。GameObject の非アクティブは、Unity の階層での子孫にも当てる。
- Transform は Unity のローカルの値をフレームごとに求め、オブジェクトの行列にしてから位置・回転・スケールのキーにする
  （行列の対応は ``core/model_space.local_basis``）。ボーンを対象にしたカーブは読み込まない（#270）。
- 対象ごとに、クリップの名前の NLA トラックをミュートで足す。読み込んだ直後の見た目は変わらない。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import bpy
from mathutils import Matrix

from ..core.animation import (
    KIND_ACTIVE,
    KIND_ENABLED,
    KIND_SHAPE_KEY,
    TRANSFORM_KINDS,
    AnimationClip,
    Curve,
    bezier_handles,
    parse_clip,
    sample_times,
    transform_spec,
)
from ..core.animation_targets import Binding, TargetObject, TargetTree, bind_paths
from ..core.model_space import ModelUnits, local_basis, local_values
from ..core.transform import IDENTITY, UNITY_TO_BLENDER, Mat4, inverse_affine
from .log import warn_skipped
from .model_import import FBX_NODE_PROP, MODEL_GUID_PROP, node_name

CLIP_GUID_PROP = "unity_clip_guid"
# GameObject の行列に、向きや形を足して置いたオブジェクト（Transform のカーブを当てない）
_ADDED_SHAPE_PROPS = ("unity_terrain", "unity_decal", "unity_light", "unity_camera")
CLIP_PATH_PROP = "unity_clip_path"


@dataclass
class ClipStats:
    """クリップの読み込みの集計（レポートに書く）。"""

    loaded: list[str] = field(default_factory=list)  # 読み込んだクリップの pathname
    humanoid: list[str] = field(default_factory=list)  # Humanoid だけのクリップ（読み込まない）
    humanoid_partial: list[str] = field(default_factory=list)  # Humanoid のクリップのうち、マッスル値以外のカーブだけ読んだもの
    root_only: list[str] = field(default_factory=list)  # ルート自身だけを動かすクリップ（どのオブジェクトのものか決められない）
    unbound: list[str] = field(default_factory=list)  # 対象のオブジェクトが見つからなかったクリップ
    empty: list[str] = field(default_factory=list)  # 読み込める種類のカーブが無いクリップ
    bone_only: list[str] = field(default_factory=list)  # ボーンだけを動かすクリップ（#270 まで読み込まない）
    bone_curves: int = 0  # ボーンを対象にしたカーブ（#270 まで読み込まない）
    missing: int = 0  # 対象が見つからなかったカーブ（シェイプキーが無い、など）
    transform_skipped: int = 0  # 行列の対応が分からないオブジェクトの Transform のカーブ


def _mat4(m: Matrix) -> Mat4:
    return tuple(tuple(float(m[r][c]) for c in range(4)) for r in range(4))  # type: ignore[return-value]


def _target_tree(collection: bpy.types.Collection) -> TargetTree:
    objects = list(collection.all_objects)
    members = set(objects)
    targets = []
    for obj in objects:
        parent = obj.parent if obj.parent in members else None
        bones: tuple = ()
        if obj.type == "ARMATURE":
            bones = tuple((b.name, b.parent.name if b.parent else None) for b in obj.data.bones)
        bone_parent = obj.parent_bone if parent is not None and obj.parent_type == "BONE" and obj.parent_bone else None
        targets.append(TargetObject(obj, node_name(obj), parent, obj.type == "ARMATURE", obj.type == "EMPTY", bone_parent, bones))
    groups = []
    for coll in [collection, *collection.children_recursive]:
        top = [o for o in coll.objects if o.parent is None or o.parent not in members]
        if top:
            groups.append(top)
    return TargetTree(targets, groups)


class ClipImporter:
    """1 回のインポートのクリップの読み込み。"""

    def __init__(self, session, collection: bpy.types.Collection):
        self.session = session
        self.pkg = session.pkg
        self.report = session.report
        self.collection = collection
        self.scene = session.scene
        render = self.scene.render
        self.fps = render.fps / (render.fps_base or 1.0)
        self.frame_start = self.scene.frame_start
        self.stats = ClipStats()
        self._units: dict[str, ModelUnits | None] = {}
        self._tree: TargetTree | None = None

    # ------------------------------------------------------------------

    def run(self) -> ClipStats:
        entries = sorted((e for e in self.pkg.entries.values() if e.ext == ".anim" and e.has_asset), key=lambda e: e.pathname)
        if not entries:
            return self.stats
        for entry in entries:
            try:
                clip = parse_clip(self.pkg.read_asset(entry.guid))
            except Exception as exc:  # noqa: BLE001 - そのクリップだけを外して続ける
                warn_skipped(self.report.warn, f"could not read animation clip {entry.pathname}", exc)
                continue
            if not clip.curves:
                (self.stats.humanoid if clip.humanoid else self.stats.empty).append(entry.pathname)
                continue
            if clip.paths == {""}:
                self.stats.root_only.append(entry.pathname)
                continue
            if self._tree is None:
                self._tree = _target_tree(self.collection)
            bindings = bind_paths(self._tree, clip.paths)
            if not bindings:
                self.stats.unbound.append(entry.pathname)
                continue
            try:
                result = self.build(entry, clip, bindings)
                if result == "loaded":
                    self.stats.loaded.append(entry.pathname)
                    if clip.humanoid:
                        self.stats.humanoid_partial.append(entry.pathname)
                elif result == "bones":
                    self.stats.bone_only.append(entry.pathname)
                else:
                    self.stats.unbound.append(entry.pathname)
            except Exception as exc:  # noqa: BLE001 - そのクリップだけを外して続ける
                warn_skipped(self.report.warn, f"could not import animation clip {entry.pathname}", exc)
        return self.stats

    # ------------------------------------------------------------------

    def build(self, entry, clip: AnimationClip, bindings: list[Binding]) -> str:
        """クリップの Action を作って NLA に並べる。"loaded"、ボーンだけなら "bones"、何も当てられなければ "none"。"""
        action = bpy.data.actions.new(clip.name or entry.name)
        action[CLIP_GUID_PROP] = entry.guid
        action[CLIP_PATH_PROP] = entry.pathname
        action.use_frame_range = True
        action.frame_start = 0.0
        action.frame_end = max((clip.stop - clip.start) * self.fps, 1.0)
        action.use_cyclic = clip.loop
        strip = action.layers.new("Layer").strips.new(type="KEYFRAME")
        users: dict[bpy.types.ID, bpy.types.ActionSlot] = {}

        def slot_for(datablock: bpy.types.ID, id_type: str, label: str):
            slot = users.get(datablock)
            if slot is None:
                slot = action.slots.new(id_type=id_type, name=label[:60] or id_type)
                users[datablock] = slot
            return slot

        by_path: dict[str, list[Curve]] = {}
        for curve in clip.curves:
            by_path.setdefault(curve.path, []).append(curve)
        tree = self._tree
        bones = 0
        for binding in bindings:
            for path, curves in by_path.items():
                target = binding.targets.get(path)
                if target is None:
                    self.stats.missing += len(curves)
                    continue
                if target.bone is not None:
                    bones += len(curves)
                    continue
                obj = target.key
                for curve in curves:
                    if curve.kind == KIND_SHAPE_KEY:
                        self._shape_key(strip, slot_for, obj, curve, clip)
                for kind in (KIND_ACTIVE, KIND_ENABLED):
                    toggles = [c for c in curves if c.kind == kind]
                    if toggles:
                        hidden = tree.unity_descendants(obj) if kind == KIND_ACTIVE else [obj]
                        for member in hidden:
                            self._toggle(strip, slot_for, member, toggles[-1], clip)
                transforms = [c for c in curves if c.kind in TRANSFORM_KINDS]
                if transforms:
                    self._transform(strip, slot_for, obj, transforms, clip)
        self.stats.bone_curves += bones
        if not users:
            bpy.data.actions.remove(action)
            return "bones" if bones else "none"
        for datablock, slot in users.items():
            self._add_track(datablock, action, slot, clip.name or entry.name)
        return "loaded"

    def _add_track(self, datablock, action, slot, name: str) -> None:
        animation_data = datablock.animation_data or datablock.animation_data_create()
        track = animation_data.nla_tracks.new()
        track.name = name
        track.mute = True
        nla_strip = track.strips.new(name, self.frame_start, action)
        if nla_strip.action_slot != slot:
            nla_strip.action_slot = slot

    # 各種のカーブ ------------------------------------------------------

    def _frame(self, time: float, clip: AnimationClip) -> float:
        return (time - clip.start) * self.fps

    def _write_keys(self, channelbag, data_path: str, index: int, curve: Curve, clip: AnimationClip, scale: float = 1.0,
                    constant: bool = False, transform=None) -> None:
        if channelbag.fcurves.find(data_path, index=index) is not None:
            return  # 同じ対象に別のカーブで書いた（GameObject の非アクティブと Renderer の無効など）
        fcurve = channelbag.fcurves.new(data_path, index=index)
        keys = bezier_handles(curve.keys, scale)
        fcurve.keyframe_points.add(len(keys))
        for point, key in zip(fcurve.keyframe_points, keys):
            value = transform(key.value) if transform else key.value
            point.co = (self._frame(key.time, clip), value)
            if constant or key.constant:
                point.interpolation = "CONSTANT"
            else:
                point.interpolation = "BEZIER"
                point.handle_left_type = "FREE"
                point.handle_right_type = "FREE"
                point.handle_left = (self._frame(key.left[0], clip), key.left[1])
                point.handle_right = (self._frame(key.right[0], clip), key.right[1])
        if clip.loop:
            fcurve.modifiers.new("CYCLES")
        fcurve.update()

    def _shape_key(self, strip, slot_for, obj, curve: Curve, clip: AnimationClip) -> None:
        name = curve.shape_key
        shape_keys = obj.data.shape_keys if obj.type == "MESH" and obj.data is not None else None
        if shape_keys is None or name not in shape_keys.key_blocks:
            self.stats.missing += 1
            return
        block = shape_keys.key_blocks[name]
        # Unity の重みは 100 を超えられる。Blender の値は slider_max までなので広げる
        peak = max((k.value for k in curve.keys), default=0.0) / 100.0
        if peak > block.slider_max:
            block.slider_max = min(peak, 10.0)
        slot = slot_for(shape_keys, "KEY", f"{node_name(obj)} Key")
        bag = strip.channelbag(slot, ensure=True)
        self._write_keys(bag, f'key_blocks["{_escape(name)}"].value', 0, curve, clip, scale=0.01)

    def _toggle(self, strip, slot_for, obj, curve: Curve, clip: AnimationClip) -> None:
        slot = slot_for(obj, "OBJECT", node_name(obj))
        bag = strip.channelbag(slot, ensure=True)
        for data_path in ("hide_viewport", "hide_render"):
            self._write_keys(bag, data_path, 0, curve, clip, constant=True, transform=lambda v: 0.0 if v >= 0.5 else 1.0)

    def _transform(self, strip, slot_for, obj, curves: list[Curve], clip: AnimationClip) -> None:
        sides = self._basis_sides(obj)
        if sides is None:
            self.stats.transform_skipped += len(curves)
            return
        left, right = sides
        slot = slot_for(obj, "OBJECT", node_name(obj))
        bag = strip.channelbag(slot, ensure=True)
        rest = _mat4(obj.matrix_basis)
        _, rest_rotation, _ = local_values(rest, left, right)
        mode = obj.rotation_mode
        times = sample_times(curves, clip.start, clip.stop, self.fps)
        has = {c.kind for c in curves}
        rows: list[tuple[float, Matrix]] = []
        for time in times:
            spec = transform_spec(curves, time, rest_rotation)
            rows.append((self._frame(time, clip), Matrix(local_basis(rest, spec, left, right))))
        channels: dict[tuple[str, int], list[tuple[float, float]]] = {}
        previous = obj.rotation_euler.copy() if mode not in ("QUATERNION", "AXIS_ANGLE") else None
        for frame, matrix in rows:
            location, rotation, scale = matrix.decompose()
            if "position" in has:
                for i in range(3):
                    channels.setdefault(("location", i), []).append((frame, location[i]))
            if has & {"rotation", "euler"}:
                if mode == "QUATERNION":
                    for i in range(4):
                        channels.setdefault(("rotation_quaternion", i), []).append((frame, rotation[i]))
                elif mode == "AXIS_ANGLE":
                    axis, angle = rotation.to_axis_angle()
                    for i, value in enumerate((angle, *axis)):
                        channels.setdefault(("rotation_axis_angle", i), []).append((frame, value))
                else:
                    euler = rotation.to_euler(mode, previous) if previous is not None else rotation.to_euler(mode)
                    previous = euler
                    for i in range(3):
                        channels.setdefault(("rotation_euler", i), []).append((frame, euler[i]))
            if "scale" in has:
                for i in range(3):
                    channels.setdefault(("scale", i), []).append((frame, scale[i]))
        for (data_path, index), points in channels.items():
            if bag.fcurves.find(data_path, index=index) is not None:
                continue
            fcurve = bag.fcurves.new(data_path, index=index)
            fcurve.keyframe_points.add(len(points))
            for point, (frame, value) in zip(fcurve.keyframe_points, points):
                point.co = (frame, value)
                point.interpolation = "LINEAR"
            if clip.loop:
                fcurve.modifiers.new("CYCLES")
            fcurve.update()

    def _basis_sides(self, obj) -> tuple[Mat4, Mat4] | None:
        """オブジェクトの行列 basis = left·L·right の (left, right)。分からなければ None。

        - FBX のノードで、親も同じモデルの FBX のノード（アーマチュアを除く）: left = A⁻¹、right = A
        - FBX のノードで、親がそれ以外: left = C、right = A（``model_space.node_basis`` と同じ）
        - シーン・prefab の GameObject のオブジェクト（Empty、Unity の Mesh・組み込みメッシュ）: left = C、right = C⁻¹
          （親の GameObject の Empty の下に C·L·C⁻¹ で置いている。ライト・カメラ・Terrain・デカールは向きや形を足しているので除く）
        """
        parent = obj.parent
        if obj.get(FBX_NODE_PROP) is not None and obj.get(MODEL_GUID_PROP):
            units = self._model_units(str(obj[MODEL_GUID_PROP]))
            if units is None:
                return None
            a = units.basis_a()
            if parent is not None and parent.get(MODEL_GUID_PROP) == obj[MODEL_GUID_PROP] and parent.get(FBX_NODE_PROP) is not None:
                if parent.type == "ARMATURE" or obj.parent_type != "OBJECT":
                    return None  # スキンメッシュ・ボーンの子は、Unity と親子が違う
                inverse = inverse_affine(a)
                return (inverse, a) if inverse is not None else None
            return UNITY_TO_BLENDER, a
        if obj.get("unity_game_object") is not None and obj.get(MODEL_GUID_PROP) is None and obj.type in ("EMPTY", "MESH"):
            if any(key in obj for key in _ADDED_SHAPE_PROPS):
                return None
            inverse = inverse_affine(UNITY_TO_BLENDER)
            return UNITY_TO_BLENDER, inverse or IDENTITY
        return None

    def _model_units(self, guid: str) -> ModelUnits | None:
        if guid not in self._units:
            from .scene_placement import model_units

            summary = self.session.model_by_guid.get(guid)
            path = self.session.paths.get(guid)
            self._units[guid] = model_units(summary, path) if summary is not None and path is not None else None
        return self._units[guid]


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace('"', '\\"')


def report_lines(stats: ClipStats) -> Iterable[str]:
    """レポートの警告に足す行（読み込まなかったものの内訳）。"""
    if stats.humanoid:
        yield f"skipped {len(stats.humanoid)} Humanoid animation clip(s) (muscle curves are not supported)"
    if stats.humanoid_partial:
        yield f"imported only the non-muscle curves of {len(stats.humanoid_partial)} Humanoid animation clip(s)"
    if stats.root_only:
        yield f"skipped {len(stats.root_only)} animation clip(s) that only animate the object they are played on (target unknown)"
    if stats.bone_only:
        yield f"skipped {len(stats.bone_only)} animation clip(s) that only animate bones (not supported yet)"
    if stats.bone_curves:
        yield f"skipped {stats.bone_curves} animation curve(s) targeting bones (not supported yet)"
    if stats.missing:
        yield f"{stats.missing} animation curve(s) had no matching shape key or object"
    if stats.unbound:
        yield f"{len(stats.unbound)} animation clip(s) did not match any imported object"
    if stats.transform_skipped:
        yield f"skipped {stats.transform_skipped} Transform curve(s) on objects whose Unity transform could not be determined"
