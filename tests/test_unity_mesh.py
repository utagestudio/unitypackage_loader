"""core/unity_mesh.py（Unity の Mesh の読み取り。Issue #216）のテスト。"""

from __future__ import annotations

import unittest

from tests import _paths  # noqa: F401
from tests.unity_mesh_writer import mesh_document
from tests.unity_yaml_writer import HEADER
from unitypackage_loader.core.unity_mesh import MeshError, read_mesh
from unitypackage_loader.core.unity_yaml import parse_documents

# Unity の組み込みの Quad と同じ並び（-Z を向く面。Unity の表は時計回り）
QUAD = [(-0.5, -0.5, 0.0), (0.5, -0.5, 0.0), (-0.5, 0.5, 0.0), (0.5, 0.5, 0.0)]
QUAD_TRIS = [0, 3, 1, 3, 0, 2]
QUAD_NORMALS = [(0.0, 0.0, -1.0)] * 4
QUAD_UVS = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]


def _body(text: str) -> dict:
    return parse_documents(HEADER + text)[0].body


def _face_normal(mesh, tri):
    a, b, c = (mesh.vertices[i] for i in tri)
    u = [b[k] - a[k] for k in range(3)]
    v = [c[k] - a[k] for k in range(3)]
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


class ReadMeshTest(unittest.TestCase):
    def test_quad(self):
        mesh = read_mesh(_body(mesh_document(4300000, "Quad", QUAD, [QUAD_TRIS], normals=QUAD_NORMALS, uvs=QUAD_UVS)))
        self.assertEqual(mesh.name, "Quad")
        # Unity の (x, y, z) は Blender の (-x, -z, y)
        self.assertEqual(mesh.vertices[1], (-0.5, -0.0, -0.5))
        self.assertEqual(mesh.normals[0], (-0.0, 1.0, 0.0))
        self.assertEqual(mesh.uvs[3], (1.0, 1.0))
        self.assertEqual(len(mesh.submeshes[0]), 2)
        for tri in mesh.submeshes[0]:
            # 面の表（反時計回り）が頂点の法線と同じ向き
            self.assertGreater(sum(a * b for a, b in zip(_face_normal(mesh, tri), mesh.normals[tri[0]])), 0)

    def test_submeshes_and_wide_indices(self):
        mesh = read_mesh(_body(mesh_document(1, "Two", QUAD, [QUAD_TRIS[:3], QUAD_TRIS[3:]], wide_indices=True)))
        self.assertEqual([len(s) for s in mesh.submeshes], [1, 1])
        self.assertIsNone(mesh.normals)
        self.assertIsNone(mesh.uvs)

    def test_broken_meshes_raise_value_error(self):
        good = mesh_document(1, "Quad", QUAD, [QUAD_TRIS])
        for broken in (
            good.replace("m_VertexCount: 4", "m_VertexCount: 9"),  # 頂点のデータが足りない
            good.replace("indexCount: 6", "indexCount: 60"),  # 頂点番号のデータが足りない
            good.replace("path: \n", "path: archive:/CAB-1/CAB-1.resS\n"),  # 外部のストリーム
            good.replace("    m_Channels:\n", "    m_Streams: []\n    m_Channels_old:\n"),  # 古い形
        ):
            with self.assertRaises(MeshError):
                read_mesh(_body(broken))
        with self.assertRaises(ValueError):
            read_mesh(_body(mesh_document(1, "Bad", QUAD, [[0, 1, 7]])))  # 範囲外の頂点番号


if __name__ == "__main__":
    unittest.main()
