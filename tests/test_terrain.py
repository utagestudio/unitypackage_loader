"""core/terrain.py（Unity の Terrain の読み取りとメッシュ。Issue #205）のテスト。"""

from __future__ import annotations

import struct
import unittest
import zlib
from array import array

from tests.terrain_writer import terrain_data_asset, terrain_layer_yaml
from unitypackage_loader.core import terrain
from unitypackage_loader.core.transform import trs
from unitypackage_loader.core.unity_binary import load_documents

GUID_A = "1aa75da84a41c884d94048112a3fc566"
GUID_B = "ff88a37a120650948a490e1684a6df94"
TEX = "17b389a43b5c54f47b98cf0fe492cc4b"
NORMAL = "2713fa43f16d1b647934bf91b96a949c"


def _grid(columns: int, rows: int) -> list[int]:
    """高さ = x × 10 + z × 1000（どの点か分かるようにする）。"""
    return [i * 10 + j * 1000 for j in range(rows) for i in range(columns)]


class ReadTerrainDataTest(unittest.TestCase):
    def test_heights_and_layers(self):
        alpha = bytes([255, 0, 0, 0, 0, 255, 0, 0, 0, 0, 255, 0, 0, 0, 0, 255])
        data = terrain_data_asset(3, 3, (2.0, 600.0, 4.0), _grid(3, 3), layers=[GUID_A, GUID_B], alphamaps=[(2, 2, alpha)], trees=2)
        td = terrain.read_terrain_data(data)
        self.assertEqual((td.columns, td.rows), (3, 3))
        self.assertEqual(list(td.heights), _grid(3, 3))
        self.assertEqual(td.size, (4.0, 600.0, 8.0))
        self.assertEqual(td.layer_refs, [GUID_A, GUID_B])
        self.assertEqual(len(td.alphamaps), 1)
        self.assertEqual(td.alphamaps[0].pixels, alpha)
        self.assertEqual(td.trees, 2)
        self.assertIsNone(td.holes)  # 穴が 1 つも無ければ None

    def test_argb_alphamap_is_reordered(self):
        rgba = bytes([10, 20, 30, 40, 50, 60, 70, 80, 1, 2, 3, 4, 5, 6, 7, 8])
        data = terrain_data_asset(2, 2, (1.0, 1.0, 1.0), [0, 0, 0, 0], alphamaps=[(2, 2, rgba)], argb=True)
        self.assertEqual(terrain.read_terrain_data(data).alphamaps[0].pixels, rgba)

    def test_old_layout_and_splat_prototypes(self):
        data = terrain_data_asset(
            2, 3, (1.0, 10.0, 1.0), [0] * 6, old_layout=True,
            legacy=[{"texture": TEX, "normalMap": NORMAL, "tileSize": (8.0, 6.0), "tileOffset": (1.0, 2.0), "smoothness": 0.5}],
        )
        td = terrain.read_terrain_data(data)
        self.assertEqual((td.columns, td.rows), (2, 3))
        self.assertEqual(len(td.legacy_layers), 1)
        layer = td.legacy_layers[0]
        self.assertEqual((layer.diffuse, layer.normal), (TEX, NORMAL))
        self.assertEqual(layer.tile_size, (8.0, 6.0))
        self.assertEqual(layer.tile_offset, (1.0, 2.0))
        self.assertAlmostEqual(layer.smoothness, 0.5)

    def test_holes(self):
        holes = [255] * 4
        holes[3] = 0
        td = terrain.read_terrain_data(terrain_data_asset(3, 3, (1.0, 1.0, 1.0), [0] * 9, holes=holes))
        self.assertEqual(td.hole_count, 1)

    def test_wrong_height_count_raises(self):
        with self.assertRaises(ValueError):
            terrain.read_terrain_data(terrain_data_asset(3, 3, (1.0, 1.0, 1.0), [0] * 8))

    def test_not_terrain_raises_value_error(self):
        with self.assertRaises(ValueError):
            terrain.read_terrain_data(b"%YAML 1.1\n--- !u!21 &1\nMaterial:\n  m_Name: x\n")

    def test_compact_documents_use_arrays(self):
        docs = load_documents(terrain_data_asset(2, 2, (1.0, 1.0, 1.0), [1, 2, 3, 4]), compact=True)
        heights = docs[0].body["m_Heightmap"]["m_Heights"]
        self.assertIsInstance(heights, array)
        self.assertEqual(list(heights), [1, 2, 3, 4])
        # compact でなければ従来どおりリスト
        plain = load_documents(terrain_data_asset(2, 2, (1.0, 1.0, 1.0), [1, 2, 3, 4]))
        self.assertIsInstance(plain[0].body["m_Heightmap"]["m_Heights"], list)


class TerrainLayerTest(unittest.TestCase):
    def test_read_layer(self):
        layer = terrain.read_terrain_layer(
            terrain_layer_yaml("Road", TEX, NORMAL, tile_size=(4.0, 2.0), tile_offset=(1.0, 0.5), smoothness=0.3), GUID_A
        )
        self.assertEqual(layer.name, "Road")
        self.assertEqual((layer.diffuse, layer.normal, layer.mask), (TEX, NORMAL, None))
        self.assertEqual(layer.tile_size, (4.0, 2.0))
        self.assertEqual(layer.source, GUID_A)
        self.assertEqual(layer.textures(), [TEX, NORMAL])

    def test_uv_transform(self):
        layer = terrain.TerrainLayer(tile_size=(4.0, 2.0), tile_offset=(1.0, 0.5))
        self.assertEqual(terrain.layer_uv_transform(layer, (400.0, 600.0, 100.0)), (100.0, 50.0, 0.25, 0.25))

    def test_not_a_layer(self):
        self.assertIsNone(terrain.read_terrain_layer("%YAML 1.1\n--- !u!21 &1\nMaterial:\n  m_Name: x\n"))


class GridTest(unittest.TestCase):
    def test_indices_keep_both_ends(self):
        self.assertEqual(terrain.grid_indices(5, 0), [0, 1, 2, 3, 4])
        self.assertEqual(terrain.grid_indices(5, 9), [0, 1, 2, 3, 4])
        self.assertEqual(terrain.grid_indices(4097, 513), list(range(0, 4097, 8)))
        self.assertEqual(terrain.grid_indices(10, 4), [0, 3, 6, 9])
        self.assertEqual(terrain.grid_indices(11, 4), [0, 4, 8, 10])


class TerrainMeshTest(unittest.TestCase):
    def _data(self, columns=3, rows=3, holes=None):
        return terrain.read_terrain_data(
            terrain_data_asset(
                columns, rows, (2.0, terrain.MAX_HEIGHT / 100.0, 4.0), _grid(columns, rows), holes=holes,
                old_layout=columns != rows,  # 正方形でない高さマップは 2019.3 より前の形だけ
            )
        )

    def test_positions_in_blender_axes(self):
        mesh = terrain.terrain_mesh(self._data())
        self.assertEqual((mesh.columns, mesh.rows), (3, 3))
        vertices = [tuple(mesh.vertices[k : k + 3]) for k in range(0, len(mesh.vertices), 3)]
        # 点 (x 番目 1, z 番目 2) は Unity の (2, 高さ, 8)。Blender では (-2, -8, 高さ)。高さ = (10 + 2000) / 100
        self.assertEqual(vertices[2 * 3 + 1][:2], (-2.0, -8.0))
        self.assertAlmostEqual(vertices[2 * 3 + 1][2], 20.1, places=4)
        self.assertEqual(tuple(mesh.uvs[(2 * 3 + 1) * 2 : (2 * 3 + 1) * 2 + 2]), (0.5, 1.0))
        self.assertEqual(len(mesh.faces), 4 * 4)

    def test_faces_point_up(self):
        mesh = terrain.terrain_mesh(self._data())
        v = [tuple(mesh.vertices[k : k + 3]) for k in range(0, len(mesh.vertices), 3)]
        for f in range(0, len(mesh.faces), 4):
            a, b, c = (v[mesh.faces[f + k]] for k in range(3))
            ab = (b[0] - a[0], b[1] - a[1])
            bc = (c[0] - b[0], c[1] - b[1])
            self.assertGreater(ab[0] * bc[1] - ab[1] * bc[0], 0)

    def test_decimation(self):
        mesh = terrain.terrain_mesh(self._data(9, 5), max_points=5)
        self.assertEqual((mesh.columns, mesh.rows), (5, 5))
        xs = sorted({mesh.vertices[k] for k in range(0, len(mesh.vertices), 3)})
        self.assertEqual(xs, [-16.0, -12.0, -8.0, -4.0, 0.0])

    def test_holes_remove_faces(self):
        holes = [255] * 4
        holes[1] = 0  # マス (x 1, z 0)
        mesh = terrain.terrain_mesh(self._data(holes=holes))
        self.assertEqual(len(mesh.faces), 3 * 4)
        self.assertEqual(mesh.removed_holes, 1)
        self.assertNotIn((1, 2, 5, 4), {tuple(mesh.faces[f : f + 4]) for f in range(0, len(mesh.faces), 4)})

    def test_holes_need_majority_after_decimation(self):
        holes = [255] * 16  # 5×5 の点、4×4 のマス。2 点おきに間引くと 1 面が 2×2 のマス
        holes[0] = 0
        self.assertEqual(terrain.terrain_mesh(self._data(5, 5, holes), max_points=3).removed_holes, 0)
        for cell in (1, 4, 5):
            holes[cell] = 0
        self.assertEqual(terrain.terrain_mesh(self._data(5, 5, holes), max_points=3).removed_holes, 1)


class PlacementTest(unittest.TestCase):
    def test_rotation_and_scale_are_ignored(self):
        world = trs((10.0, 1.0, -5.0), (0.0, 0.3826834, 0.0, 0.9238795), (2.0, 2.0, 2.0))
        local = terrain.terrain_local(world)
        # GameObject の行列を掛けると、位置だけの行列に戻る
        from unitypackage_loader.core.transform import multiply

        placed = multiply(world, local)
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(placed[i][j], 1.0 if i == j else 0.0, places=5)
        self.assertAlmostEqual(placed[0][3], 10.0, places=5)
        self.assertAlmostEqual(placed[2][3], -5.0, places=5)


class PngTest(unittest.TestCase):
    def test_rows_are_flipped(self):
        pixels = bytes([1, 2, 3, 4, 5, 6, 7, 8])  # 1 × 2。1 行目（下）が (1, 2, 3, 4)
        png = terrain.png_rgba(1, 2, pixels)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", png[16:24])
        self.assertEqual((width, height), (1, 2))
        start = png.index(b"IDAT") + 4
        length = struct.unpack(">I", png[start - 8 : start - 4])[0]
        raw = zlib.decompress(png[start : start + length])
        self.assertEqual(raw, bytes([0, 5, 6, 7, 8, 0, 1, 2, 3, 4]))


if __name__ == "__main__":
    unittest.main()
