"""シーン（.unity）の読み込みを確かめる合成 unitypackage を Blender で生成する（Issue #48）。

    blender -b --factory-startup --python tests/make_synthetic_scene_package.py -- <出力パス.unitypackage>

モデル Probe.fbx は、どのメッシュも立方体の 1 頂点だけを引き出した「突起」を持つ（重心から最も遠い頂点が一意）。

- Spike: 位置 (0.5, 0, 0)、Z 30 度
- Group（Empty、位置 (0, 1, 0)、X 20 度）の子の Child: 位置 (0, 0, 0.5)、Y 15 度、スケール 0.5
- Armature（位置 (0, -1, 0)）の Hips にスキンした Skinned

Assets/Synthetic/Scenes/Probe.unity に 4 通りに置く。値は、同じ形の FBX を Unity 6 で置いたシーンの保存内容に合わせてあり、
期待値（tests/expectations_synthetic_scene.json）の頂点座標は、そのシーンで Unity が書き出したワールド座標を Blender の座標に直したもの。

- A: FBX を中に置いた prefab（Nested。FBX に位置 0.3・Y 45 度・スケール 2）を、位置 (1, 2, 3)・euler (10, 20, 30) に
- Holder（位置 (-2, 0, 1)・Y 90 度・スケール 1.5）の下の B: FBX を位置 (0, 1, 0)・Z 45 度で直置き
- C: FBX を展開した prefab（Unpacked。Transform の値は Unity が展開したときのもの）を、位置 (0, 0, -3)・Y 180 度・
  スケール (1, 2, 1) に。中の Child の位置を上書きし、Child のマテリアルを ProbeRed に差し替える
- 非アクティブな Hidden の下に FBX を直置き（読み込んで非表示にする）。ライト 1 つ
- SlabRoot: 1 メッシュの FBX（Slab.fbx。ノードは原点から離れている）のメッシュを直接指す prefab を、位置 (3, 1, 14)・
  Y 45 度に（Issue #98。Unity はノードの変換をモデルのルートの Transform に載せるので、メッシュ参照には掛からない）
- SlabInstance: 同じ FBX をモデルの PrefabInstance で、位置 (2, 0.5, -6)・Y 30 度に。スケールは上書きせず、FBX の
  ノードの値のままにする（Issue #99。Unity のルートの Transform はノードの変換に上書きを当てたもの）
- SlabBareInstance: 同じ FBX を表の無い .meta で別のモデル（SlabBare.fbx）にし、SlabInstance から Unity の X に 10 ずらして
  同じ向きで置く（Issue #111。表が無くても、読み込んだオブジェクトが 1 つなら畳まれた形として組み立て直す）
- SlabBareWrapped / SlabWrapped: 1 メッシュの FBX（表なし / 表あり）をルートの位置 0・回転なしで置いた prefab を、さらに
  Unity の X に 20 / 30 ずらして SlabInstance と同じ向きで置く（Issue #111。包んだ prefab の上からの上書きも畳まれたノードに当てる）
- TowerRoot: LOD を 2 段持つ FBX（Tower.fbx）の LODGroup を持つ prefab を、位置 (-6, 0, 0) に（Issue #104。
  Tower_LOD1 は読み込んだうえで非表示にする）
- KioskRoot: Renderer が 1 つの FBX（Kiosk.fbx。表も externalObjects も無い 2 マテリアルのメッシュ）をそのまま置いた
  prefab（Kiosk.prefab）を、位置 (8, 0, 8) に。prefab はモデルの中の Renderer の m_Materials[0] だけを ProbeRed に
  上書きする。表が無いので名前は引けないが、Renderer が 1 つなので対象は確定する（Issue #109）
- BoothRoot: メッシュが 2 つの FBX（Booth.fbx）をそのまま置いた prefab（Booth.prefab）を、位置 (-8, 0, 8) に。
  モデルの中の Renderer 1 つへのマテリアルの上書きは、どちらのメッシュか決められないので当てずに警告する（Issue #31）

Assets/Synthetic/Scenes/Menu.unity は UI（RectTransform）だけのシーン（候補には読み込めない理由付きで残る）。
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.make_synthetic_package import guid_of, mat_yaml, model_meta, png_bytes, prefab_meta, texture_meta, write_package  # noqa: E402
from unitypackage_loader.core.unity_ids import mesh_file_id  # noqa: E402

ROOT_GAME_OBJECT = 919132149155446097
ROOT_TRANSFORM = -8679921383154817045
HEADER = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"


def export_probe_fbx(path: Path) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    material = bpy.data.materials.new("ProbeMat")

    def spike(name, location=(0, 0, 0), rotation=(0, 0, 0), scale=(1, 1, 1), spike_to=(1.8, 1.2, 0.9)):
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        obj = bpy.context.active_object
        obj.name = name
        obj.data.name = f"{name}_Mesh"
        corner = max(obj.data.vertices, key=lambda v: (v.co.x, v.co.y, v.co.z))
        corner.co = spike_to
        obj.location = location
        obj.rotation_euler = [math.radians(a) for a in rotation]
        obj.scale = scale
        obj.data.materials.append(material)
        return obj

    spike("Spike", location=(0.5, 0, 0), rotation=(0, 0, 30))
    group = bpy.data.objects.new("Group", None)
    bpy.context.collection.objects.link(group)
    group.location = (0, 1, 0)
    group.rotation_euler = (math.radians(20), 0, 0)
    child = spike("Child", location=(0, 0, 0.5), rotation=(0, 15, 0), scale=(0.5, 0.5, 0.5), spike_to=(-1.5, 0.8, 1.1))
    child.parent = group

    arm_data = bpy.data.armatures.new("ArmatureData")
    arm = bpy.data.objects.new("Armature", arm_data)
    bpy.context.collection.objects.link(arm)
    arm.location = (0, -1, 0)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    hips = arm_data.edit_bones.new("Hips")
    hips.head, hips.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    skinned = spike("Skinned", spike_to=(0.7, -1.6, 1.3))
    skinned.vertex_groups.new(name="Hips").add([v.index for v in skinned.data.vertices], 1.0, "REPLACE")
    skinned.modifiers.new("Armature", "ARMATURE").object = arm
    skinned.parent = arm
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False, bake_anim=False)


def export_slab_fbx(path: Path) -> None:
    """メッシュが 1 つだけで、そのノードが原点から離れている FBX（Issue #98）。

    Unity はこの形の FBX でノードを ``//RootNode`` に畳み、ノードの変換をモデルのルートの Transform に載せる。
    メッシュの頂点はノード空間のままなので、メッシュを直接指す Renderer にはノードの変換が掛からない。
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    material = bpy.data.materials.new("ProbeMat")
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.active_object
    obj.name = "Slab"
    obj.data.name = "Slab_Mesh"
    corner = max(obj.data.vertices, key=lambda v: (v.co.x, v.co.y, v.co.z))
    corner.co = (1.6, 1.1, 0.7)
    obj.location = (0, 4, 0.2)  # Unity では (0, 0.2, -4)
    obj.rotation_euler = (0, 0, math.radians(90))
    obj.data.materials.append(material)
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False, bake_anim=False)


def export_lod_fbx(path: Path) -> None:
    """LOD の段ごとにメッシュを持つ FBX（Tower_LOD0 は突起付き、Tower_LOD1 は素の立方体）。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    material = bpy.data.materials.new("ProbeMat")
    for name, tip in (("Tower_LOD0", (1.7, 0.6, 1.3)), ("Tower_LOD1", None)):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
        obj = bpy.context.active_object
        obj.name = name
        obj.data.name = name
        if tip is not None:
            max(obj.data.vertices, key=lambda v: (v.co.x, v.co.y, v.co.z)).co = tip
        obj.data.materials.append(material)
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False, bake_anim=False)


def game_object(file_id, name, active=1):
    return f"--- !u!1 &{file_id}\nGameObject:\n  m_Name: {name}\n  m_IsActive: {active}\n"


def transform(file_id, go, father=0, pos=(0, 0, 0), rot=(0, 0, 0, 1), scale=(1, 1, 1), class_id=4, kind="Transform"):
    return (
        f"--- !u!{class_id} &{file_id}\n{kind}:\n  m_GameObject: {{fileID: {go}}}\n"
        f"  m_LocalRotation: {{x: {rot[0]}, y: {rot[1]}, z: {rot[2]}, w: {rot[3]}}}\n"
        f"  m_LocalPosition: {{x: {pos[0]}, y: {pos[1]}, z: {pos[2]}}}\n"
        f"  m_LocalScale: {{x: {scale[0]}, y: {scale[1]}, z: {scale[2]}}}\n"
        f"  m_Father: {{fileID: {father}}}\n"
    )


def mesh_renderer(filter_id, renderer_id, go, model_guid, mat_guid, mesh_file_id=4300000):
    return (
        f"--- !u!33 &{filter_id}\nMeshFilter:\n  m_GameObject: {{fileID: {go}}}\n"
        f"  m_Mesh: {{fileID: {mesh_file_id}, guid: {model_guid}, type: 3}}\n"
        f"--- !u!23 &{renderer_id}\nMeshRenderer:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n"
        f"  m_Materials:\n  - {{fileID: 2100000, guid: {mat_guid}, type: 2}}\n"
    )


def lod_group(file_id, go, levels, enabled=1):
    """LODGroup（#104）。``levels`` は段の順に並べた Renderer の fileID。"""
    body = ""
    for index, renderers in enumerate(levels):
        body += f"  - screenRelativeHeight: {round(0.5 / (index + 1), 4)}\n    fadeTransitionWidth: 0\n    renderers:\n"
        body += "".join(f"    - renderer: {{fileID: {r}}}\n" for r in renderers)
    return (
        f"--- !u!205 &{file_id}\nLODGroup:\n  m_GameObject: {{fileID: {go}}}\n  serializedVersion: 2\n"
        f"  m_LocalReferencePoint: {{x: 0, y: 0, z: 0}}\n  m_Size: 2\n  m_FadeMode: 0\n  m_AnimateCrossFading: 0\n"
        f"  m_LastLODIsBillboard: 0\n  m_LODs:\n{body}  m_Enabled: {enabled}\n"
    )


def skinned_renderer(renderer_id, go, model_guid, mat_guid, mesh_file_id=4300000):
    return (
        f"--- !u!137 &{renderer_id}\nSkinnedMeshRenderer:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n"
        f"  m_Materials:\n  - {{fileID: 2100000, guid: {mat_guid}, type: 2}}\n"
        f"  m_Mesh: {{fileID: {mesh_file_id}, guid: {model_guid}, type: 3}}\n"
    )


# Unity 6 が Probe.fbx と同じ形の FBX を展開した prefab で、MeshFilter / SkinnedMeshRenderer の m_Mesh に付けた fileID
# （xxHash64("Type:Mesh-><名前>0")）
MESH_IDS = {"Child": -7630113837697264728, "Spike": 9188603553798411242, "Skinned": 2907845800280414690}


def light_doc(file_id, go, light_type, intensity=1, light_range=10, spot=30, inner=21.80208, color=(1, 1, 1), enabled=1,
              shadow=2, lightmapping=4, area=(1, 1), use_temperature=0, temperature=6570):
    return (
        f"--- !u!108 &{file_id}\nLight:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: {enabled}\n  serializedVersion: 10\n"
        f"  m_Type: {light_type}\n  m_Color: {{r: {color[0]}, g: {color[1]}, b: {color[2]}, a: 1}}\n"
        f"  m_Intensity: {intensity}\n  m_Range: {light_range}\n  m_SpotAngle: {spot}\n  m_InnerSpotAngle: {inner}\n"
        f"  m_Shadows:\n    m_Type: {shadow}\n  m_Lightmapping: {lightmapping}\n  m_AreaSize: {{x: {area[0]}, y: {area[1]}}}\n"
        f"  m_ColorTemperature: {temperature}\n  m_UseColorTemperature: {use_temperature}\n  m_ShadowRadius: 0\n  m_ShadowAngle: 0\n"
    )


def camera_doc(file_id, go, fov=60, orthographic=0, size=5, near=0.3, far=1000, mode=1, focal=50, sensor=(36, 24), gate=2):
    return (
        f"--- !u!20 &{file_id}\nCamera:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n  serializedVersion: 2\n"
        f"  m_projectionMatrixMode: {mode}\n  m_GateFitMode: {gate}\n  m_FocalLength: {focal}\n"
        f"  m_SensorSize: {{x: {sensor[0]}, y: {sensor[1]}}}\n  m_LensShift: {{x: 0, y: 0}}\n"
        f"  near clip plane: {near}\n  far clip plane: {far}\n  field of view: {fov}\n"
        f"  orthographic: {orthographic}\n  orthographic size: {size}\n"
    )


def export_kiosk_fbx(path: Path) -> None:
    """メッシュが 1 つで、2 つのマテリアルをポリゴンの前半・後半に使う FBX（Issue #109）。

    マテリアルの名前は .mat と一致させない（externalObjects も無いので、prefab の上書きを読まないと割り当てが無い）。
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.active_object
    obj.name = "Kiosk"
    obj.data.name = "Kiosk"
    obj.data.materials.append(bpy.data.materials.new("KioskTileFbx"))
    obj.data.materials.append(bpy.data.materials.new("KioskFrameFbx"))
    polygons = obj.data.polygons
    for polygon in polygons[len(polygons) // 2 :]:
        polygon.material_index = 1
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False, bake_anim=False)


def export_booth_fbx(path: Path) -> None:
    """メッシュが 2 つの FBX（Issue #31。表が無いと、中の Renderer への上書きの対象を決められない）。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    material = bpy.data.materials.new("BoothFbx")
    for name, z in (("BoothWall", 0.0), ("BoothRoof", 1.5)):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, z))
        obj = bpy.context.active_object
        obj.name = name
        obj.data.name = name
        obj.data.materials.append(material)
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False, bake_anim=False)


# Renderer の fileID は階層パスのハッシュで、計算式は分かっていない（Issue #31）。値は Shibuya.unitypackage の事例のもの
KIOSK_RENDERER = -7511558181221131132
BOOTH_RENDERER = 3713382542316546517


def mod(target, guid, path, value="", reference="{fileID: 0}"):
    return (
        f"    - target: {{fileID: {target}, guid: {guid}, type: 3}}\n"
        f"      propertyPath: {path}\n      value: {value}\n      objectReference: {reference}\n"
    )


def instance(file_id, source, parent, mods):
    return (
        f"--- !u!1001 &{file_id}\nPrefabInstance:\n  m_Modification:\n"
        f"    m_TransformParent: {{fileID: {parent}}}\n    m_Modifications:\n{''.join(mods)}"
        f"    m_RemovedComponents: []\n    m_RemovedGameObjects: []\n"
        f"  m_SourcePrefab: {{fileID: 100100000, guid: {source}, type: 3}}\n"
    )


def stripped_transform(file_id, source_file_id, source_guid, instance_id):
    return (
        f"--- !u!4 &{file_id} stripped\nTransform:\n"
        f"  m_CorrespondingSourceObject: {{fileID: {source_file_id}, guid: {source_guid}, type: 3}}\n"
        f"  m_PrefabInstance: {{fileID: {instance_id}}}\n"
    )


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else REPO_ROOT / "_local" / "unitypackages" / "synthetic_scene.unitypackage"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="synthetic_scene_"))
    entries: dict[str, tuple[str, bytes | None, str | None]] = {}

    def add(pathname: str, asset: bytes | None, meta: str | None) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    tex_path = "Assets/Synthetic/Textures/ColorA.png"
    tex = add(tex_path, png_bytes(8, (200, 80, 80)), texture_meta(guid_of(tex_path), False))
    mats = {}
    for name, color in (("ProbeMat", (1, 1, 1)), ("ProbeRed", (1, 0.2, 0.2))):
        mat_path = f"Assets/Synthetic/Materials/{name}.mat"
        mats[name] = add(mat_path, mat_yaml(name, tex, None, color, 0).encode(),
                         f"fileFormatVersion: 2\nguid: {guid_of(mat_path)}\nNativeFormatImporter:\n  mainObjectFileID: 2100000\n")

    fbx = tmp / "probe.fbx"
    export_probe_fbx(fbx)
    model_path = "Assets/Synthetic/Models/Probe.fbx"
    # 古い形式（Unity 2018.2 以前）の表。新しい形式のシーンは中への上書きを持たないので、表があっても結果は変わらない
    recycle = "\n".join(f"    {k}: {v}" for k, v in (
        (100000, "//RootNode"), (100002, "Spike"), (100004, "Group"), (100006, "Child"),
        (400000, "//RootNode"), (400002, "Spike"), (400004, "Group"), (400006, "Child"),
        (2300000, "Spike"), (2300002, "Child"), (4300000, "Spike"), (4300002, "Child"),
    ))
    meta = model_meta(guid_of(model_path), {"ProbeMat": mats["ProbeMat"]}).replace(
        "ModelImporter:\n", f"ModelImporter:\n  fileIDToRecycleName:\n{recycle}\n", 1
    )
    model = add(model_path, fbx.read_bytes(), meta)

    # 1 メッシュの FBX（#98）。表は新しい Unity が書く internalIDToNameTable の形で、GameObject はルートだけ
    slab_fbx = tmp / "slab.fbx"
    export_slab_fbx(slab_fbx)
    slab_path = "Assets/Synthetic/Models/Slab.fbx"
    slab_table = "".join(
        f"  - first:\n      {class_id}: {file_id}\n    second: {name}\n" for class_id, file_id, name in (
            (1, 100000, "//RootNode"), (4, 400000, "//RootNode"), (23, 2300000, "//RootNode"),
            (33, 3300000, "//RootNode"), (43, 4300000, "Slab"),
        )
    )
    slab_meta = model_meta(guid_of(slab_path), {"ProbeMat": mats["ProbeMat"]}).replace(
        "ModelImporter:\n", f"ModelImporter:\n  internalIDToNameTable:\n{slab_table}", 1
    )
    slab_model = add(slab_path, slab_fbx.read_bytes(), slab_meta)

    # 同じ FBX を、表の無い新しい形式の .meta で別のモデルとして置く（#111: 表が無いと 1 メッシュの FBX か分からない）
    bare_path = "Assets/Synthetic/Models/SlabBare.fbx"
    bare_model = add(bare_path, slab_fbx.read_bytes(), model_meta(guid_of(bare_path), {"ProbeMat": mats["ProbeMat"]}))

    # 1 メッシュの FBX をそのまま置き、ルートの位置 0・回転なしに上書きした prefab（#111: Shibuya の地下鉄の入口と同じ形）。
    # 表の無いモデル（SlabBare）と表のあるモデル（Slab）で 1 つずつ作る
    wrapped = {}
    for label, wrapped_model in (("SlabBareWrapped", bare_model), ("SlabWrapped", slab_model)):
        wrapped_path = f"Assets/Synthetic/Prefabs/{label}.prefab"
        wrapped[label] = add(wrapped_path, (HEADER + "".join([
            instance(100, wrapped_model, 0, [
                mod(ROOT_TRANSFORM, wrapped_model, f"m_LocalPosition.{axis}", 0) for axis in "xyz"
            ] + [
                mod(ROOT_TRANSFORM, wrapped_model, f"m_LocalRotation.{axis}", value) for axis, value in zip("xyzw", (0, 0, 0, 1))
            ] + [mod(ROOT_GAME_OBJECT, wrapped_model, "m_Name", label)]),
            stripped_transform(ROOT_TRANSFORM ^ 100, ROOT_TRANSFORM, wrapped_model, 100),
        ])).encode(), prefab_meta(wrapped_path))

    # Unity で、メッシュを直接指す GameObject だけの prefab（街並みアセットの歩道と同じ形）
    slab_prefab_path = "Assets/Synthetic/Prefabs/Slab.prefab"
    slab_prefab = add(slab_prefab_path, (HEADER + "".join([
        game_object(10, "Slab"),
        transform(11, 10),
        mesh_renderer(12, 13, 10, slab_model, mats["ProbeMat"], 4300000),
    ])).encode(), prefab_meta(slab_prefab_path))

    # FBX を展開した prefab。Transform の値は Unity が展開したときのもの（ルート直下は X -90 度・スケール 100）
    def unpacked_prefab(name, mat_guid, spike_only=False):
        # spike_only: FBX を展開したあと Child と Skinned の Renderer を消した prefab（#58: Unity では Spike しか表示されない）
        child_renderer = "" if spike_only else mesh_renderer(302, 303, 300, model, mat_guid, MESH_IDS["Child"])
        skinned = "" if spike_only else skinned_renderer(702, 700, model, mat_guid, MESH_IDS["Skinned"])
        return (HEADER + "".join([
            game_object(100, name),
            transform(101, 100),
            game_object(200, "Group"),
            transform(201, 200, father=101, pos=(0, 0, -1), rot=(-0.5735765, 0, 0, 0.819152), scale=(100, 99.99999, 99.99999)),
            game_object(300, "Child"),
            transform(301, 300, father=201, pos=(0, 0, 0.0050000004), rot=(0, -0.13052621, 0, 0.9914449), scale=(0.5, 0.50000006, 0.50000006)),
            child_renderer,
            game_object(400, "Spike"),
            transform(401, 400, father=101, pos=(-0.5, 0, 0), rot=(-0.68301266, -0.18301271, -0.18301271, 0.6830127), scale=(100, 100, 100)),
            mesh_renderer(402, 403, 400, model, mat_guid, MESH_IDS["Spike"]),
            game_object(500, "Armature"),
            transform(501, 500, father=101, pos=(0, 0, 1), rot=(-0.7071068, 0, 0, 0.7071068), scale=(100, 100, 100)),
            game_object(600, "Hips"),
            transform(601, 600, father=501, rot=(0.7071068, 0, 0, 0.7071068)),
            game_object(700, "Skinned"),
            transform(701, 700, father=101, pos=(0, 0, 1), rot=(-0.7071068, 0, 0, 0.7071068), scale=(100, 100, 100)),
            skinned,
        ])).encode()

    unpacked_path = "Assets/Synthetic/Prefabs/Unpacked.prefab"
    unpacked = add(unpacked_path, unpacked_prefab("Unpacked", mats["ProbeMat"]), prefab_meta(unpacked_path))
    # すべての Renderer が ProbeRed の色違い。シーンで最初に読み込ませ、FBX のマテリアル（ProbeMat）が差し替えで
    # 使われなくなって削除された後に、同じモデルを読み直す配置（A 以降）で ProbeMat を組み直せることを確かめる
    all_red_path = "Assets/Synthetic/Prefabs/AllRed.prefab"
    all_red = add(all_red_path, unpacked_prefab("AllRed", mats["ProbeRed"]), prefab_meta(all_red_path))
    partial_path = "Assets/Synthetic/Prefabs/Partial.prefab"
    partial = add(partial_path, unpacked_prefab("Partial", mats["ProbeMat"], spike_only=True), prefab_meta(partial_path))

    # FBX を中に置いた prefab
    nested_path = "Assets/Synthetic/Prefabs/Nested.prefab"
    nested = add(nested_path, (HEADER + "".join([
        game_object(10, "Nested"),
        transform(11, 10),
        instance(20, model, 11, [
            mod(ROOT_TRANSFORM, model, "m_LocalPosition.x", 0.3),
            mod(ROOT_TRANSFORM, model, "m_LocalRotation.y", 0.38268346),
            mod(ROOT_TRANSFORM, model, "m_LocalRotation.w", 0.9238795),
            mod(ROOT_TRANSFORM, model, "m_LocalScale.x", 2),
            mod(ROOT_TRANSFORM, model, "m_LocalScale.y", 2),
            mod(ROOT_TRANSFORM, model, "m_LocalScale.z", 2),
        ]),
        stripped_transform(21, ROOT_TRANSFORM, model, 20),
    ])).encode(), prefab_meta(nested_path))

    # LOD の 2 段を持つ FBX と、その LODGroup を持つ prefab（#104）
    lod_fbx = tmp / "tower.fbx"
    export_lod_fbx(lod_fbx)
    lod_model_path = "Assets/Synthetic/Models/Tower.fbx"
    lod_model = add(lod_model_path, lod_fbx.read_bytes(), model_meta(guid_of(lod_model_path), {"ProbeMat": mats["ProbeMat"]}))
    lod_prefab_path = "Assets/Synthetic/Prefabs/Tower.prefab"
    lod_prefab = add(lod_prefab_path, (HEADER + "".join([
        game_object(10, "Tower"),
        transform(11, 10),
        lod_group(12, 10, [[103], [113]]),
        game_object(100, "Tower_LOD0"),
        transform(101, 100, father=11),
        mesh_renderer(102, 103, 100, lod_model, mats["ProbeMat"], mesh_file_id("Tower_LOD0")),
        game_object(110, "Tower_LOD1"),
        transform(111, 110, father=11),
        mesh_renderer(112, 113, 110, lod_model, mats["ProbeMat"], mesh_file_id("Tower_LOD1")),
    ])).encode(), prefab_meta(lod_prefab_path))

    # Renderer が 1 つの FBX をそのまま置き、中の Renderer の m_Materials[0] だけを上書きした prefab（#109）。
    # .meta は新しい形式で、表も externalObjects も無い
    red = f"{{fileID: 2100000, guid: {mats['ProbeRed']}, type: 2}}"
    fbx_prefabs = {}
    for name, export, renderer in (("Kiosk", export_kiosk_fbx, KIOSK_RENDERER), ("Booth", export_booth_fbx, BOOTH_RENDERER)):
        path = tmp / f"{name.lower()}.fbx"
        export(path)
        fbx_model_path = f"Assets/Synthetic/Models/{name}.fbx"
        fbx_model = add(fbx_model_path, path.read_bytes(), model_meta(guid_of(fbx_model_path), {}))
        fbx_prefab_path = f"Assets/Synthetic/Prefabs/{name}.prefab"
        fbx_prefabs[name] = add(fbx_prefab_path, (HEADER + "".join([
            instance(100, fbx_model, 0, [
                mod(ROOT_GAME_OBJECT, fbx_model, "m_Name", name),
                mod(renderer, fbx_model, "m_Materials.Array.data[0]", "", red),
            ]),
            stripped_transform(ROOT_TRANSFORM ^ 100, ROOT_TRANSFORM, fbx_model, 100),
        ])).encode(), prefab_meta(fbx_prefab_path))

    # 点光源だけの prefab。シーンで強さを上書きして置く
    lamp_path = "Assets/Synthetic/Prefabs/Lamp.prefab"
    lamp = add(lamp_path, (HEADER + game_object(10, "Lamp") + transform(11, 10, pos=(0, 2, 0))
                           + light_doc(12, 10, 2, intensity=1, light_range=4)).encode(), prefab_meta(lamp_path))

    scene_path = "Assets/Synthetic/Scenes/Probe.unity"
    add(scene_path, (HEADER + "".join([
        instance(2500, all_red, 0, [mod(101, all_red, "m_LocalPosition.z", 8)]),
        instance(2600, partial, 0, [mod(101, partial, "m_LocalPosition.z", -12)]),
        instance(3000, nested, 0, [
            mod(11, nested, "m_LocalPosition.x", 1),
            mod(11, nested, "m_LocalPosition.y", 2),
            mod(11, nested, "m_LocalPosition.z", 3),
            mod(11, nested, "m_LocalRotation.x", 0.12767945),
            mod(11, nested, "m_LocalRotation.y", 0.14487813),
            mod(11, nested, "m_LocalRotation.z", 0.23929834),
            mod(11, nested, "m_LocalRotation.w", 0.9515485),
            mod(10, nested, "m_Name", "A"),
        ]),
        game_object(1000, "Holder"),
        transform(1001, 1000, pos=(-2, 0, 1), rot=(0, 0.7071068, 0, 0.7071068), scale=(1.5, 1.5, 1.5)),
        instance(2000, model, 1001, [
            mod(ROOT_TRANSFORM, model, "m_LocalPosition.y", 1),
            mod(ROOT_TRANSFORM, model, "m_LocalRotation.z", 0.38268346),
            mod(ROOT_TRANSFORM, model, "m_LocalRotation.w", 0.9238795),
            mod(ROOT_GAME_OBJECT, model, "m_Name", "B"),
        ]),
        stripped_transform(2001, ROOT_TRANSFORM, model, 2000),
        instance(4000, unpacked, 0, [
            mod(101, unpacked, "m_LocalPosition.z", -3),
            mod(101, unpacked, "m_LocalRotation.y", 1),
            mod(101, unpacked, "m_LocalRotation.w", -0.00000004371139),
            mod(101, unpacked, "m_LocalScale.y", 2),
            mod(301, unpacked, "m_LocalPosition.y", 0.5),
            mod(303, unpacked, "m_Materials.Array.data[0]", "", f"{{fileID: 2100000, guid: {mats['ProbeRed']}, type: 2}}"),
            mod(100, unpacked, "m_Name", "C"),
        ]),
        # 1 メッシュの FBX をメッシュ参照で使う prefab（#98）。Y 45 度で (3, 1, 14) に置く
        instance(5000, slab_prefab, 0, [
            mod(11, slab_prefab, "m_LocalPosition.x", 3),
            mod(11, slab_prefab, "m_LocalPosition.y", 1),
            mod(11, slab_prefab, "m_LocalPosition.z", 14),
            mod(11, slab_prefab, "m_LocalRotation.y", 0.38268346),
            mod(11, slab_prefab, "m_LocalRotation.w", 0.9238795),
            mod(10, slab_prefab, "m_Name", "SlabRoot"),
        ]),
        # 1 メッシュの FBX をモデルの PrefabInstance で置く（#99）。位置と回転だけを上書きし、スケールは
        # FBX のノードの値（Unity では 100）のままにする
        instance(5100, slab_model, 0, [
            mod(ROOT_TRANSFORM, slab_model, "m_LocalPosition.x", 2),
            mod(ROOT_TRANSFORM, slab_model, "m_LocalPosition.y", 0.5),
            mod(ROOT_TRANSFORM, slab_model, "m_LocalPosition.z", -6),
            mod(ROOT_TRANSFORM, slab_model, "m_LocalRotation.x", 0),
            mod(ROOT_TRANSFORM, slab_model, "m_LocalRotation.y", 0.25881905),
            mod(ROOT_TRANSFORM, slab_model, "m_LocalRotation.z", 0),
            mod(ROOT_TRANSFORM, slab_model, "m_LocalRotation.w", 0.96592583),
            mod(ROOT_GAME_OBJECT, slab_model, "m_Name", "SlabInstance"),
        ]),
        stripped_transform(5101, ROOT_TRANSFORM, slab_model, 5100),
        # 表の無い同じ FBX を、SlabInstance から Unity の X に 10 ずらして同じ向きで置く（#111）
        instance(5150, bare_model, 0, [
            mod(ROOT_TRANSFORM, bare_model, "m_LocalPosition.x", 12),
            mod(ROOT_TRANSFORM, bare_model, "m_LocalPosition.y", 0.5),
            mod(ROOT_TRANSFORM, bare_model, "m_LocalPosition.z", -6),
            mod(ROOT_TRANSFORM, bare_model, "m_LocalRotation.x", 0),
            mod(ROOT_TRANSFORM, bare_model, "m_LocalRotation.y", 0.25881905),
            mod(ROOT_TRANSFORM, bare_model, "m_LocalRotation.z", 0),
            mod(ROOT_TRANSFORM, bare_model, "m_LocalRotation.w", 0.96592583),
            mod(ROOT_GAME_OBJECT, bare_model, "m_Name", "SlabBareInstance"),
        ]),
        stripped_transform(5151, ROOT_TRANSFORM, bare_model, 5150),
        # 上の prefab を、さらに Unity の X に 10 / 20 ずらして同じ向きで置く（#111: 包んだ prefab の上からの上書き）
        *[instance(file_id, wrapped[label], 0, [
            mod(ROOT_TRANSFORM ^ 100, wrapped[label], "m_LocalPosition.x", x),
            mod(ROOT_TRANSFORM ^ 100, wrapped[label], "m_LocalPosition.y", 0.5),
            mod(ROOT_TRANSFORM ^ 100, wrapped[label], "m_LocalPosition.z", -6),
            mod(ROOT_TRANSFORM ^ 100, wrapped[label], "m_LocalRotation.y", 0.25881905),
            mod(ROOT_TRANSFORM ^ 100, wrapped[label], "m_LocalRotation.w", 0.96592583),
        ]) for file_id, label, x in ((5160, "SlabBareWrapped", 22), (5170, "SlabWrapped", 32))],
        # LODGroup を持つ prefab（#104）。LOD1 は読み込んで非表示にする
        instance(5200, lod_prefab, 0, [
            mod(11, lod_prefab, "m_LocalPosition.x", -6),
            mod(10, lod_prefab, "m_Name", "TowerRoot"),
        ]),
        # FBX をそのまま置いた prefab の中の Renderer へのマテリアルの上書き（#109 / #31）
        instance(5300, fbx_prefabs["Kiosk"], 0, [
            mod(ROOT_TRANSFORM ^ 100, fbx_prefabs["Kiosk"], "m_LocalPosition.x", 8),
            mod(ROOT_TRANSFORM ^ 100, fbx_prefabs["Kiosk"], "m_LocalPosition.z", 8),
            mod(ROOT_GAME_OBJECT ^ 100, fbx_prefabs["Kiosk"], "m_Name", "KioskRoot"),
        ]),
        instance(5400, fbx_prefabs["Booth"], 0, [
            mod(ROOT_TRANSFORM ^ 100, fbx_prefabs["Booth"], "m_LocalPosition.x", -8),
            mod(ROOT_TRANSFORM ^ 100, fbx_prefabs["Booth"], "m_LocalPosition.z", 8),
            mod(ROOT_GAME_OBJECT ^ 100, fbx_prefabs["Booth"], "m_Name", "BoothRoot"),
        ]),
        game_object(6000, "Hidden", active=0),
        transform(6001, 6000, pos=(6, 0, 0)),
        instance(6002, model, 6001, []),
        # ライト（#49）。Unity の既定の平行光源と同じ向き（euler 50, -30, 0）
        game_object(8000, "Directional Light"),
        transform(8001, 8000, pos=(0, 3, 0), rot=(0.40821788, -0.23456968, 0.10938163, 0.8754261)),
        light_doc(8002, 8000, 1, intensity=2, color=(1, 0.95686275, 0.8392157), shadow=2),
        game_object(8100, "Point Light"),
        transform(8101, 8100, pos=(2, 1, 0)),
        light_doc(8102, 8100, 2, intensity=2, light_range=5, use_temperature=1, temperature=3863),
        game_object(8200, "Spot Light"),  # euler (90, 0, 0): 真下を向く
        transform(8201, 8200, pos=(0, 3, 2), rot=(0.7071068, 0, 0, 0.7071068)),
        light_doc(8202, 8200, 0, intensity=1, light_range=10, spot=60, inner=40),
        game_object(8300, "Area Light"),
        transform(8301, 8300, pos=(0, 2, 0), rot=(0.7071068, 0, 0, 0.7071068)),
        light_doc(8302, 8300, 3, area=(2, 0.5), lightmapping=2, shadow=0),
        game_object(8400, "Disabled Light"),
        transform(8401, 8400, pos=(-3, 1, 0)),
        light_doc(8402, 8400, 2, enabled=0),
        instance(8500, lamp, 0, [
            mod(11, lamp, "m_LocalPosition.x", 4),
            mod(12, lamp, "m_Intensity", 3),
        ]),
        # カメラ。Main Camera はシーンで最初の有効なカメラなので、シーンのカメラになる
        game_object(9000, "Main Camera"),
        transform(9001, 9000, pos=(0, 1, -10)),
        camera_doc(9002, 9000, fov=40, far=200),
        game_object(9100, "Physical Camera"),
        transform(9101, 9100, pos=(5, 1, 0)),
        camera_doc(9102, 9100, mode=2, focal=15.638705, sensor=(36, 24), gate=1),
        game_object(9200, "Ortho Camera"),
        transform(9201, 9200, pos=(0, 10, 0), rot=(0.7071068, 0, 0, 0.7071068)),
        camera_doc(9202, 9200, orthographic=1, size=2.5),
    ])).encode(), f"fileFormatVersion: 2\nguid: {guid_of(scene_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")

    # 古い形式のシーン（#53）。FBX を X に 10 置き、中の Spike の位置 y を 2 に、Child のマテリアルを ProbeRed に上書きする。
    # Spike の元のノードの位置は (-0.5, 0, 0)（Unity 6 で読んだ値）なので、Spike は Unity の +Y（Blender の +Z）に 2 動く
    legacy_path = "Assets/Synthetic/Scenes/Legacy.unity"
    add(legacy_path, (HEADER + (
        "--- !u!1001 &100\nPrefab:\n  serializedVersion: 2\n  m_Modification:\n    m_TransformParent: {fileID: 0}\n"
        "    m_Modifications:\n"
        + mod(400000, model, "m_LocalPosition.x", 10)
        + mod(100000, model, "m_Name", "LegacyProbe")
        + mod(400002, model, "m_LocalPosition.x", -0.5)
        + mod(400002, model, "m_LocalPosition.y", 2)
        + mod(2300002, model, "m_Materials.Array.data[0]", "", f"{{fileID: 2100000, guid: {mats['ProbeRed']}, type: 2}}")
        + "    m_RemovedComponents: []\n"
        f"  m_ParentPrefab: {{fileID: 100100000, guid: {model}, type: 3}}\n  m_IsPrefabParent: 0\n"
        f"--- !u!4 &101 stripped\nTransform:\n  m_PrefabParentObject: {{fileID: 400000, guid: {model}, type: 3}}\n"
        "  m_PrefabInternal: {fileID: 100}\n"
        # FBX から切り離した Spike を、部屋（Room）の下に 2 つ複製して置く（#60: 1 つに潰れていた）。回転・スケールは
        # Unity が読んだ Spike のノードの値のまま、位置だけ変える。Unity の +X に 1.5 / -0.5、+Z に 20 動く
        + game_object(7000, "Room")
        + transform(7001, 7000, pos=(0, 0, 20))
        + game_object(7100, "Spike (1)")
        + transform(7101, 7100, father=7001, pos=(1, 0, 0), rot=(-0.68301266, -0.18301271, -0.18301271, 0.6830127), scale=(100, 100, 100))
        + mesh_renderer(7102, 7103, 7100, model, mats["ProbeMat"], 4300000)
        + game_object(7200, "Spike (2)")
        + transform(7201, 7200, father=7001, pos=(-1, 0, 0), rot=(-0.68301266, -0.18301271, -0.18301271, 0.6830127), scale=(100, 100, 100))
        + mesh_renderer(7202, 7203, 7200, model, mats["ProbeRed"], 4300000)
    )).encode(), f"fileFormatVersion: 2\nguid: {guid_of(legacy_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")

    menu_path = "Assets/Synthetic/Scenes/Menu.unity"
    add(menu_path, (HEADER + game_object(10, "Canvas") + transform(11, 10, class_id=224, kind="RectTransform")
                    + "--- !u!223 &12\nCanvas:\n  m_GameObject: {fileID: 10}\n").encode(),
        f"fileFormatVersion: 2\nguid: {guid_of(menu_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")

    write_package(out, entries)


if __name__ == "__main__":
    main()
