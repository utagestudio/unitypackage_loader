import struct
import tempfile
import unittest
from pathlib import Path

from tests import _paths  # noqa: F401
from unitypackage_loader.core.fbx_units import parse_unit_scale, read_unit_scale

HEADER = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400)


def string_property(text: bytes) -> bytes:
    return b"S" + struct.pack("<I", len(text)) + text


def binary_fbx(value_property: bytes, strings=(b"double", b"Number", b"")) -> bytes:
    body = string_property(b"UnitScaleFactor") + b"".join(string_property(s) for s in strings) + value_property
    return HEADER + b"\x00" * 64 + body + b"\x00" * 16


class BinaryTest(unittest.TestCase):
    def test_fbx7_double(self):
        self.assertEqual(parse_unit_scale(binary_fbx(b"D" + struct.pack("<d", 100.0))), 100.0)

    def test_fbx6_style_fewer_strings(self):
        self.assertEqual(parse_unit_scale(binary_fbx(b"D" + struct.pack("<d", 1.0), strings=(b"double",))), 1.0)

    def test_other_numeric_types(self):
        self.assertEqual(parse_unit_scale(binary_fbx(b"F" + struct.pack("<f", 2.54))), struct.unpack("<f", struct.pack("<f", 2.54))[0])
        self.assertEqual(parse_unit_scale(binary_fbx(b"I" + struct.pack("<i", 100))), 100.0)

    def test_missing_or_broken(self):
        self.assertIsNone(parse_unit_scale(HEADER + b"\x00" * 64))
        self.assertIsNone(parse_unit_scale(binary_fbx(b"D" + struct.pack("<d", float("nan")))))
        self.assertIsNone(parse_unit_scale(binary_fbx(b"D" + struct.pack("<d", 0.0))))
        self.assertIsNone(parse_unit_scale(binary_fbx(b"D\x00\x00")))  # 途中で切れている
        self.assertIsNone(parse_unit_scale(binary_fbx(b"X")))


class AsciiTest(unittest.TestCase):
    def test_fbx7(self):
        text = b'GlobalSettings:  {\n\tProperties70:  {\n\t\tP: "UnitScaleFactor", "double", "Number", "",100\n\t}\n}\n'
        self.assertEqual(parse_unit_scale(text), 100.0)

    def test_fbx6(self):
        text = b'; FBX 6.1.0 project file\n    Properties60:  {\n            Property: "UnitScaleFactor", "double", "",1\n        }\n'
        self.assertEqual(parse_unit_scale(text), 1.0)

    def test_missing(self):
        self.assertIsNone(parse_unit_scale(b'; FBX 7.4.0 project file\nObjects: {}\n'))


class ReadFileTest(unittest.TestCase):
    def test_reads_file_and_handles_missing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.fbx"
            path.write_bytes(binary_fbx(b"D" + struct.pack("<d", 100.0)))
            self.assertEqual(read_unit_scale(path), 100.0)
            self.assertIsNone(read_unit_scale(Path(tmp) / "missing.fbx"))


if __name__ == "__main__":
    unittest.main()
