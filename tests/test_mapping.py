"""マテリアルの対応付け（core/mapping.py）。モデルのマテリアルと .mat の解決、prefab の割り当てでの差し替え。

以前は tests/test_material.py の中にあった（#199）。prefab の割り当て（slot_assignments など）は tests/test_prefab.py。
"""

import unittest

from tests import _paths  # noqa: F401
from tests.test_material import LILTOON_OPAQUE, LILTOON_TRANS, TEX_A, TEX_E, TEX_N, UNKNOWN_LILTOON_LIKE
from unitypackage_loader.core.mapping import (
    MaterialResolution,
    expected_materials,
    fully_replaced_materials,
    material_textures,
    resolve_materials,
)
from unitypackage_loader.core.material import NormalizedMaterial, TexRef, parse_material
from unitypackage_loader.core.meta import ModelImporterInfo


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.mats = {
            "1" * 32: parse_material(LILTOON_OPAQUE, guid="1" * 32, pathname="Assets/Chara/Materials/Skin.mat"),
            "2" * 32: parse_material(LILTOON_TRANS, guid="2" * 32, pathname="Assets/Chara/Materials/Glass.mat"),
            "3" * 32: parse_material(UNKNOWN_LILTOON_LIKE, guid="3" * 32, pathname="Assets/Other/Cloth.mat"),
            "4" * 32: parse_material(UNKNOWN_LILTOON_LIKE, guid="4" * 32, pathname="Assets/Chara/Materials/Cloth.mat"),
        }
        self.model_info = ModelImporterInfo(external_materials={"Skin": "1" * 32, "Skin.001": "1" * 32, "Missing": "9" * 32})

    def test_external_then_name_then_none(self):
        res = resolve_materials(
            ["Skin", "Skin.002", "Glass", "Cloth", "Cloth.001", "Nothing", "Missing"],
            self.model_info,
            self.mats,
            model_pathname="Assets/Chara/FBX/Chara.fbx",
        )
        self.assertEqual((res["Skin"].guid, res["Skin"].method), ("1" * 32, "external"))
        self.assertEqual((res["Skin.002"].guid, res["Skin.002"].method), ("1" * 32, "external"))
        self.assertEqual((res["Glass"].guid, res["Glass"].method), ("2" * 32, "name"))
        # 同名候補が複数ならモデルと同じフォルダ階層に近いものを選ぶ
        self.assertEqual(res["Cloth"].guid, "4" * 32)
        self.assertEqual(res["Cloth.001"].guid, "4" * 32)
        self.assertEqual((res["Nothing"].guid, res["Nothing"].method), (None, "none"))
        # externalObjects が指す .mat がパッケージに無ければ名前一致へフォールバック → 無ければ none
        self.assertEqual(res["Missing"].method, "none")

    def test_without_model_info(self):
        res = resolve_materials(["Skin"], None, self.mats)
        self.assertEqual(res["Skin"].method, "name")

    def test_expected_materials(self):
        """読み込む前に分かる、モデル自身の割り当てで使う .mat（#164）。"""
        found = expected_materials(["Skin", "Cloth", "Nothing"], self.model_info, self.mats, "Assets/Chara/FBX/Chara.fbx")
        self.assertEqual(found, {"1" * 32, "4" * 32})
        # FBX のマテリアル名が分からなければ externalObjects の分だけ（パッケージに無い .mat は除く）
        self.assertEqual(expected_materials(None, self.model_info, self.mats), {"1" * 32})
        self.assertEqual(expected_materials(None, None, self.mats), set())
        self.assertEqual(expected_materials([], self.model_info, self.mats), set())

    def test_material_textures(self):
        base = NormalizedMaterial("Base", base_color_tex=TexRef(TEX_A), normal_tex=TexRef(TEX_N))
        matcap = NormalizedMaterial("MatCap", extras={"matcap": {"tex": TEX_E}})
        normalized = {"1" * 32: base, "2" * 32: matcap, "3" * 32: NormalizedMaterial("Plain")}
        self.assertEqual(material_textures(["1" * 32, "2" * 32, "3" * 32, "9" * 32], normalized), {TEX_A, TEX_N, TEX_E})
        self.assertEqual(material_textures(["3" * 32], normalized), set())


class FullyReplacedMaterialsTests(unittest.TestCase):
    """prefab の割り当てで、組み立てても捨てるだけのマテリアルを見分ける（#77）。"""

    def test_material_replaced_in_every_slot(self):
        object_slots = {"Body": ["FbxMat", "Shared"], "Wheel": ["FbxMat"], "Seat": ["Shared"]}
        resolution = {
            "FbxMat": MaterialResolution("FbxMat", "a" * 32, "external"),
            "Shared": MaterialResolution("Shared", "b" * 32, "external"),
        }
        # FbxMat は使われている 2 スロットとも別の .mat に差し替わる。Shared は Seat では差し替わらない
        assignments = {("Body", 0): "c" * 32, ("Wheel", 0): "d" * 32, ("Body", 1): "c" * 32}
        self.assertEqual(fully_replaced_materials(object_slots, assignments, resolution), {"FbxMat"})

    def test_assignment_to_the_same_mat_is_not_a_replacement(self):
        resolution = {"FbxMat": MaterialResolution("FbxMat", "a" * 32, "external")}
        self.assertEqual(fully_replaced_materials({"Body": ["FbxMat"]}, {("Body", 0): "a" * 32}, resolution), set())

    def test_unresolved_material_replaced_everywhere(self):
        resolution = {"Unknown": MaterialResolution("Unknown", None, "none")}
        slots = {"Body": ["Unknown", ""]}
        self.assertEqual(fully_replaced_materials(slots, {("Body", 0): "c" * 32}, resolution), {"Unknown"})
        self.assertEqual(fully_replaced_materials(slots, {}, resolution), set())


if __name__ == "__main__":
    unittest.main()
