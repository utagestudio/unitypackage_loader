import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.meta import (
    ModelImporterInfo,
    TextureImporterInfo,
    parse_meta,
    strip_numeric_suffix,
)

MODEL_META = """\
fileFormatVersion: 2
guid: cccccccccccccccccccccccccccccccc
ModelImporter:
  serializedVersion: 22200
  externalObjects:
  - first:
      type: UnityEngine:Material
      assembly: UnityEngine.CoreModule
      name: MatA
    second: {fileID: 2100000, guid: dddddddddddddddddddddddddddddddd, type: 2}
  - first:
      type: UnityEngine:Material
      assembly: UnityEngine.CoreModule
      name: MatA.001
    second: {fileID: 2100000, guid: dddddddddddddddddddddddddddddddd, type: 2}
  - first:
      type: UnityEngine:Material
      assembly: UnityEngine.CoreModule
      name: OnlySuffixed.001
    second: {fileID: 2100000, guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee, type: 2}
  - first:
      type: UnityEngine:Material
      assembly: UnityEngine.CoreModule
      name: Unassigned
    second: {fileID: 0}
  - first:
      type: UnityEngine:AnimationClip
      assembly: UnityEngine.CoreModule
      name: Walk
    second: {fileID: 7400000, guid: ffffffffffffffffffffffffffffffff, type: 2}
  materials:
    materialImportMode: 2
  meshes:
    globalScale: 0.01
    useFileScale: 0
    importBlendShapes: 1
"""

TEXTURE_META = """\
fileFormatVersion: 2
guid: 99999999999999999999999999999999
TextureImporter:
  serializedVersion: 13
  mipmaps:
    sRGBTexture: 0
  textureSettings:
    filterMode: 1
    wrapU: 1
    wrapV: 1
  alphaUsage: 1
  alphaIsTransparency: 1
  textureType: 1
"""


class ModelImporterTests(unittest.TestCase):
    def test_external_materials_only(self):
        info = ModelImporterInfo.from_meta(MODEL_META)
        self.assertEqual(
            info.external_materials,
            {"MatA": "d" * 32, "MatA.001": "d" * 32, "OnlySuffixed.001": "e" * 32},
        )
        self.assertEqual(info.material_import_mode, 2)
        self.assertAlmostEqual(info.global_scale, 0.01)
        self.assertFalse(info.use_file_scale)
        self.assertTrue(info.import_blend_shapes)

    def test_resolve_material(self):
        info = ModelImporterInfo.from_meta(MODEL_META)
        self.assertEqual(info.resolve_material("MatA"), "d" * 32)
        self.assertEqual(info.resolve_material("MatA.002"), "d" * 32)  # Blender 側の連番
        self.assertEqual(info.resolve_material("OnlySuffixed"), "e" * 32)  # Unity 側だけ連番
        self.assertIsNone(info.resolve_material("Unassigned"))
        self.assertIsNone(info.resolve_material("Missing"))

    def test_non_model_meta_gives_empty_info(self):
        info = ModelImporterInfo.from_meta(TEXTURE_META)
        self.assertEqual(info.external_materials, {})
        self.assertIsNone(info.material_import_mode)


class LegacyRecycleNameTests(unittest.TestCase):
    """Unity 2018.2 以前の形式の fileIDToRecycleName（モデルの中のオブジェクトの fileID → 名前）。"""

    META = """\
fileFormatVersion: 2
guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
ModelImporter:
  serializedVersion: 22
  fileIDToRecycleName:
    100000: //RootNode
    100002: door01
    100004: door01_frame
    400000: //RootNode
    400002: door01
    2300000: door01
    4300000: door01
  externalObjects: {}
"""

    def test_table_is_read(self):
        info = ModelImporterInfo.from_meta(self.META)
        self.assertEqual(info.recycle_names[400000], "//RootNode")
        self.assertEqual(info.recycle_names[400002], "door01")
        self.assertEqual(info.recycle_names[100004], "door01_frame")
        self.assertEqual(len(info.recycle_names), 7)

    def test_internal_id_table_is_read(self):
        # 古い番号のまま新しい Unity で開き直したモデルの .meta
        meta = """\
fileFormatVersion: 2
guid: ffffffffffffffffffffffffffffffff
ModelImporter:
  serializedVersion: 22200
  internalIDToNameTable:
  - first:
      1: 100000
    second: //RootNode
  - first:
      43: 4300104
    second: AE_House_01_LOD0
  - first:
      43: bad
    second: Broken
  externalObjects: {}
"""
        info = ModelImporterInfo.from_meta(meta)
        self.assertEqual(info.recycle_names, {100000: "//RootNode", 4300104: "AE_House_01_LOD0"})

    def test_new_format_has_empty_table(self):
        self.assertEqual(ModelImporterInfo.from_meta(MODEL_META).recycle_names, {})


class TextureImporterTests(unittest.TestCase):
    def test_normal_map_linear_clamp(self):
        info = TextureImporterInfo.from_meta(TEXTURE_META)
        self.assertTrue(info.is_normal_map)
        self.assertTrue(info.is_linear)
        self.assertFalse(info.srgb)
        self.assertTrue(info.alpha_is_transparency)
        self.assertTrue(info.clamps)

    def test_defaults(self):
        info = TextureImporterInfo.from_meta(None)
        self.assertFalse(info.is_normal_map)
        self.assertFalse(info.is_linear)
        self.assertFalse(info.clamps)
        info2 = TextureImporterInfo.from_meta("fileFormatVersion: 2\nguid: 1\nNativeFormatImporter:\n  mainObjectFileID: 0\n")
        self.assertEqual(info2, TextureImporterInfo())


class HelpersTests(unittest.TestCase):
    def test_parse_meta(self):
        meta = parse_meta(TEXTURE_META)
        self.assertEqual(meta.guid, "9" * 32)
        self.assertEqual(meta.importer, "TextureImporter")
        self.assertEqual(meta.data["textureType"], 1)

    def test_strip_numeric_suffix(self):
        self.assertEqual(strip_numeric_suffix("Body.001"), "Body")
        self.assertEqual(strip_numeric_suffix("Body.1"), "Body.1")
        self.assertEqual(strip_numeric_suffix("v1.0"), "v1.0")
        self.assertEqual(strip_numeric_suffix("Body"), "Body")
        # Blender は同名が 1000 を超えると 4 桁以上の連番を付ける（#67）
        self.assertEqual(strip_numeric_suffix("Pole.1003"), "Pole")
        self.assertEqual(strip_numeric_suffix("Arm.L.1000000"), "Arm.L")
        self.assertEqual(strip_numeric_suffix("Body.00\u0661"), "Body.00\u0661")  # ASCII 以外の数字は連番とみなさない

    def test_resolve_material_with_long_suffix(self):
        info = ModelImporterInfo(external_materials={"Pole": "a" * 32})
        self.assertEqual(info.resolve_material("Pole.1003"), "a" * 32)


if __name__ == "__main__":
    unittest.main()
