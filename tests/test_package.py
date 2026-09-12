import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests import _paths
from unitypackage_loader.core.package import PackageError, UnityPackage, safe_relative_path

GUID_FOLDER = "0" * 32
GUID_MAT = "1" * 32
GUID_TEX = "2" * 32
GUID_FBX = "3" * 32
GUID_OTHER = "4" * 32
GUID_VRM = "5" * 32

MAT_TEXT = b"%YAML 1.1\n--- !u!21 &2100000\nMaterial:\n  m_Name: ExampleMat\n"


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def build_package(path: Path) -> None:
    with tarfile.open(path, "w:gz") as tar:
        # フォルダ（asset 無し）
        _add(tar, f"{GUID_FOLDER}/pathname", b"Assets/Example\n")
        _add(tar, f"{GUID_FOLDER}/asset.meta", b"fileFormatVersion: 2\nguid: " + GUID_FOLDER.encode() + b"\nfolderAsset: yes\n")
        # マテリアル（pathname が asset より後に来る順序）
        _add(tar, f"./{GUID_MAT}/asset", MAT_TEXT)
        _add(tar, f"./{GUID_MAT}/asset.meta", b"fileFormatVersion: 2\nguid: " + GUID_MAT.encode() + b"\n")
        _add(tar, f"./{GUID_MAT}/pathname", b"Assets/Example/Materials/ExampleMat.mat\n00\n")
        # テクスチャ
        _add(tar, f"{GUID_TEX}/pathname", b"Assets/Example/Textures/Base.PNG")
        _add(tar, f"{GUID_TEX}/asset", b"\x89PNG-fake")
        _add(tar, f"{GUID_TEX}/asset.meta", b"fileFormatVersion: 2\nguid: " + GUID_TEX.encode() + b"\nTextureImporter:\n  textureType: 1\n")
        _add(tar, f"{GUID_TEX}/preview.png", b"ignored")
        # モデル
        _add(tar, f"{GUID_FBX}/pathname", b"Assets/Example/Model.fbx")
        _add(tar, f"{GUID_FBX}/asset", b"Kaydara-fake" * 10)
        # 対象外
        _add(tar, f"{GUID_OTHER}/pathname", b"Assets/Example/Script.cs")
        _add(tar, f"{GUID_OTHER}/asset", b"class X {}")
        _add(tar, f"{GUID_VRM}/pathname", b"Assets/Example/Avatar.vrm")
        _add(tar, f"{GUID_VRM}/asset", b"glTF" + b"\0" * 16)
        # 想定外のメンバーは無視される
        _add(tar, "README.txt", b"hello")


class SyntheticPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg_path = Path(self.tmp.name) / "example.unitypackage"
        build_package(self.pkg_path)
        self.pkg = UnityPackage(self.pkg_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_and_classify(self):
        entries = self.pkg.scan()
        self.assertEqual(set(entries), {GUID_FOLDER, GUID_MAT, GUID_TEX, GUID_FBX, GUID_OTHER, GUID_VRM})
        self.assertEqual(entries[GUID_FOLDER].kind, "folder")
        self.assertEqual(entries[GUID_MAT].kind, "material")
        self.assertEqual(entries[GUID_TEX].kind, "texture")  # 拡張子は大文字でも可
        self.assertEqual(entries[GUID_FBX].kind, "model")
        self.assertEqual(entries[GUID_OTHER].kind, "other")
        self.assertEqual(entries[GUID_VRM].kind, "model")  # .vrm は glTF バイナリ
        self.assertEqual(entries[GUID_MAT].pathname, "Assets/Example/Materials/ExampleMat.mat")
        self.assertEqual(entries[GUID_MAT].name, "ExampleMat.mat")
        self.assertIn("TextureImporter", entries[GUID_TEX].meta_text)
        self.assertEqual({e.guid for e in self.pkg.models()}, {GUID_FBX, GUID_VRM})
        self.assertEqual([e.guid for e in self.pkg.materials()], [GUID_MAT])
        self.assertEqual([e.guid for e in self.pkg.textures()], [GUID_TEX])

    def test_read_asset_uses_cache_for_mat_and_rescans_others(self):
        self.pkg.scan()
        self.assertEqual(self.pkg.read_asset(GUID_MAT), MAT_TEXT)
        self.assertIsNotNone(self.pkg.entries[GUID_MAT]._cache)
        self.assertIsNone(self.pkg.entries[GUID_TEX]._cache)
        self.assertEqual(self.pkg.read_asset(GUID_TEX), b"\x89PNG-fake")
        with self.assertRaises(KeyError):
            self.pkg.read_asset(GUID_FOLDER)

    def test_extract_writes_pathname_tree_and_skips_existing(self):
        dest = Path(self.tmp.name) / "out"
        paths = self.pkg.extract([GUID_TEX, GUID_FBX, GUID_FOLDER, "f" * 32], dest)
        self.assertEqual(set(paths), {GUID_TEX, GUID_FBX})
        self.assertEqual(paths[GUID_TEX], dest / "Assets/Example/Textures/Base.PNG")
        self.assertEqual(paths[GUID_TEX].read_bytes(), b"\x89PNG-fake")
        self.assertEqual(paths[GUID_FBX].stat().st_size, len(b"Kaydara-fake" * 10))
        # 2 回目は同サイズならスキップ（mtime が変わらない）
        before = paths[GUID_TEX].stat().st_mtime_ns
        self.pkg.extract([GUID_TEX], dest)
        self.assertEqual(paths[GUID_TEX].stat().st_mtime_ns, before)

    def test_find_by_path(self):
        self.assertEqual(self.pkg.find_by_path("Assets/Example/Model.fbx").guid, GUID_FBX)
        self.assertIsNone(self.pkg.find_by_path("Assets/Nope"))

    def test_not_a_package(self):
        bad = Path(self.tmp.name) / "bad.unitypackage"
        bad.write_bytes(b"not a tar")
        with self.assertRaises(PackageError):
            UnityPackage(bad).scan()
        empty = Path(self.tmp.name) / "empty.unitypackage"
        with tarfile.open(empty, "w:gz") as tar:
            _add(tar, "README.txt", b"x")
        with self.assertRaises(PackageError):
            UnityPackage(empty).scan()


class SafePathTests(unittest.TestCase):
    def test_accepts_normal_paths(self):
        self.assertEqual(str(safe_relative_path("Assets/A/B.png")), "Assets/A/B.png")
        self.assertEqual(str(safe_relative_path("Assets\\A\\B.png")), "Assets/A/B.png")

    def test_rejects_traversal_and_absolute(self):
        for bad in ("../x", "Assets/../../x", "/etc/passwd", ""):
            with self.assertRaises(PackageError):
                safe_relative_path(bad)


class LocalSampleTests(unittest.TestCase):
    def test_scan_local_packages(self):
        packages = _paths.local_packages()
        if not packages:
            self.skipTest("no local sample packages")
        for path in packages:
            pkg = UnityPackage(path)
            pkg.scan()
            self.assertTrue(pkg.models(), "sample package should contain at least one model")
            self.assertTrue(pkg.materials())
            self.assertTrue(pkg.textures())
            for entry in pkg.materials():
                self.assertTrue(pkg.read_asset(entry.guid).startswith(b"%YAML"))


if __name__ == "__main__":
    unittest.main()
