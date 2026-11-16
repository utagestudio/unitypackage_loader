"""Unity の Mesh（.asset と、prefab に埋め込んだもの）だけを使う合成 unitypackage を生成する（Issue #216）。

    blender -b --factory-startup --python tests/make_synthetic_mesh_package.py -- <出力パス.unitypackage>

モデルファイルは入れない。実在アセットのデータは使わない。

- Assets/Synthetic/Meshes/Wedge.asset: Unity の Quad と同じ形の Mesh（サブメッシュ 2 つ）。Prefabs/Wedge.prefab の Wedge
  （位置 (0, 1, 0)）が MeshMat.mat と AssetMat.asset（マテリアルを .asset に入れたもの）で使う。
- Prefabs/Embedded.prefab: prefab の中に埋め込んだ同じ形の Mesh を、Panel が MeshMat で使う（UModeler の prefab と同じ形）。
- Scenes/Meshes.unity: Wedge.prefab を X に 5、Embedded.prefab を Z に 5 動かして置き、Direct が Wedge.asset を 2 倍で使う。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.make_synthetic_package import guid_of, mat_yaml, png_bytes, prefab_meta, texture_meta, write_package  # noqa: E402
from tests.test_unity_mesh import QUAD, QUAD_NORMALS, QUAD_TRIS, QUAD_UVS  # noqa: E402
from tests.unity_mesh_writer import mesh_document  # noqa: E402
from tests.unity_yaml_writer import HEADER, game_object, instance, mod, transform  # noqa: E402

EMBEDDED_MESH_ID = 5000


def _renderer(filter_id: int, renderer_id: int, go: int, mesh_ref: str, materials: list[str]) -> str:
    mats = "".join(f"  - {{fileID: 2100000, guid: {m}, type: 2}}\n" for m in materials)
    return (
        f"--- !u!33 &{filter_id}\nMeshFilter:\n  m_GameObject: {{fileID: {go}}}\n  m_Mesh: {mesh_ref}\n"
        f"--- !u!23 &{renderer_id}\nMeshRenderer:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n  m_Materials:\n{mats}"
    )


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

    scene_path = "Assets/Synthetic/Scenes/Meshes.unity"
    scene = HEADER + "".join([
        instance(100, wedge_prefab, 0, [mod(11, wedge_prefab, "m_LocalPosition.x", 5)]),
        instance(200, embedded, 0, [mod(11, embedded, "m_LocalPosition.z", 5)]),
        game_object(300, "Direct"), transform(301, 300, scale=(2, 2, 2)), _renderer(302, 303, 300, wedge_ref, [mesh_mat]),
    ])
    add(scene_path, scene.encode(), f"fileFormatVersion: 2\nguid: {guid_of(scene_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")
    write_package(out, entries)


if __name__ == "__main__":
    main()
