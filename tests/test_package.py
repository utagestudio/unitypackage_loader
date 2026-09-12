import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _paths
from unitypackage_loader.core import package as package_module
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

    def test_extract_refuses_symlinked_directory(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        dest = Path(self.tmp.name) / "out_dir_link"
        (dest / "Assets").mkdir(parents=True)
        # 展開先配下の中間ディレクトリが外側へのリンク
        (dest / "Assets" / "Example").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PackageError):
            self.pkg.extract([GUID_TEX], dest)
        self.assertFalse(any(outside.iterdir()), "nothing must be written through the link")

    def test_extract_refuses_symlinked_file(self):
        outside = Path(self.tmp.name) / "outside_file.bin"
        outside.write_bytes(b"original")
        dest = Path(self.tmp.name) / "out_file_link"
        target = dest / "Assets/Example/Textures/Base.PNG"
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        with self.assertRaises(PackageError):
            self.pkg.extract([GUID_TEX], dest)
        self.assertEqual(outside.read_bytes(), b"original")
        # 上書き指定でも同じ
        with self.assertRaises(PackageError):
            self.pkg.extract([GUID_TEX], dest, overwrite=True)
        self.assertEqual(outside.read_bytes(), b"original")

    def test_extract_allows_symlinked_dest_root(self):
        real = Path(self.tmp.name) / "real_root"
        real.mkdir()
        dest = Path(self.tmp.name) / "root_link"
        dest.symlink_to(real, target_is_directory=True)
        paths = self.pkg.extract([GUID_TEX], dest)
        self.assertEqual(paths[GUID_TEX].read_bytes(), b"\x89PNG-fake")

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

    def test_rejects_windows_drive_and_streams(self):
        # PureWindowsPath("D:/dest") / "C:/evil.txt" は C:\evil.txt になるため、コロンは全て拒否する
        for bad in ("C:/evil.txt", "C:\\evil.txt", "Assets/C:/x.png", "Assets/name.png:stream", "Assets/a:b/c.png"):
            with self.subTest(bad=bad), self.assertRaises(PackageError):
                safe_relative_path(bad)

    def test_rejects_reserved_device_names(self):
        for bad in ("CON", "Assets/nul", "Assets/COM1.png", "Assets/lpt9.tar.gz", "Assets/Aux/x.png"):
            with self.subTest(bad=bad), self.assertRaises(PackageError):
                safe_relative_path(bad)
        # 予約名を含むだけの名前は許可する
        for ok in ("Assets/CONSOLE.png", "Assets/COM10.png", "Assets/nul_mask.png", "Assets/Auxiliary/x.png"):
            with self.subTest(ok=ok):
                safe_relative_path(ok)

    def test_rejects_control_and_special_chars(self):
        for bad in ("Assets/a\x00b.png", "Assets/a\x1bb.png", "Assets/a\x7f.png", "Assets/a\nb.png",
                    "Assets/a?.png", "Assets/a*.png", "Assets/<a>.png", "Assets/a|b.png", 'Assets/a"b.png'):
            with self.subTest(bad=bad), self.assertRaises(PackageError):
                safe_relative_path(bad)

    def test_rejects_trailing_dot_or_space(self):
        for bad in ("Assets/a. /b.png", "Assets/a./b.png", "Assets/b.png.", "Assets/b.png ", "Assets /b.png"):
            with self.subTest(bad=bad), self.assertRaises(PackageError):
                safe_relative_path(bad)

    def test_accepts_unicode_and_dots(self):
        for ok in ("Assets/日本語/テクスチャ.png", "Assets/.hidden/x.png", "Assets/a.b.c.png", "Assets/a b/c d.png", "Assets/./x.png"):
            with self.subTest(ok=ok):
                safe_relative_path(ok)


class SizeLimitTests(unittest.TestCase):
    """メモリへ丸ごと読むメンバーの上限。実際に巨大なファイルは作らず、上限側を小さくして検証する。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "big.unitypackage"
        big_meta = b"fileFormatVersion: 2\nguid: " + GUID_TEX.encode() + b"\n" + b"#" * 600
        with tarfile.open(self.path, "w:gz") as tar:
            _add(tar, f"{GUID_MAT}/pathname", b"Assets/Big/Big.mat")
            _add(tar, f"{GUID_MAT}/asset", MAT_TEXT + b"# " + b"x" * 600)
            _add(tar, f"{GUID_MAT}/asset.meta", b"fileFormatVersion: 2\nguid: " + GUID_MAT.encode() + b"\n")
            _add(tar, f"{GUID_TEX}/pathname", b"Assets/Big/" + b"a" * 600 + b".png")
            _add(tar, f"{GUID_TEX}/asset", b"\x89PNG-fake")
            _add(tar, f"{GUID_TEX}/asset.meta", big_meta)
            _add(tar, f"{GUID_FBX}/pathname", b"Assets/Big/Model.fbx")
            _add(tar, f"{GUID_FBX}/asset", b"Kaydara-fake" * 100)
            _add(tar, f"{GUID_FBX}/asset.meta", b"fileFormatVersion: 2\nguid: " + GUID_FBX.encode() + b"\n")

    def test_oversized_metadata_is_ignored_with_warning(self):
        with mock.patch.object(package_module, "_METADATA_MAX_SIZE", 512):
            pkg = UnityPackage(self.path)
            pkg.scan()
        self.assertEqual(pkg.entries[GUID_TEX].pathname, "", "oversized pathname must not be read")
        self.assertIsNone(pkg.entries[GUID_TEX].meta_text, "oversized asset.meta must not be read")
        self.assertEqual(pkg.entries[GUID_FBX].pathname, "Assets/Big/Model.fbx")
        self.assertEqual(len(pkg.warnings), 2)
        self.assertTrue(all("exceeds" in w for w in pkg.warnings))

    def test_oversized_asset_is_refused_by_read_asset(self):
        with mock.patch.object(package_module, "_CACHE_MAX_SIZE", 0), \
             mock.patch.object(package_module, "_READ_ASSET_MAX_SIZE", 512):
            pkg = UnityPackage(self.path)
            pkg.scan()
            self.assertIsNone(pkg.entries[GUID_MAT]._cache)
            with self.assertRaises(PackageError):
                pkg.read_asset(GUID_MAT)
            with self.assertRaises(PackageError):
                pkg.read_asset(GUID_FBX)
            # 上限内なら再走査して読める
            self.assertEqual(pkg.read_asset(GUID_TEX), b"\x89PNG-fake")
        # extract は上限の対象外（ディスクへ流すだけ）
        with mock.patch.object(package_module, "_READ_ASSET_MAX_SIZE", 512):
            paths = pkg.extract([GUID_FBX], Path(self.tmp.name) / "out")
        self.assertEqual(paths[GUID_FBX].stat().st_size, len(b"Kaydara-fake" * 100))


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
