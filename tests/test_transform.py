import math
import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.transform import (
    IDENTITY,
    BLENDER_TO_UNITY,
    UNITY_TO_BLENDER,
    chain,
    inverse_affine,
    multiply,
    normalize_quat,
    transform_point,
    trs,
    unity_to_blender,
)

# Y 軸まわりに 90 度（Unity の並び x, y, z, w）
Y90 = (0.0, math.sqrt(0.5), 0.0, math.sqrt(0.5))


def assert_vec(test, actual, expected, places=5):
    for a, e in zip(actual, expected):
        test.assertAlmostEqual(a, e, places=places)


def assert_mat(test, actual, expected, places=5):
    for row_a, row_e in zip(actual, expected):
        assert_vec(test, row_a, row_e, places)


class TrsTest(unittest.TestCase):
    def test_identity(self):
        assert_mat(self, trs((0, 0, 0), (0, 0, 0, 1), (1, 1, 1)), IDENTITY)

    def test_translation_rotation_scale_order(self):
        # スケール → 回転 → 平行移動の順に効く（Unity の Matrix4x4.TRS と同じ）
        m = trs((-2.0, 0.0, 1.0), Y90, (1.5, 1.5, 1.5))
        # ローカルの +X（長さ 1）は 1.5 倍され、Y 軸まわり 90 度で -Z を向き、(-2, 0, 1) だけ動く
        assert_vec(self, transform_point(m, (1.0, 0.0, 0.0)), (-2.0, 0.0, -0.5))

    def test_broken_quaternion_falls_back_to_identity_rotation(self):
        self.assertEqual(normalize_quat((0.0, 0.0, 0.0, 0.0)), (0.0, 0.0, 0.0, 1.0))
        self.assertEqual(normalize_quat((float("nan"), 0.0, 0.0, 1.0)), (0.0, 0.0, 0.0, 1.0))
        assert_vec(self, normalize_quat((0.0, 0.0, 0.0, 2.0)), (0.0, 0.0, 0.0, 1.0))

    def test_chain_applies_parent_first(self):
        parent = trs((0.0, 0.0, 5.0), (0, 0, 0, 1), (2.0, 2.0, 2.0))
        child = trs((1.0, 0.0, 0.0), (0, 0, 0, 1), (1.0, 1.0, 1.0))
        assert_vec(self, transform_point(chain([parent, child]), (0.0, 0.0, 0.0)), (2.0, 0.0, 5.0))


class InverseTest(unittest.TestCase):
    def test_round_trip(self):
        m = trs((1.0, 2.0, 3.0), normalize_quat((0.1276794, 0.1448781, 0.2392983, 0.9515485)), (1.0, 2.0, 0.5))
        inv = inverse_affine(m)
        self.assertIsNotNone(inv)
        assert_mat(self, multiply(m, inv), IDENTITY)
        assert_mat(self, multiply(inv, m), IDENTITY)

    def test_singular_returns_none(self):
        self.assertIsNone(inverse_affine(trs((0, 0, 0), (0, 0, 0, 1), (1.0, 0.0, 1.0))))


class UnityToBlenderTest(unittest.TestCase):
    def test_basis_maps_axes(self):
        # Unity の (x, y, z) は Blender では (-x, -z, y)
        assert_vec(self, transform_point(UNITY_TO_BLENDER, (1.0, 2.0, 3.0)), (-1.0, -3.0, 2.0))
        assert_mat(self, multiply(UNITY_TO_BLENDER, BLENDER_TO_UNITY), IDENTITY)

    def test_unity_up_translation_becomes_blender_up(self):
        m = unity_to_blender(trs((0.0, 3.0, 0.0), (0, 0, 0, 1), (1, 1, 1)))
        assert_vec(self, transform_point(m, (0.0, 0.0, 0.0)), (0.0, 0.0, 3.0))

    def test_rotation_about_unity_up_stays_proper(self):
        # 変換後も鏡映にならない（行列式が正）
        m = unity_to_blender(trs((0, 0, 0), Y90, (1, 1, 1)))
        det = (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )
        self.assertAlmostEqual(det, 1.0)

    def test_matches_unity_probe_scene(self):
        """合成 FBX を Unity 6 のシーンに置いた結果（Issue #48 の調査）と一致する。

        FBX の Spike の突起の頂点は、Blender の FBX インポーターで原点に読むと (1.4588, 1.9392, 0.9)。
        Unity では、空の親 Holder（位置 (-2, 0, 1)、Y 90 度、スケール 1.5）の下に置いた FBX のルート
        （位置 (0, 1, 0)、Z 45 度）の下で、ワールド座標 (-4.9088, 0.9073, 3.5019) になった。
        """
        holder = trs((-2.0, 0.0, 1.0), Y90, (1.5, 1.5, 1.5))
        root = trs((0.0, 1.0, 0.0), (0.0, 0.0, 0.38268346, 0.9238795), (1.0, 1.0, 1.0))
        placed = unity_to_blender(chain([holder, root]))
        tip_in_blender = transform_point(placed, (1.4588, 1.9392, 0.9))
        assert_vec(self, tip_in_blender, transform_point(UNITY_TO_BLENDER, (-4.9088, 0.9073, 3.5019)), places=3)


if __name__ == "__main__":
    unittest.main()
