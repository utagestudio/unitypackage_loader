import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LicenseCopyTest(unittest.TestCase):
    """配布 zip は unitypackage_loader/ の中だけを詰めるので、そこに置いた LICENSE がルートのものと同じであること。"""

    def test_addon_license_matches_root(self):
        root = (ROOT / "LICENSE").read_bytes()
        addon = (ROOT / "unitypackage_loader" / "LICENSE").read_bytes()
        self.assertEqual(addon, root)


if __name__ == "__main__":
    unittest.main()
