"""AnimationClip（.anim）を含む合成 unitypackage を生成する（Issue #209）。

    blender -b --factory-startup --python tests/make_synthetic_anim_package.py -- <出力パス.unitypackage>

実在アセットのデータは使わない。

- Models/Rig.fbx: 最上位のノード Face（シェイプキー smile を持つ板）・Hat・Door（子に Knob）・Armature（ボーン Hips → Spine。
  スキンメッシュ Body はアーマチュアの子として書き出す。Unity では Armature と兄弟）。
- Animations/
  - Smile.anim: Face の blendShape.smile を 0 → 100（1 秒、接線 0）。
  - HatOff.anim: Hat の m_IsActive を 0（階段）。
  - DoorOpen.anim: Door のオイラー回転を (0, 0, 0) → (0, 90, 0)（1 秒）、Door/Knob の位置を (0.5, 1, 0)。ループする。
  - Wave.anim: ボーン Armature/Hips/Spine の回転（読み込まない。#270）と、Body の m_Enabled を 0。
  - Gesture.anim: Humanoid のマッスル値だけ（読み込まない）。
  - Spin.anim: ルート自身（空のパス）の回転だけ（どのオブジェクトのものか決められない）。
  - Elsewhere.anim: どこにも無いパス。
- Scenes/Two.unity: Rig.fbx を X に -2 と 2 の 2 か所に置く（同じクリップが両方に付く）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tempfile  # noqa: E402

import bpy  # noqa: E402

from tests.anim_writer import clip_yaml  # noqa: E402
from tests.make_synthetic_package import guid_of, model_meta, write_package  # noqa: E402
from tests.unity_yaml_writer import HEADER, ROOT_TRANSFORM, instance, mod  # noqa: E402

INF = float("inf")
ZERO3 = (0, 0, 0)
ZERO4 = (0, 0, 0, 0)


def export_rig_fbx(path: Path) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # 表情のシェイプキーを持つ板
    bpy.ops.mesh.primitive_plane_add(size=0.5, location=(0, 0, 1.6))
    face = bpy.context.object
    face.name = face.data.name = "Face"
    face.shape_key_add(name="Basis")
    smile = face.shape_key_add(name="smile")
    for point in smile.data:
        point.co.z += 0.1
    bpy.ops.mesh.primitive_cube_add(size=0.2, location=(0, 0, 1.9))
    hat = bpy.context.object
    hat.name = hat.data.name = "Hat"
    # 扉と、その子の取っ手
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(1.5, 0, 0.5))
    door = bpy.context.object
    door.name = door.data.name = "Door"
    bpy.ops.mesh.primitive_cube_add(size=0.1, location=(1.9, -0.5, 0.5))
    knob = bpy.context.object
    knob.name = knob.data.name = "Knob"
    knob.parent = door
    knob.matrix_parent_inverse = door.matrix_world.inverted()
    # アーマチュアとスキンメッシュ
    bpy.ops.object.armature_add(location=(0, 0, 0))
    armature = bpy.context.object
    armature.name = armature.data.name = "Armature"
    bpy.ops.object.mode_set(mode="EDIT")
    bones = armature.data.edit_bones
    hips = bones[0]
    hips.name = "Hips"
    hips.head, hips.tail = (0, 0, 0.8), (0, 0, 1.0)
    spine = bones.new("Spine")
    spine.head, spine.tail, spine.parent = (0, 0, 1.0), (0, 0, 1.3), hips
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0, 0, 1.0))
    body = bpy.context.object
    body.name = body.data.name = "Body"
    group = body.vertex_groups.new(name="Spine")
    group.add([v.index for v in body.data.vertices], 1.0, "REPLACE")
    body.parent = armature
    body.modifiers.new("Armature", "ARMATURE").object = armature
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False)


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else REPO_ROOT / "_local" / "unitypackages" / "synthetic_anim.unitypackage"
    entries: dict[str, tuple[str, bytes | None, str | None]] = {}

    def add(pathname: str, asset: bytes | None, meta: str | None) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    def clip(name: str, text: str) -> None:
        path = f"Assets/Synthetic/Animations/{name}.anim"
        add(path, text.encode(), f"fileFormatVersion: 2\nguid: {guid_of(path)}\nNativeFormatImporter:\n  mainObjectFileID: 7400000\n")

    tmp = Path(tempfile.mkdtemp(prefix="synthetic_anim_"))
    export_rig_fbx(tmp / "rig.fbx")
    rig_path = "Assets/Synthetic/Models/Rig.fbx"
    rig = add(rig_path, (tmp / "rig.fbx").read_bytes(), model_meta(guid_of(rig_path), {}))

    clip("Smile", clip_yaml("Smile", float_curves=[("Face", "blendShape.smile", 137, [(0, 0, 0, 0), (1, 100, 0, 0)])], stop_time=1))
    clip("HatOff", clip_yaml("HatOff", float_curves=[("Hat", "m_IsActive", 1, [(0, 0, INF, INF)])]))
    clip("DoorOpen", clip_yaml(
        "DoorOpen",
        vector_curves={
            "m_EulerCurves": [("Door", [(0, ZERO3, ZERO3, ZERO3), (1, (0, 90, 0), ZERO3, ZERO3)])],
            "m_PositionCurves": [("Door/Knob", [(0, (0.5, 1, 0), ZERO3, ZERO3)])],
        },
        loop=True,
        stop_time=1,
    ))
    clip("Wave", clip_yaml(
        "Wave",
        float_curves=[("Body", "m_Enabled", 137, [(0, 0, INF, INF)])],
        vector_curves={"m_RotationCurves": [("Armature/Hips/Spine", [(0, (0, 0, 0, 1), ZERO4, ZERO4), (1, (0, 0.38268343, 0, 0.9238795), ZERO4, ZERO4)])]},
        stop_time=1,
    ))
    clip("Gesture", clip_yaml("Gesture", float_curves=[("", "LeftHand.Index.1 Stretched", 95, [(0, -1, 0, 0)]), ("", "RootT.y", 95, [(0, 1, 0, 0)])]))
    clip("Spin", clip_yaml("Spin", vector_curves={"m_EulerCurves": [("", [(0, ZERO3, ZERO3, ZERO3), (2, (0, 360, 0), ZERO3, ZERO3)])]}, loop=True, stop_time=2))
    clip("Elsewhere", clip_yaml("Elsewhere", float_curves=[("Nothing/Here", "m_IsActive", 1, [(0, 0, INF, INF)])]))

    scene_path = "Assets/Synthetic/Scenes/Two.unity"
    scene = HEADER + "".join([
        instance(100, rig, 0, [mod(ROOT_TRANSFORM, rig, "m_LocalPosition.x", -2)]),
        instance(200, rig, 0, [mod(ROOT_TRANSFORM, rig, "m_LocalPosition.x", 2)]),
    ])
    add(scene_path, scene.encode(), f"fileFormatVersion: 2\nguid: {guid_of(scene_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")
    write_package(out, entries)


if __name__ == "__main__":
    main()
