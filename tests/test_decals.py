"""DecalProjector を板で近似する（Issue #165）。

座標は Blender の (-x, -z, y)（Unity の (x, y, z)）。
"""

import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.decals import (
    INHERIT_FROM_HIERARCHY,
    SCALE_INVARIANT,
    Decal,
    decal_from_body,
    decal_plane,
    is_decal_projector,
)
from unitypackage_loader.core.primitives import _polygon_normal
from unitypackage_loader.core.unity_yaml import UnityRef

MAT = "d" * 32


def projector(**fields):
    """Shibuya の URP DecalProjector と同じフィールド。"""
    body = {
        "m_Enabled": 1,
        "m_Script": UnityRef(11500000, "0777d029ed3dffa4692f417d4aba19ca", 3),
        "m_Material": UnityRef(2100000, MAT, 2),
        "m_DrawDistance": 1000,
        "m_UVScale": {"x": 1, "y": 1},
        "m_UVBias": {"x": 0, "y": 0},
        "m_ScaleMode": 1,
        "m_Offset": {"x": 0, "y": 0, "z": 0},
        "m_Size": {"x": 1, "y": 1.02, "z": 0.02},
        "m_FadeFactor": 1,
    }
    body.update(fields)
    return body


def corners(mesh):
    """Unity の座標での (x, y, z) → UV。"""
    return {(round(-x, 6), round(z, 6), round(-y, 6)): tuple(round(c, 6) for c in uv)
            for face, uvs in zip(mesh.faces, mesh.uvs) for (x, y, z), uv in ((mesh.vertices[i], uv) for i, uv in zip(face, uvs))}


class ReadTest(unittest.TestCase):
    def test_reads_fields(self):
        self.assertTrue(is_decal_projector(projector()))
        decal = decal_from_body(projector(m_Offset={"x": 0.1, "y": 0, "z": 0.5}, m_UVScale={"x": 2, "y": 1}))
        self.assertEqual(decal, Decal(MAT, (1.0, 1.02, 0.02), (0.1, 0.0, 0.5), (2.0, 1.0), (0.0, 0.0), INHERIT_FROM_HIERARCHY))

    def test_not_a_decal(self):
        self.assertFalse(is_decal_projector({"m_Material": UnityRef(0, None, 0), "m_Size": {}}))
        self.assertIsNone(decal_from_body(projector(m_Material=UnityRef(0, None, 0))))
        self.assertIsNone(decal_from_body(projector(m_Size={"x": 0, "y": 1, "z": 1})))
        self.assertEqual(decal_from_body(projector(m_ScaleMode=None)).scale_mode, SCALE_INVARIANT)


class PlaneTest(unittest.TestCase):
    def test_plane_in_middle_of_box_facing_projector(self):
        decal = decal_from_body(projector(m_Size={"x": 2, "y": 1, "z": 0.4}, m_Offset={"x": 0, "y": 0.5, "z": 1}))
        mesh = decal_plane(decal)
        uv = corners(mesh)
        # 箱の中心（z = 1）から投影元へ 2 mm 寄せた高さに、xy の範囲の板。UV は左下 (0, 0)、右上 (1, 1)（Unity の +X が右、+Y が上）
        self.assertEqual(uv, {(-1.0, 0.0, 0.998): (0, 0), (1.0, 0.0, 0.998): (1, 0), (1.0, 1.0, 0.998): (1, 1), (-1.0, 1.0, 0.998): (0, 1)})
        # 表は投影元（Unity の -Z、Blender の +Y）を向く
        normal = _polygon_normal([mesh.vertices[i] for i in mesh.faces[0]])
        self.assertGreater(normal[1], 0)
        self.assertAlmostEqual(normal[0], 0)
        self.assertAlmostEqual(normal[2], 0)

    def test_uv_scale_and_bias(self):
        decal = decal_from_body(projector(m_UVScale={"x": 0.5, "y": 2}, m_UVBias={"x": 0.25, "y": -1}))
        values = sorted(set(corners(decal_plane(decal)).values()))
        self.assertEqual(values, [(0.25, -1.0), (0.25, 1.0), (0.75, -1.0), (0.75, 1.0)])

    def test_thin_box_moves_less(self):
        decal = decal_from_body(projector(m_Size={"x": 1, "y": 1, "z": 0.004}))
        self.assertEqual({p[2] for p in corners(decal_plane(decal))}, {-0.001})

    def test_scale_invariant_divides_by_object_scale(self):
        decal = decal_from_body(projector(m_ScaleMode=0, m_Size={"x": 2, "y": 2, "z": 1}))
        xs = {p[0] for p in corners(decal_plane(decal, (4.0, 1.0, 1.0)))}
        self.assertEqual(xs, {-0.25, 0.25})  # GameObject の 4 倍を掛けると 2 m に戻る
        inherit = decal_from_body(projector(m_Size={"x": 2, "y": 2, "z": 1}))
        self.assertEqual({p[0] for p in corners(decal_plane(inherit, (4.0, 1.0, 1.0)))}, {-1.0, 1.0})


if __name__ == "__main__":
    unittest.main()
