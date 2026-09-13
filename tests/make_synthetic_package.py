"""検証用の合成 unitypackage を Blender で生成する。

実在アセットを使わずにダイアログや複数モデル・非 FBX 形式の動作を確認するためのもの。

    blender -b --factory-startup --python tests/make_synthetic_package.py -- <出力パス.unitypackage>

内容: FBX 3 つ（Cube / Sphere / Cone）、OBJ 1 つ、.blend 1 つ、Standard シェーダーの .mat 3 つ、PNG 2 枚（うち 1 枚は
ノーマルマップ設定）、externalObjects 付きの .meta、Cone と Pair（2 マテリアル。ポリゴンの使用順がスロット順と逆）に
.mat を割り当てる prefab。
"""

from __future__ import annotations

import hashlib
import io
import struct
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path

import bpy


def guid_of(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()


def png_bytes(size: int, rgb: tuple[int, int, int]) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def mat_yaml(name: str, tex_guid: str, normal_guid: str | None, color, mode: int) -> str:
    normal_ref = f"{{fileID: 2800000, guid: {normal_guid}, type: 3}}" if normal_guid else "{fileID: 0}"
    return f"""%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!21 &2100000
Material:
  serializedVersion: 8
  m_Name: {name}
  m_Shader: {{fileID: 46, guid: 0000000000000000f000000000000000, type: 0}}
  m_ValidKeywords: []
  m_CustomRenderQueue: -1
  m_SavedProperties:
    serializedVersion: 3
    m_TexEnvs:
    - _MainTex:
        m_Texture: {{fileID: 2800000, guid: {tex_guid}, type: 3}}
        m_Scale: {{x: 1, y: 1}}
        m_Offset: {{x: 0, y: 0}}
    - _BumpMap:
        m_Texture: {normal_ref}
        m_Scale: {{x: 1, y: 1}}
        m_Offset: {{x: 0, y: 0}}
    m_Floats:
    - _Mode: {mode}
    - _Cutoff: 0.5
    - _Metallic: 0.1
    - _Glossiness: 0.4
    - _BumpScale: 1
    m_Colors:
    - _Color: {{r: {color[0]}, g: {color[1]}, b: {color[2]}, a: 1}}
    - _EmissionColor: {{r: 0, g: 0, b: 0, a: 1}}
"""


def toon_lit_mat_yaml(name: str, tex_guid: str) -> str:
    """VRChat/Mobile/Toon Lit に切り替えた後の典型的な .mat。

    Standard 時代の _Color（黒）と _EmissionColor（白）が残り、_EMISSION は m_InvalidKeywords に移っている。
    Toon Lit は _MainTex しか参照しないので、テクスチャそのままの Unlit として読めなければならない。
    """
    return f"""%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!21 &2100000
Material:
  serializedVersion: 8
  m_Name: {name}
  m_Shader: {{fileID: 4800000, guid: affc81f3d164d734d8f13053effb1c5c, type: 3}}
  m_ValidKeywords: []
  m_InvalidKeywords:
  - _EMISSION
  m_CustomRenderQueue: -1
  m_SavedProperties:
    serializedVersion: 3
    m_TexEnvs:
    - _MainTex:
        m_Texture: {{fileID: 2800000, guid: {tex_guid}, type: 3}}
        m_Scale: {{x: 1, y: 1}}
        m_Offset: {{x: 0, y: 0}}
    - _EmissionMap:
        m_Texture: {{fileID: 0}}
        m_Scale: {{x: 1, y: 1}}
        m_Offset: {{x: 0, y: 0}}
    m_Floats:
    - _Mode: 0
    - _Cutoff: 0.5
    - _Metallic: 0
    - _Glossiness: 0.05
    m_Colors:
    - _Color: {{r: 0, g: 0, b: 0, a: 1}}
    - _EmissionColor: {{r: 1, g: 1, b: 1, a: 1}}
"""


def model_meta(guid: str, materials: dict[str, str]) -> str:
    lines = [f"fileFormatVersion: 2", f"guid: {guid}", "ModelImporter:", "  serializedVersion: 22200", "  externalObjects:"]
    for name, mat_guid in materials.items():
        lines += [
            "  - first:",
            "      type: UnityEngine:Material",
            "      assembly: UnityEngine.CoreModule",
            f"      name: {name}",
            f"    second: {{fileID: 2100000, guid: {mat_guid}, type: 2}}",
        ]
    lines += ["  materials:", "    materialImportMode: 2", "  meshes:", "    globalScale: 1", "    useFileScale: 1"]
    return "\n".join(lines) + "\n"


def prefab_yaml(renderers: list[tuple[str, list[str]]]) -> str:
    """(GameObject 名, m_Materials に並べる .mat GUID) の組から prefab を作る。m_Materials は Unity のサブメッシュ順。"""
    lines = ["%YAML 1.1", "%TAG !u! tag:unity3d.com,2011:"]
    for i, (game_object, mat_guids) in enumerate(renderers):
        go_id, renderer_id = 100 * (i + 1), 100 * (i + 1) + 1
        lines += [
            f"--- !u!1 &{go_id}",
            "GameObject:",
            f"  m_Name: {game_object}",
            "  m_Component:",
            f"  - component: {{fileID: {renderer_id}}}",
            f"--- !u!23 &{renderer_id}",
            "MeshRenderer:",
            f"  m_GameObject: {{fileID: {go_id}}}",
            "  m_Materials:",
        ]
        lines += [f"  - {{fileID: 2100000, guid: {guid}, type: 2}}" for guid in mat_guids]
    return "\n".join(lines) + "\n"


def texture_meta(guid: str, normal: bool) -> str:
    return f"""fileFormatVersion: 2
guid: {guid}
TextureImporter:
  serializedVersion: 13
  mipmaps:
    sRGBTexture: {0 if normal else 1}
  textureSettings:
    wrapU: 0
    wrapV: 0
  alphaUsage: 1
  alphaIsTransparency: 0
  textureType: {1 if normal else 0}
"""


def export_model(kind: str, mat_name: str, path: Path, *, name: str | None = None, first_used_mat: str | None = None) -> None:
    """``first_used_mat`` を渡すと 2 番目のスロットに追加し、先頭側の半分のポリゴンに割り当てる。

    ポリゴンで最初に使われるのが 2 番目のスロットになり、Unity のサブメッシュ順がスロット順と逆になる。
    """
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if kind == "cube":
        bpy.ops.mesh.primitive_cube_add()
    elif kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add()
    elif kind == "cone":
        bpy.ops.mesh.primitive_cone_add()
    elif kind == "torus":
        bpy.ops.mesh.primitive_torus_add()
    elif kind == "icosphere":
        bpy.ops.mesh.primitive_ico_sphere_add()
    elif kind == "monkey":
        bpy.ops.mesh.primitive_monkey_add()
    else:
        bpy.ops.mesh.primitive_cylinder_add()
    obj = bpy.context.active_object
    obj.name = name or f"Synthetic{kind.capitalize()}"
    mat = bpy.data.materials.new(mat_name)
    obj.data.materials.append(mat)
    if first_used_mat:
        obj.data.materials.append(bpy.data.materials.new(first_used_mat))
        polygons = obj.data.polygons
        for polygon in polygons[: len(polygons) // 2]:
            polygon.material_index = 1
    if path.suffix == ".fbx":
        bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, add_leaf_bones=False)
    elif path.suffix == ".vrm":
        # .vrm は glTF バイナリ。VRM 拡張の無い .glb を .vrm 名で置き、glTF インポーターへのフォールバック経路を確認する
        bpy.ops.export_scene.gltf(filepath=str(path.with_suffix(".glb")), export_format="GLB", use_selection=False)
        path.with_suffix(".glb").rename(path)
    elif path.suffix == ".blend":
        # 既にノードが組まれたマテリアルを持つ .blend（KEEP の確認用）
        mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.2, 0.9, 0.3, 1.0)
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
    else:
        bpy.ops.wm.obj_export(filepath=str(path), export_materials=True)


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent / "_local" / "synthetic_multi.unitypackage"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="synthetic_pkg_"))

    entries: dict[str, tuple[str, bytes | None, str | None]] = {}  # guid -> (pathname, asset, meta)

    def add(pathname: str, asset: bytes | None, meta: str | None) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    add("Assets/Synthetic", None, "fileFormatVersion: 2\nguid: %s\nfolderAsset: yes\n" % guid_of("Assets/Synthetic"))
    tex_a = add("Assets/Synthetic/Textures/ColorA.png", png_bytes(8, (200, 80, 80)), texture_meta(guid_of("Assets/Synthetic/Textures/ColorA.png"), False))
    tex_n = add("Assets/Synthetic/Textures/Normal.png", png_bytes(8, (128, 128, 255)), texture_meta(guid_of("Assets/Synthetic/Textures/Normal.png"), True))

    mats = {
        "CubeMat": (tex_a, tex_n, (1, 1, 1), 0),
        "SphereMat": (tex_a, None, (0.5, 0.8, 1.0), 1),
        "CylinderMat": (tex_a, None, (1, 1, 0.5), 3),
    }
    mat_guids = {}
    for name, (t, n, color, mode) in mats.items():
        pathname = f"Assets/Synthetic/Materials/{name}.mat"
        mat_guids[name] = add(pathname, mat_yaml(name, t, n, color, mode).encode(), f"fileFormatVersion: 2\nguid: {guid_of(pathname)}\nNativeFormatImporter:\n  mainObjectFileID: 2100000\n")

    for kind, mat_name, ext in (("cube", "CubeMat", ".fbx"), ("sphere", "SphereMat", ".fbx"), ("cylinder", "CylinderMat", ".obj")):
        path = tmp / f"{kind}{ext}"
        export_model(kind, mat_name, path)
        pathname = f"Assets/Synthetic/Models/{kind.capitalize()}{ext}"
        add(pathname, path.read_bytes(), model_meta(guid_of(pathname), {mat_name: mat_guids[mat_name]}))
        if ext == ".obj":
            mtl = path.with_suffix(".mtl")
            if mtl.is_file():
                add(f"Assets/Synthetic/Models/{kind.capitalize()}.mtl", mtl.read_bytes(), None)

    # VRChat Mobile Toon Lit（Standard の残骸プロパティ付き）を externalObjects で割り当てたモデル
    path = tmp / "icosphere.fbx"
    export_model("icosphere", "QuestToonMat", path)
    ico_path = "Assets/Synthetic/Models/Icosphere.fbx"
    quest_mat_path = "Assets/Synthetic/Materials/QuestToonMat.mat"
    quest_mat = add(quest_mat_path, toon_lit_mat_yaml("QuestToonMat", tex_a).encode(),
                    f"fileFormatVersion: 2\nguid: {guid_of(quest_mat_path)}\nNativeFormatImporter:\n  mainObjectFileID: 2100000\n")
    add(ico_path, path.read_bytes(), model_meta(guid_of(ico_path), {"QuestToonMat": quest_mat}))

    # .vrm（VRM 拡張の無い glb）。UniVRM と同じく ScriptedImporter の .meta で、マテリアルは名前一致で解決する
    path = tmp / "monkey.vrm"
    export_model("monkey", "SphereMat", path)
    vrm_path = "Assets/Synthetic/Models/Monkey.vrm"
    add(vrm_path, path.read_bytes(),
        f"fileFormatVersion: 2\nguid: {guid_of(vrm_path)}\nScriptedImporter:\n  internalIDToNameTable: []\n  externalObjects: {{}}\n")

    # 同梱 .blend（マテリアルは既に設定済み。externalObjects は CubeMat を指す）
    path = tmp / "torus.blend"
    export_model("torus", "TorusBlendMat", path)
    blend_path = "Assets/Synthetic/Models/Torus.blend"
    add(blend_path, path.read_bytes(), model_meta(guid_of(blend_path), {"TorusBlendMat": mat_guids["CubeMat"]}))

    # externalObjects も名前一致も無く、prefab の Renderer だけが .mat を指しているモデル
    path = tmp / "cone.fbx"
    export_model("cone", "ConeFbxMat", path)
    cone_path = "Assets/Synthetic/Models/Cone.fbx"
    add(cone_path, path.read_bytes(), model_meta(guid_of(cone_path), {}))

    # 2 マテリアルのモデル。ポリゴンは 2 番目のスロット（PairB）から使われるので、Unity のサブメッシュ順と
    # prefab の m_Materials は PairB, PairA になる。スロット番号で突き合わせると入れ替わってしまう（Issue #22）
    for pair_name in ("PairA", "PairB"):
        pathname = f"Assets/Synthetic/Materials/{pair_name}.mat"
        mat_guids[pair_name] = add(pathname, mat_yaml(pair_name, tex_a, None, (1, 1, 1), 0).encode(),
                                   f"fileFormatVersion: 2\nguid: {guid_of(pathname)}\nNativeFormatImporter:\n  mainObjectFileID: 2100000\n")
    path = tmp / "pair.fbx"
    export_model("cube", "PairA", path, name="SyntheticPair", first_used_mat="PairB")
    pair_path = "Assets/Synthetic/Models/Pair.fbx"
    add(pair_path, path.read_bytes(),
        model_meta(guid_of(pair_path), {"PairA": mat_guids["PairA"], "PairB": mat_guids["PairB"]}))

    prefab_path = "Assets/Synthetic/Prefabs/Cone.prefab"
    prefab = prefab_yaml([
        ("SyntheticCone", [mat_guids["CylinderMat"]]),
        ("SyntheticPair", [mat_guids["PairB"], mat_guids["PairA"]]),
    ])
    add(prefab_path, prefab.encode(),
        f"fileFormatVersion: 2\nguid: {guid_of(prefab_path)}\nPrefabImporter:\n  externalObjects: {{}}\n")

    with tarfile.open(out, "w:gz") as tar:
        for guid, (pathname, asset, meta) in entries.items():
            def put(name: str, data: bytes) -> None:
                info = tarfile.TarInfo(f"{guid}/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            put("pathname", (pathname + "\n").encode())
            if asset is not None:
                put("asset", asset)
            if meta is not None:
                put("asset.meta", meta.encode())
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(entries)} entries)")


if __name__ == "__main__":
    main()
