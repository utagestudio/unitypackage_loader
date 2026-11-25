"""モデルの中のアニメーションのテイクを含む合成 unitypackage を生成する（Issue #276）。

    blender -b --factory-startup --python tests/make_synthetic_motion_package.py -- <出力パス.unitypackage>

実在アセットのデータは使わない。

- Motions/Walk.fbx: モーションだけ（アーマチュア Rig。ボーン Hips → Spine → Hand。末端に Hand_End）。テイクは tpose（1 フレーム）と walk_take
  （30 フレームで Spine を 90° 曲げる）。.meta の clipAnimations で tpose・Walk_Start（0〜10）・Walk（0〜30、ループ）に分ける。
- Motions/Run.fbx: モーションだけ。骨格は Walk の部分集合（Hand が無い）。書き出したときの姿勢がレストになるので、レストも Walk と違う。テイク run_take
  （30 フレームで Spine を 60° 曲げる）。clipAnimations は無い（テイクがそのままクリップになる）。
- Models/Character.fbx: アーマチュア CharRig とスキンメッシュ Body。テイク wave_take。メッシュがあるのでまとめない。
- Scenes/Two.unity: Character.fbx を 2 か所に置く（テイクの Action を使い回す）。

曲げる Action はクォータニオンの成分を LINEAR でつなぐので、角度は時間に比例しない（Walk の 0.2 秒は 18° ではなく 17.086°）。
期待値（tests/expectations_synthetic_motion*.json）のボーンの向きは、この補間で求めた値。
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402

from tests.make_synthetic_package import guid_of, write_package  # noqa: E402
from tests.unity_yaml_writer import HEADER, ROOT_TRANSFORM, instance, mod  # noqa: E402


def _armature(name: str, bones: list[tuple[str, tuple, tuple, str | None]]) -> bpy.types.Object:
    bpy.ops.object.armature_add(location=(0, 0, 0))
    armature = bpy.context.object
    armature.name = armature.data.name = name
    bpy.ops.object.mode_set(mode="EDIT")
    edit = armature.data.edit_bones
    edit.remove(edit[0])
    for bone_name, head, tail, parent in bones:
        bone = edit.new(bone_name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = edit[parent]
            bone.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    return armature


def _bend(armature, action_name: str, bone: str, degrees: float, frames: int) -> bpy.types.Action:
    """``bone`` をローカルの X まわりに 0 → ``degrees`` 曲げる Action（フレーム 1 から ``frames`` フレーム）。"""
    action = bpy.data.actions.new(action_name)
    armature.animation_data_create()
    armature.animation_data.action = action
    pose_bone = armature.pose.bones[bone]
    pose_bone.rotation_mode = "QUATERNION"
    for frame, angle in ((1, 0.0), (1 + frames, math.radians(degrees))):
        pose_bone.rotation_quaternion = (math.cos(angle / 2), math.sin(angle / 2), 0, 0)
        pose_bone.keyframe_insert("rotation_quaternion", frame=frame)
    for fcurve in _fcurves(action):
        for point in fcurve.keyframe_points:
            point.interpolation = "LINEAR"
    action.use_fake_user = True
    return action


def _fcurves(action):
    # Blender 5 でも action.fcurves は残っているが、スロット付きの Action では空を返す
    return [fc for layer in action.layers for strip in layer.strips for bag in strip.channelbags for fc in bag.fcurves]


def _hold(armature, action_name: str, frames: int) -> bpy.types.Action:
    """どこも動かさない Action（T ポーズのテイク）。"""
    action = bpy.data.actions.new(action_name)
    armature.animation_data.action = action
    for pose_bone in armature.pose.bones:
        pose_bone.rotation_mode = "QUATERNION"
        pose_bone.rotation_quaternion = (1, 0, 0, 0)
        for frame in (1, 1 + frames):
            pose_bone.keyframe_insert("rotation_quaternion", frame=frame)
    action.use_fake_user = True
    return action


def _export(path: Path, objects) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.ops.export_scene.fbx(
        filepath=str(path), use_selection=True, add_leaf_bones=False, bake_anim=True, bake_anim_use_all_actions=True,
        bake_anim_use_nla_strips=False, bake_anim_simplify_factor=0.0,
    )


def _reset() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = 30


# 読み込みは Ignore Leaf Bones（既定 ON）で末端のボーンを消すので、チェーンの先に末端のボーン（*_End）を置く
SPINE = [("Hips", (0, 0, 0.8), (0, 0, 1.0), None), ("Spine", (0, 0, 1.0), (0, 0, 1.4), "Hips")]


def export_walk(path: Path) -> None:
    _reset()
    rig = _armature("Rig", SPINE + [("Hand", (0, 0, 1.4), (0, 0, 1.6), "Spine"), ("Hand_End", (0, 0, 1.6), (0, 0, 1.7), "Hand")])
    rig.animation_data_create()
    _hold(rig, "tpose", 1)
    _bend(rig, "walk_take", "Spine", 90, 30)
    rig.animation_data.action = None
    _export(path, [rig])


def export_run(path: Path) -> None:
    _reset()
    # Spine のレストを Y の向きに傾ける（Walk とレストが違う）
    rig = _armature("Rig", [SPINE[0], ("Spine", (0, 0, 1.0), (0, 0.2, 1.35), "Hips"), ("Spine_End", (0, 0.2, 1.35), (0, 0.25, 1.45), "Spine")])
    rig.animation_data_create()
    _bend(rig, "run_take", "Spine", 60, 30)
    rig.animation_data.action = None
    _export(path, [rig])


def export_character(path: Path) -> None:
    _reset()
    rig = _armature("CharRig", SPINE + [("Spine_End", (0, 0, 1.4), (0, 0, 1.5), "Spine")])
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0, 0, 1.2))
    body = bpy.context.object
    body.name = body.data.name = "Body"
    body.vertex_groups.new(name="Spine").add([v.index for v in body.data.vertices], 1.0, "REPLACE")
    body.parent = rig
    body.modifiers.new("Armature", "ARMATURE").object = rig
    rig.animation_data_create()
    _bend(rig, "wave_take", "Spine", 45, 30)
    rig.animation_data.action = None
    _export(path, [rig, body])


def takes_of(path: Path) -> dict[str, tuple[float, float]]:
    """書き出した FBX を読み直して、テイクの名前 → Unity のフレーム番号の範囲（Blender のフレーム - 1）。"""
    _reset()
    bpy.ops.wm.fbx_import(filepath=str(path), use_anim=True)
    return {a.name: (a.frame_range[0] - 1, a.frame_range[1] - 1) for a in bpy.data.actions}


def motion_meta(guid: str, clips: list[tuple[str, str, float, float, bool]]) -> str:
    lines = ["fileFormatVersion: 2", f"guid: {guid}", "ModelImporter:", "  serializedVersion: 22200", "  externalObjects: {}",
             "  materials:", "    materialImportMode: 2", "  animations:", "    importAnimatedCustomProperties: 0"]
    if clips:
        lines.append("    clipAnimations:")
        for name, take, first, last, loop in clips:
            lines += [
                "    - serializedVersion: 16",
                f"      name: {name}",
                f"      takeName: {take}",
                f"      firstFrame: {first:g}",
                f"      lastFrame: {last:g}",
                "      wrapMode: 0",
                f"      loopTime: {1 if loop else 0}",
            ]
    else:
        lines.append("    clipAnimations: []")
    lines += ["  meshes:", "    globalScale: 1", "    useFileScale: 1", "  importAnimation: 1", "  animationType: 2"]
    return "\n".join(lines) + "\n"


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else REPO_ROOT / "_local" / "unitypackages" / "synthetic_motion.unitypackage"
    tmp = Path(tempfile.mkdtemp(prefix="synthetic_motion_"))
    entries: dict[str, tuple[str, bytes | None, str | None]] = {}

    def add(pathname: str, asset: bytes, meta: str) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    export_walk(tmp / "walk.fbx")
    export_run(tmp / "run.fbx")
    export_character(tmp / "character.fbx")
    walk_takes = takes_of(tmp / "walk.fbx")
    print("walk takes:", walk_takes, "run takes:", takes_of(tmp / "run.fbx"), "character takes:", takes_of(tmp / "character.fbx"))
    tpose = next(name for name in walk_takes if name.endswith("tpose"))
    walk = next(name for name in walk_takes if name.endswith("walk_take"))
    start = walk_takes[walk][0]
    walk_clips = [
        ("tpose", tpose, walk_takes[tpose][0], walk_takes[tpose][1], False),
        ("Walk_Start", walk, start, start + 10, False),
        ("Walk", walk, start, start + 30, True),
    ]
    path = "Assets/Synthetic/Motions/Walk.fbx"
    add(path, (tmp / "walk.fbx").read_bytes(), motion_meta(guid_of(path), walk_clips))
    path = "Assets/Synthetic/Motions/Run.fbx"
    add(path, (tmp / "run.fbx").read_bytes(), motion_meta(guid_of(path), []))
    path = "Assets/Synthetic/Models/Character.fbx"
    character = add(path, (tmp / "character.fbx").read_bytes(), motion_meta(guid_of(path), []))
    scene_path = "Assets/Synthetic/Scenes/Two.unity"
    scene = HEADER + "".join([
        instance(100, character, 0, [mod(ROOT_TRANSFORM, character, "m_LocalPosition.x", -2)]),
        instance(200, character, 0, [mod(ROOT_TRANSFORM, character, "m_LocalPosition.x", 2)]),
    ])
    add(scene_path, scene.encode(), f"fileFormatVersion: 2\nguid: {guid_of(scene_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")
    write_package(out, entries)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
