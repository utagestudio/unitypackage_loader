import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.unity_ids import local_file_id, mesh_file_id, xxh64


class Xxh64Test(unittest.TestCase):
    def test_reference_vectors(self):
        self.assertEqual(xxh64(b""), 0xEF46DB3751D8E999)
        self.assertEqual(xxh64(b"a"), 0xD24EC4F1A98C6E5B)
        self.assertEqual(xxh64(b"abc"), 0x44BC2CF5AD770999)


class UnityFileIdTest(unittest.TestCase):
    def test_matches_unity_prefab_mesh_references(self):
        # Unity 6 が合成 FBX を展開した prefab の MeshFilter / SkinnedMeshRenderer の m_Mesh（Issue #58 の調査）
        self.assertEqual(mesh_file_id("Child"), -7630113837697264728)
        self.assertEqual(mesh_file_id("Spike"), 9188603553798411242)
        self.assertEqual(mesh_file_id("Skinned"), 2907845800280414690)

    def test_offset_and_type_change_the_id(self):
        self.assertNotEqual(mesh_file_id("Spike", 1), mesh_file_id("Spike"))
        self.assertNotEqual(local_file_id("Material", "Spike"), mesh_file_id("Spike"))

    def test_result_is_signed_64_bit(self):
        for name in ("Child", "Spike", "Skinned", "日本語メッシュ", "a" * 100):
            value = mesh_file_id(name)
            self.assertTrue(-(1 << 63) <= value < (1 << 63))


if __name__ == "__main__":
    unittest.main()
