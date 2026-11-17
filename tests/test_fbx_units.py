import struct
import tempfile
import unittest
from pathlib import Path

from tests import _paths  # noqa: F401
from unitypackage_loader.core.fbx_units import (
    Y_UP_AXES,
    blender_axis_matrix,
    parse_axes,
    parse_unit_scale,
    read_unit_scale,
    unity_axis_matrix,
)

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


def axis_fbx(values) -> bytes:
    """GlobalSettings の軸の 6 つの整数属性（I）だけを持つバイナリ FBX。"""
    names = (b"UpAxis", b"UpAxisSign", b"FrontAxis", b"FrontAxisSign", b"CoordAxis", b"CoordAxisSign")
    body = b"".join(
        string_property(name) + string_property(b"int") + string_property(b"Integer") + string_property(b"") + b"I" + struct.pack("<i", v)
        for name, v in zip(names, values)
    )
    return HEADER + b"\x00" * 64 + body + b"\x00" * 16


MAX_AXES = (2, 1, 1, -1, 0, 1)  # 3ds Max の FBX（Z が上、前が -Y。Cardboard Boxes Pack の box0.FBX）


class AxesTest(unittest.TestCase):
    """FBX の軸（#240）。"""

    def test_parse(self):
        self.assertEqual(parse_axes(axis_fbx(MAX_AXES)), MAX_AXES)
        self.assertEqual(parse_axes(axis_fbx(Y_UP_AXES)), Y_UP_AXES)
        ascii_fbx = b"".join(b'P: "%s", "int", "Integer", "",%d\n' % (n, v) for n, v in zip(
            (b"UpAxis", b"UpAxisSign", b"FrontAxis", b"FrontAxisSign", b"CoordAxis", b"CoordAxisSign"), MAX_AXES))
        self.assertEqual(parse_axes(ascii_fbx), MAX_AXES)

    def test_broken_axes(self):
        self.assertIsNone(parse_axes(HEADER))
        self.assertIsNone(parse_axes(axis_fbx((1, 1, 1, 1, 0, 1))))  # 軸が重なる
        self.assertIsNone(parse_axes(axis_fbx((1, 2, 2, 1, 0, 1))))  # 向きが ±1 でない

    def test_matrices(self):
        # Y が上: Blender は X 軸のまわりに 90° 回し、Unity は X を反転する（C·diag(-1, 1, 1) = B なので今までの式と同じ）
        self.assertEqual(blender_axis_matrix(Y_UP_AXES), [[1, 0, 0], [0, 0, -1], [0, 1, 0]])
        self.assertEqual(unity_axis_matrix(Y_UP_AXES), [[-1, 0, 0], [0, 1, 0], [0, 0, 1]])
        # Z が上（3ds Max）: Blender は回転を掛けず、Unity は Y と Z を入れ替える
        self.assertEqual(blender_axis_matrix(MAX_AXES), [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
        self.assertEqual(unity_axis_matrix(MAX_AXES), [[1, 0, 0], [0, 0, 1], [0, 1, 0]])
        self.assertEqual(blender_axis_matrix(None), blender_axis_matrix(Y_UP_AXES))

