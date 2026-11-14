"""Unity の組み込みメッシュの形（Issue #190）。

寸法・表の向き・UV は、Unity 6000.6 の ``GameObject.CreatePrimitive`` のメッシュと突き合わせた値。
座標は Blender の (-x, -z, y)（Unity の (x, y, z)）。
"""

import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.primitives import (
    BUILTIN_MESH_GUID,
    BUILTIN_MESHES,
    _polygon_normal,
    build_primitive,
    builtin_mesh_kind,
)


def bounds(mesh):
    lo = [min(v[i] for v in mesh.vertices) for i in range(3)]
    hi = [max(v[i] for v in mesh.vertices) for i in range(3)]
    return [round(x, 6) for x in lo + hi]


def unit_normal(mesh, face):
    n = _polygon_normal([mesh.vertices[i] for i in face])
    length = sum(c * c for c in n) ** 0.5
    return tuple(round(c / length, 6) + 0.0 for c in n)


def uv_at(mesh, point):
    """``point``（Blender の座標）にある頂点の UV（最初に見つかった面の角）。"""
    for face, uvs in zip(mesh.faces, mesh.uvs):
        for index, uv in zip(face, uvs):
            if all(abs(a - b) < 1e-6 for a, b in zip(mesh.vertices[index], point)):
                return tuple(round(c, 6) for c in uv)
    return None


class BuiltinKindTest(unittest.TestCase):
    def test_known_file_ids(self):
        self.assertEqual(builtin_mesh_kind(BUILTIN_MESH_GUID, 10210), "Quad")
        self.assertEqual(builtin_mesh_kind(BUILTIN_MESH_GUID.upper(), 10209), "Plane")
        self.assertEqual(sorted(BUILTIN_MESHES.values()), ["Capsule", "Cube", "Cylinder", "Plane", "Quad", "Sphere"])

    def test_other_references(self):
        self.assertIsNone(builtin_mesh_kind(BUILTIN_MESH_GUID, 10303))  # 既定のマテリアルなど、メッシュ以外
        self.assertIsNone(builtin_mesh_kind("a" * 32, 10210))
        self.assertIsNone(builtin_mesh_kind(None, 10210))


class ShapeTest(unittest.TestCase):
    def test_bounds(self):
        expected = {
            "Quad": [-0.5, 0, -0.5, 0.5, 0, 0.5],
            "Plane": [-5, -5, 0, 5, 5, 0],
            "Cube": [-0.5, -0.5, -0.5, 0.5, 0.5, 0.5],
            "Sphere": [-0.5, -0.5, -0.5, 0.5, 0.5, 0.5],
            "Cylinder": [-0.5, -0.5, -1, 0.5, 0.5, 1],
            "Capsule": [-0.5, -0.5, -1, 0.5, 0.5, 1],
        }
        for kind, box in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(bounds(build_primitive(kind)), box)

    def test_quad_faces_unity_minus_z(self):
        quad = build_primitive("Quad")
        self.assertEqual(unit_normal(quad, quad.faces[0]), (0, 1, 0))  # Unity の -Z
        self.assertEqual(uv_at(quad, (0.5, 0, -0.5)), (0, 0))  # Unity の (-0.5, -0.5, 0)
        self.assertEqual(uv_at(quad, (-0.5, 0, 0.5)), (1, 1))  # Unity の (0.5, 0.5, 0)

    def test_plane_faces_up_with_unity_uv(self):
        plane = build_primitive("Plane")
        self.assertEqual((len(plane.vertices), len(plane.faces)), (121, 100))
        self.assertTrue(all(unit_normal(plane, f) == (0, 0, 1) for f in plane.faces))
        self.assertEqual(uv_at(plane, (-5, -5, 0)), (0, 0))  # Unity の (5, 0, 5)
        self.assertEqual(uv_at(plane, (5, 5, 0)), (1, 1))  # Unity の (-5, 0, -5)

    def test_cube_face_uv(self):
        cube = build_primitive("Cube")
        # Unity の +Z の面（Blender の -Y）は、(0.5, -0.5, 0.5) が UV (0, 0)
        face = next(i for i, f in enumerate(cube.faces) if unit_normal(cube, f) == (0, -1, 0))
        corners = dict(zip((cube.vertices[i] for i in cube.faces[face]), cube.uvs[face]))
        self.assertEqual(tuple(corners[(-0.5, -0.5, -0.5)]), (0, 0))

    def test_closed_shapes_face_outward(self):
        for kind in ("Cube", "Sphere", "Cylinder", "Capsule"):
            mesh = build_primitive(kind)
            for face in mesh.faces:
                points = [mesh.vertices[i] for i in face]
                center = [sum(p[i] for p in points) / len(points) for i in range(3)]
                normal = _polygon_normal(points)
                with self.subTest(kind=kind):
                    self.assertGreater(sum(a * b for a, b in zip(normal, center)), 0)

    def test_uvs_match_corners(self):
        for kind in BUILTIN_MESHES.values():
            mesh = build_primitive(kind)
            with self.subTest(kind=kind):
                self.assertEqual([len(f) for f in mesh.faces], [len(u) for u in mesh.uvs])
                self.assertEqual(len(mesh.smooth), len(mesh.faces))


if __name__ == "__main__":
    unittest.main()
