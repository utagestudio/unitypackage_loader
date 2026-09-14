import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.arrange import MIN_GAP, arrange_offsets, grid_columns


def box(cx: float, cy: float, width: float, depth: float = 1.0, height: float = 1.0):
    return (
        (cx - width / 2, cy - depth / 2, 0.0),
        (cx + width / 2, cy + depth / 2, height),
    )


class GridColumnsTest(unittest.TestCase):
    def test_single_row_up_to_four(self):
        self.assertEqual([grid_columns(n) for n in (0, 1, 2, 3, 4)], [1, 1, 2, 3, 4])

    def test_square_grid_above_four(self):
        self.assertEqual([grid_columns(n) for n in (5, 9, 10, 50)], [3, 3, 4, 8])


class ArrangeOffsetsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(arrange_offsets([]), [])

    def test_first_unit_stays(self):
        self.assertEqual(arrange_offsets([box(3.0, -2.0, 1.0)]), [(0.0, 0.0)])

    def test_row_along_x_without_overlap(self):
        bounds = [box(0, 0, 1.0), box(0, 0, 2.0), box(0, 0, 1.0)]
        offsets = arrange_offsets(bounds)
        gap = 0.5  # いちばん大きい幅 2.0 の 25%
        self.assertEqual(offsets[0], (0.0, 0.0))
        self.assertAlmostEqual(offsets[1][0], 0.5 + gap + 1.0)  # 2 つ目の左端を 1 つ目の右端 + 間隔に
        self.assertAlmostEqual(offsets[2][0], 0.5 + gap + 2.0 + gap + 0.5)
        self.assertTrue(all(y == 0.0 for _, y in offsets))
        placed = [(mn[0] + dx, mx[0] + dx) for (mn, mx), (dx, _) in zip(bounds, offsets)]
        for (_, right), (left, _) in zip(placed, placed[1:]):
            self.assertAlmostEqual(left - right, gap)

    def test_wraps_into_grid_behind(self):
        bounds = [box(0, 0, 1.0, depth=d) for d in (1.0, 3.0, 1.0, 1.0, 1.0)]
        offsets = arrange_offsets(bounds)
        gap = 0.75  # いちばん大きい奥行き 3.0 の 25%
        self.assertEqual([y for _, y in offsets[:3]], [0.0, 1.0, 0.0])  # 1 行目は手前側を揃える
        self.assertAlmostEqual(offsets[3][1], 3.0 + gap)  # 2 行目は 1 行目の最大の奥行き + 間隔だけ後ろ
        self.assertAlmostEqual(offsets[3][0], 0.0)

    def test_tiny_units_keep_minimum_gap(self):
        offsets = arrange_offsets([box(0, 0, 0.01, 0.01), box(0, 0, 0.01, 0.01)])
        self.assertAlmostEqual(offsets[1][0], 0.01 + MIN_GAP)

    def test_missing_bounds_are_points(self):
        offsets = arrange_offsets([box(0, 0, 1.0), None])
        self.assertAlmostEqual(offsets[1][0], 0.5 + 0.25)  # 1 つ目の右端 + 間隔（幅 1.0 の 25%）
        self.assertAlmostEqual(offsets[1][1], -0.5)  # 手前側を 1 つ目に揃える


if __name__ == "__main__":
    unittest.main()
