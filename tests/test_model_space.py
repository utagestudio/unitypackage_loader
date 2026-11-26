import math
import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.model_space import (
    ModelUnits,
    axis_correction,
    collapsed_root,
    decompose,
    file_scale,
    node_basis,
    offset_world,
    renderer_world,
    same_placement,
)
from unitypackage_loader.core.transform import (
    IDENTITY,
    UNITY_TO_BLENDER,
    chain,
    inverse_affine,
    multiply,
    trs,
    unity_to_blender,
)
from tests.test_transform import Y90, assert_mat, assert_vec

Z_UP_MAX = (2, 1, 1, -1, 0, 1)  # 3ds Max の Z が上の FBX
Y_UP = (1, 1, 2, 1, 0, 1)


class FileScaleTest(unittest.TestCase):
    def test_unit_scale(self):
        self.assertAlmostEqual(file_scale(100.0), 1.0)
        self.assertAlmostEqual(file_scale(1.0), 0.01)

    def test_unreadable_is_one(self):
        for value in (None, 0.0, -1.0, math.nan, math.inf):
            self.assertAlmostEqual(file_scale(value), 0.01)


class ModelUnitsTest(unittest.TestCase):
    def test_y_up_flips_x(self):
        assert_mat(self, ModelUnits(100.0, Y_UP).basis_a(), ((-1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)))

    def test_unknown_axes_are_y_up(self):
        self.assertEqual(ModelUnits(1.0).basis_a(), ModelUnits(1.0, Y_UP).basis_a())

    def test_z_up_swaps_y_and_z(self):
        assert_mat(self, ModelUnits(1.0, Z_UP_MAX).basis_a(), ((0.01, 0, 0, 0), (0, 0, 0.01, 0), (0, 0.01, 0, 0), (0, 0, 0, 1)))

    def test_world_basis_includes_global_scale(self):
        units = ModelUnits(100.0, Y_UP, 2.0)
        assert_mat(self, units.world_basis_a(), ((-2, 0, 0, 0), (0, 2, 0, 0), (0, 0, 2, 0), (0, 0, 0, 1)))

    def test_invalid_global_scale_is_ignored(self):
        for value in (0.0, -1.0, math.nan):
            self.assertEqual(ModelUnits(100.0, Y_UP, value).world_basis_a(), ModelUnits(100.0, Y_UP).basis_a())


class AxisCorrectionTest(unittest.TestCase):
    def test_y_up_needs_none(self):
        self.assertIsNone(axis_correction(Y_UP))
        self.assertIsNone(axis_correction(None))

    def test_z_up_is_rotation(self):
        k = axis_correction(Z_UP_MAX)
        self.assertIsNotNone(k)
        rotation = [row[:3] for row in k[:3]]
        det = (
            rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
            - rotation[0][1] * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
            + rotation[0][2] * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0])
        )
        self.assertAlmostEqual(det, 1.0)


class DecomposeTest(unittest.TestCase):
    def test_round_trip(self):
        for rotation in ((0, 0, 0, 1), Y90, (0.5, 0.5, 0.5, 0.5), (0.9, 0.1, -0.3, 0.2), (0.0, 0.0, 1.0, 0.0), (1.0, 0, 0, 0)):
            m = trs((1.0, -2.0, 3.0), rotation, (2.0, 0.5, 3.0))
            location, quat, scale = decompose(m)
            assert_vec(self, location, (1.0, -2.0, 3.0))
            assert_vec(self, scale, (2.0, 0.5, 3.0))
            assert_mat(self, trs(location, quat, scale), m)

    def test_mirror_negates_all_scales(self):
        # mathutils.Matrix.decompose と同じく、鏡映は 3 成分すべての符号を反転して回転に回す
        m = trs((0.0, 0.0, 0.0), Y90, (-1.0, 2.0, 3.0))
        location, quat, scale = decompose(m)
        assert_vec(self, scale, (-1.0, -2.0, -3.0))
        assert_mat(self, trs(location, quat, scale), m)


def _object_matrix(local, basis_a):
    """原点に読み込んだオブジェクトの行列 N = C·L·A。"""
    return chain((UNITY_TO_BLENDER, local, basis_a))


class NodeBasisTest(unittest.TestCase):
    basis_a = ModelUnits(1.0, Y_UP).basis_a()
    local = trs((1.0, 2.0, 3.0), Y90, (1.0, 1.0, 1.0))

    def test_no_override_keeps_matrix(self):
        basis = _object_matrix(self.local, self.basis_a)
        assert_mat(self, node_basis(basis, {}, self.basis_a), basis)

    def test_partial_override(self):
        basis = _object_matrix(self.local, self.basis_a)
        spec = {"position": [None, 5.0, None], "scale": [2.0, 2.0, 2.0]}
        expected = _object_matrix(trs((1.0, 5.0, 3.0), Y90, (2.0, 2.0, 2.0)), self.basis_a)
        assert_mat(self, node_basis(basis, spec, self.basis_a), expected)

    def test_short_or_extra_values(self):
        basis = _object_matrix(self.local, self.basis_a)
        spec = {"position": [7.0], "rotation": [0, 0, 0, 1, 99]}
        expected = _object_matrix(trs((7.0, 2.0, 3.0), (0, 0, 0, 1), (1.0, 1.0, 1.0)), self.basis_a)
        assert_mat(self, node_basis(basis, spec, self.basis_a), expected)

    def test_collapsed_root(self):
        basis = _object_matrix(self.local, self.basis_a)
        spec = {"position": [0.0, 0.0, 0.0]}
        root, obj = collapsed_root(basis, spec, self.basis_a)
        assert_mat(self, obj, multiply(UNITY_TO_BLENDER, self.basis_a))
        assert_mat(self, multiply(root, obj), node_basis(basis, spec, self.basis_a))
        # ルートの Empty は C·M·C⁻¹ の形（M = 上書きを当てたノードの行列）
        assert_mat(self, root, unity_to_blender(trs((0.0, 0.0, 0.0), Y90, (1.0, 1.0, 1.0))))


class PlacementTest(unittest.TestCase):
    def test_renderer_world(self):
        a = ModelUnits(100.0, Y_UP, 2.0).world_basis_a()
        root = trs((1.0, 2.0, 3.0), (0, 0, 0, 1), (1, 1, 1))
        relative = trs((0.0, 1.0, 0.0), Y90, (1, 1, 1))
        assert_mat(self, renderer_world(root, relative, a), chain((root, UNITY_TO_BLENDER, relative, a)))

    def test_offset_world_same_root_keeps_matrix(self):
        root = trs((1.0, 2.0, 3.0), Y90, (1, 1, 1))
        current = trs((4.0, 5.0, 6.0), (0, 0, 0, 1), (2, 2, 2))
        assert_mat(self, offset_world(root, root, 2.0, current), current)

    def test_offset_world_moves_by_difference(self):
        root = IDENTITY
        offset = trs((1.0, 0.0, 0.0), (0, 0, 0, 1), (1, 1, 1))
        current = trs((0.0, 0.0, 0.0), (0, 0, 0, 1), (1, 1, 1))
        # Unity の +X は Blender の -X
        assert_vec(self, [row[3] for row in offset_world(offset, root, 1.0, current)[:3]], (-1.0, 0.0, 0.0))

    def test_offset_world_ignores_invalid_scale(self):
        root = trs((1.0, 2.0, 3.0), Y90, (1, 1, 1))
        offset = trs((2.0, 2.0, 3.0), Y90, (1, 1, 1))
        assert_mat(self, offset_world(offset, root, 0.0, IDENTITY), offset_world(offset, root, 1.0, IDENTITY))

    def test_same_placement(self):
        a = trs((216.0, 0.0, 0.0), Y90, (1, 1, 1))
        self.assertTrue(same_placement(a, trs((216.00005, 0.0, 0.0), Y90, (1, 1, 1))))
        self.assertFalse(same_placement(a, trs((216.02, 0.0, 0.0), Y90, (1, 1, 1))))  # 遠くの 2 cm を見逃さない
        self.assertFalse(same_placement(a, trs((216.0, 0.0, 0.0), (0, 0, 0, 1), (1, 1, 1))))
        big = trs((0.0, 0.0, 0.0), (0, 0, 0, 1), (100, 100, 100))
        self.assertTrue(same_placement(big, trs((0.0, 0.0, 0.0), (0, 0, 0, 1), (100.005, 100, 100))))

    def test_inverse_exists_for_basis(self):
        self.assertIsNotNone(inverse_affine(ModelUnits(1.0, Z_UP_MAX, 3.0).world_basis_a()))


if __name__ == "__main__":
    unittest.main()
