"""Unity の Mesh（.asset と、prefab に埋め込んだもの）だけを使う合成 unitypackage を生成する（Issue #216）。

    blender -b --factory-startup --python tests/make_synthetic_mesh_package.py -- <出力パス.unitypackage>

モデルファイルは入れない。実在アセットのデータは使わない。

- Assets/Synthetic/Meshes/Wedge.asset: Unity の Quad と同じ形の Mesh（サブメッシュ 2 つ）。Prefabs/Wedge.prefab の Wedge
  （位置 (0, 1, 0)）が MeshMat.mat と AssetMat.asset（マテリアルを .asset に入れたもの）で使う。
- Prefabs/Embedded.prefab: prefab の中に埋め込んだ同じ形の Mesh を、Panel が MeshMat で使う（UModeler の prefab と同じ形）。
- Scenes/Meshes.unity: Wedge.prefab を X に 5、Embedded.prefab を Z に 5 動かして置き、Direct が Wedge.asset を 2 倍で使う。
  Direct のマテリアルは PsdMat.mat（Textures/PsdColor.psd。統合した画像を持つ PSD）と BrokenPsdMat.mat
  （Textures/Broken.psd。ヘッダーだけで画素の無い PSD。読めずに警告になる）（#238）。
  Leaf が Wedge.asset を LeafMat.mat で使う。LeafMat のシェーダー Shaders/Leaf.shader は Amplify Shader Editor の URP テンプレートと
  同じ形で、タグは Opaque だがパスで常に切り抜き、白い _EmissionColor を隠して宣言する（#235 / #241）。
  Toon が Wedge.asset を ToonMat.mat で使う。ToonMat のシェーダー Shaders/Toon.shader は Toon Harbor Pack の TH_CustomToon と同じ形で、
  色を _TextureSample1、陰の段階を _TextureRamp に持つ（名前では色が分からない。#239）。
  ShadowPlane は Wedge.asset を Shadows Only（m_CastShadows: 3）の Renderer で使う（影だけを落とし、本体は描かない。#232）。
- Models/Other.fbx（ノード Strip.001 だけ）と Models/Strips.fbx（最上位のノード Strip / Strip.001 / Strip.002。Strip.001 はノード自体が
  Z に 3 動いている）。Prefabs/StripB.prefab は Strips.fbx を展開して Strip.001 だけを使う（GameObject も Strip.001）。シーンでは
  先に Other.fbx を置くので、Strips.fbx を読むときには Strip.001 という名前が使われている（Blender が付け替える。#237）。
  StripB は X に 8 動かして置く。
- Models/Crate.fbx: .mat の無いマテリアル CrateFbx が、作者の PC のパス（/nonexistent/author/Textures/CrateColor.png）の画像を使う。
  同じファイル名の画像は Textures/CrateColor.png にある（#220）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tempfile  # noqa: E402

import bpy  # noqa: E402

from tests.make_synthetic_package import guid_of, mat_yaml, model_meta, png_bytes, prefab_meta, shader_meta, texture_meta, write_package  # noqa: E402
from tests.test_shader_source import AMPLIFY, TOON  # noqa: E402
from tests.test_unity_mesh import QUAD, QUAD_NORMALS, QUAD_TRIS, QUAD_UVS  # noqa: E402
from tests.unity_mesh_writer import mesh_document  # noqa: E402
from tests.unity_yaml_writer import HEADER, ROOT_TRANSFORM, game_object, instance, mod, transform  # noqa: E402
from unitypackage_loader.core.unity_ids import mesh_file_id  # noqa: E402

EMBEDDED_MESH_ID = 5000


def psd_bytes(size: int, rgb: tuple[int, int, int]) -> bytes:
    """8 bit の RGB で、統合した画像（無圧縮）だけを持つ PSD。レイヤーは無い。"""
    import struct

    header = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, size, size, 8, 3)
    sections = struct.pack(">III", 0, 0, 0)  # カラーモード・画像リソース・レイヤーとマスクの長さ
    planes = b"".join(bytes([value]) * (size * size) for value in rgb)  # チャンネルごとの面
    return header + sections + struct.pack(">H", 0) + planes


def _renderer(filter_id: int, renderer_id: int, go: int, mesh_ref: str, materials: list[str]) -> str:
    mats = "".join(f"  - {{fileID: 2100000, guid: {m}, type: 2}}\n" for m in materials)
    return (
        f"--- !u!33 &{filter_id}\nMeshFilter:\n  m_GameObject: {{fileID: {go}}}\n  m_Mesh: {mesh_ref}\n"
        f"--- !u!23 &{renderer_id}\nMeshRenderer:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n  m_Materials:\n{mats}"
    )


def export_crate_fbx(path: Path) -> None:
    """マテリアルの画像が作者の PC のパスを指す FBX（#220）。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.active_object
    obj.name = obj.data.name = "Crate"
    material = bpy.data.materials.new("CrateFbx")
    material.use_nodes = True
    image = bpy.data.images.new("CrateColor.png", 4, 4)
    image.source = "FILE"
    image.filepath = "/nonexistent/author/Textures/CrateColor.png"
    tex = material.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    bsdf = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    material.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    obj.data.materials.append(material)
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False, bake_anim=False, path_mode="ABSOLUTE")


def export_strips_fbx(path: Path, names: list[tuple[str, tuple[float, float, float]]]) -> None:
    """ノード名が Blender の連番の形をした板だけの FBX（#237）。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for name, location in names:
        bpy.ops.mesh.primitive_plane_add(size=1.0, location=location)
        obj = bpy.context.active_object
        obj.name = obj.data.name = name
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False, bake_anim=False)


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else REPO_ROOT / "_local" / "unitypackages" / "synthetic_mesh.unitypackage"
    entries: dict[str, tuple[str, bytes | None, str | None]] = {}

    def add(pathname: str, asset: bytes | None, meta: str | None) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    def native_meta(path: str, main_id: int) -> str:
        return f"fileFormatVersion: 2\nguid: {guid_of(path)}\nNativeFormatImporter:\n  mainObjectFileID: {main_id}\n"

    tex_path = "Assets/Synthetic/Textures/MeshColor.png"
    tex = add(tex_path, png_bytes(8, (80, 160, 220)), texture_meta(guid_of(tex_path), False))
    mat_path = "Assets/Synthetic/Materials/MeshMat.mat"
    mesh_mat = add(mat_path, mat_yaml("MeshMat", tex, None, (1, 1, 1), 0).encode(), native_meta(mat_path, 2100000))
    asset_mat_path = "Assets/Synthetic/Materials/AssetMat.asset"
    asset_mat = add(asset_mat_path, mat_yaml("AssetMat", tex, None, (1, 0.2, 0.2), 0).encode(), native_meta(asset_mat_path, 2100000))

    wedge_path = "Assets/Synthetic/Meshes/Wedge.asset"
    wedge = add(
        wedge_path,
        (HEADER + mesh_document(4300000, "Wedge", QUAD, [QUAD_TRIS[:3], QUAD_TRIS[3:]], normals=QUAD_NORMALS, uvs=QUAD_UVS)).encode(),
        native_meta(wedge_path, 4300000),
    )
    wedge_ref = f"{{fileID: 4300000, guid: {wedge}, type: 2}}"

    wedge_prefab_path = "Assets/Synthetic/Prefabs/Wedge.prefab"
    wedge_prefab = add(wedge_prefab_path, (HEADER + "".join([
        game_object(10, "Wedge"), transform(11, 10, pos=(0, 1, 0)), _renderer(12, 13, 10, wedge_ref, [mesh_mat, asset_mat]),
    ])).encode(), prefab_meta(wedge_prefab_path))

    embedded_path = "Assets/Synthetic/Prefabs/Embedded.prefab"
    embedded = add(embedded_path, (HEADER + "".join([
        game_object(10, "Panel"), transform(11, 10), _renderer(12, 13, 10, f"{{fileID: {EMBEDDED_MESH_ID}}}", [mesh_mat]),
        mesh_document(EMBEDDED_MESH_ID, "um_panel", QUAD, [QUAD_TRIS], normals=QUAD_NORMALS, uvs=QUAD_UVS),
    ])).encode(), prefab_meta(embedded_path))

    psd_path = "Assets/Synthetic/Textures/PsdColor.psd"
    psd = add(psd_path, psd_bytes(8, (220, 120, 40)), texture_meta(guid_of(psd_path), False))
    psd_mat_path = "Assets/Synthetic/Materials/PsdMat.mat"
    psd_mat = add(psd_mat_path, mat_yaml("PsdMat", psd, None, (1, 1, 1), 0).encode(), native_meta(psd_mat_path, 2100000))
    broken_path = "Assets/Synthetic/Textures/Broken.psd"
    broken = add(broken_path, psd_bytes(8, (0, 0, 0))[:26], texture_meta(guid_of(broken_path), False))
    broken_mat_path = "Assets/Synthetic/Materials/BrokenPsdMat.mat"
    broken_mat = add(broken_mat_path, mat_yaml("BrokenPsdMat", broken, None, (1, 1, 1), 0).encode(), native_meta(broken_mat_path, 2100000))

    leaf_shader_path = "Assets/Synthetic/Shaders/Leaf.shader"
    leaf_shader = add(leaf_shader_path, AMPLIFY.replace("ALPHA_TEST_LINE", "#define _ALPHATEST_ON 1").encode(), shader_meta(leaf_shader_path))
    leaf_mat_path = "Assets/Synthetic/Materials/LeafMat.mat"
    leaf_yaml = mat_yaml("LeafMat", tex, None, (1, 1, 1), 0).replace(
        "m_Shader: {fileID: 46, guid: 0000000000000000f000000000000000, type: 0}",
        f"m_Shader: {{fileID: 4800000, guid: {leaf_shader}, type: 3}}",
    ).replace("    - _Cutoff: 0.5\n", "    - _AlphaCutoff: 0.679\n").replace(
        "_EmissionColor: {r: 0, g: 0, b: 0, a: 1}", "_EmissionColor: {r: 1, g: 1, b: 1, a: 1}"
    )
    leaf_mat = add(leaf_mat_path, leaf_yaml.encode(), native_meta(leaf_mat_path, 2100000))

    toon_shader_path = "Assets/Synthetic/Shaders/Toon.shader"
    toon_shader = add(toon_shader_path, TOON.encode(), shader_meta(toon_shader_path))
    ramp_path = "Assets/Synthetic/Textures/ToonRamp.png"
    ramp = add(ramp_path, png_bytes(4, (40, 40, 40)), texture_meta(guid_of(ramp_path), False))
    toon_mat_path = "Assets/Synthetic/Materials/ToonMat.mat"
    toon_yaml = mat_yaml("ToonMat", tex, None, (1, 1, 1), 0).replace(
        "m_Shader: {fileID: 46, guid: 0000000000000000f000000000000000, type: 0}",
        f"m_Shader: {{fileID: 4800000, guid: {toon_shader}, type: 3}}",
    ).replace("    - _MainTex:", "    - _TextureSample1:").replace(
        "    - _BumpMap:", f"    - _TextureRamp:\n        m_Texture: {{fileID: 2800000, guid: {ramp}, type: 3}}\n"
        "        m_Scale: {x: 1, y: 1}\n        m_Offset: {x: 0, y: 0}\n    - _BumpMap:",
    )
    toon_mat = add(toon_mat_path, toon_yaml.encode(), native_meta(toon_mat_path, 2100000))

    crate_tex_path = "Assets/Synthetic/Textures/CrateColor.png"
    add(crate_tex_path, png_bytes(8, (200, 150, 60)), texture_meta(guid_of(crate_tex_path), False))
    crate_fbx = Path(tempfile.mkdtemp(prefix="synthetic_mesh_")) / "crate.fbx"
    export_crate_fbx(crate_fbx)
    crate_path = "Assets/Synthetic/Models/Crate.fbx"
    add(crate_path, crate_fbx.read_bytes(), model_meta(guid_of(crate_path), {}))

    tmp = Path(tempfile.mkdtemp(prefix="synthetic_strips_"))
    export_strips_fbx(tmp / "other.fbx", [("Strip.001", (0, 0, 0))])
    other_path = "Assets/Synthetic/Models/Other.fbx"
    other = add(other_path, (tmp / "other.fbx").read_bytes(), model_meta(guid_of(other_path), {}))
    export_strips_fbx(tmp / "strips.fbx", [("Strip", (-2, 0, 0)), ("Strip.001", (0, 0, 3)), ("Strip.002", (2, 0, 0))])
    strips_path = "Assets/Synthetic/Models/Strips.fbx"
    strips = add(strips_path, (tmp / "strips.fbx").read_bytes(), model_meta(guid_of(strips_path), {}))
    strip_prefab_path = "Assets/Synthetic/Prefabs/StripB.prefab"
    strip_ref = f"{{fileID: {mesh_file_id('Strip.001')}, guid: {strips}, type: 3}}"
    strip_prefab = add(strip_prefab_path, (HEADER + "".join([
        game_object(10, "Strip.001"), transform(11, 10), _renderer(12, 13, 10, strip_ref, [mesh_mat]),
    ])).encode(), prefab_meta(strip_prefab_path))

    scene_path = "Assets/Synthetic/Scenes/Meshes.unity"
    scene = HEADER + "".join([
        instance(700, other, 0, [mod(ROOT_TRANSFORM, other, "m_LocalPosition.x", -8)]),
        instance(800, strip_prefab, 0, [mod(11, strip_prefab, "m_LocalPosition.x", 8)]),
        instance(100, wedge_prefab, 0, [mod(11, wedge_prefab, "m_LocalPosition.x", 5)]),
        instance(200, embedded, 0, [mod(11, embedded, "m_LocalPosition.z", 5)]),
        game_object(600, "ShadowPlane"), transform(601, 600, pos=(-5, 0, -5)),
        _renderer(602, 603, 600, wedge_ref, [mesh_mat, mesh_mat]).replace("  m_Enabled: 1\n", "  m_Enabled: 1\n  m_CastShadows: 3\n"),
        game_object(500, "Toon"), transform(501, 500, pos=(5, 0, -5)), _renderer(502, 503, 500, wedge_ref, [toon_mat, toon_mat]),
        game_object(400, "Leaf"), transform(401, 400, pos=(0, 0, -5)), _renderer(402, 403, 400, wedge_ref, [leaf_mat, leaf_mat]),
        game_object(300, "Direct"), transform(301, 300, scale=(2, 2, 2)), _renderer(302, 303, 300, wedge_ref, [psd_mat, broken_mat]),
    ])
    add(scene_path, scene.encode(), f"fileFormatVersion: 2\nguid: {guid_of(scene_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")
    write_package(out, entries)


if __name__ == "__main__":
    main()
