import unittest

from tests import _paths  # noqa: F401
from tests.test_material import LILTOON_OPAQUE, LILTOON_TRANS
from unitypackage_loader.core.mapping import resolve_materials
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.meta import ModelImporterInfo
from unitypackage_loader.core.prefab import merge_prefab_tables, parse_prefab_materials

MAT_A = "1" * 32
MAT_B = "2" * 32

PREFAB = f"""\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &100
GameObject:
  m_Name: Root
  m_Component:
  - component: {{fileID: 101}}
--- !u!1 &200
GameObject:
  m_Name: Body
--- !u!137 &201
SkinnedMeshRenderer:
  m_GameObject: {{fileID: 200}}
  m_Materials:
  - {{fileID: 2100000, guid: {MAT_A}, type: 2}}
  - {{fileID: 2100000, guid: {MAT_B}, type: 2}}
  m_Mesh: {{fileID: 4300000, guid: 99999999999999999999999999999999, type: 3}}
--- !u!1 &300
GameObject:
  m_Name: Prop
--- !u!23 &301
MeshRenderer:
  m_GameObject: {{fileID: 300}}
  m_Materials:
  - {{fileID: 2100000, guid: {MAT_B}, type: 2}}
  - {{fileID: 0}}
--- !u!1 &400 stripped
GameObject:
  m_CorrespondingSourceObject: {{fileID: 1, guid: 88888888888888888888888888888888, type: 3}}
  m_PrefabInstance: {{fileID: 500}}
--- !u!137 &401 stripped
SkinnedMeshRenderer:
  m_CorrespondingSourceObject: {{fileID: 2, guid: 88888888888888888888888888888888, type: 3}}
  m_PrefabInstance: {{fileID: 500}}
"""


class ParsePrefabTests(unittest.TestCase):
    def test_renderers_by_game_object_name(self):
        table = parse_prefab_materials(PREFAB)
        self.assertEqual(set(table), {"Body", "Prop"})
        self.assertEqual(table["Body"].materials, [MAT_A, MAT_B])
        self.assertEqual(table["Body"].renderer_class, 137)
        self.assertEqual(table["Prop"].materials, [MAT_B, None])
        self.assertEqual(table["Prop"].renderer_class, 23)

    def test_merge_keeps_first(self):
        a = parse_prefab_materials(PREFAB)
        b = parse_prefab_materials(PREFAB.replace(MAT_A, "3" * 32))
        merged = merge_prefab_tables([a, b])
        self.assertEqual(merged["Body"].materials[0], MAT_A)


class PrefabMappingTests(unittest.TestCase):
    def setUp(self):
        self.mats = {
            MAT_A: parse_material(LILTOON_OPAQUE, guid=MAT_A, pathname="Assets/X/Materials/A.mat"),
            MAT_B: parse_material(LILTOON_TRANS, guid=MAT_B, pathname="Assets/X/Materials/B.mat"),
        }
        self.table = parse_prefab_materials(PREFAB)

    def test_prefab_fallback_by_slot(self):
        # FBX 内マテリアル名は .mat 名（Skin/Glass）と一致せず、externalObjects も無い
        object_slots = {"Body": ["FbxMat0", "FbxMat1"], "Prop.001": ["FbxMat1", "FbxMat2"]}
        res = resolve_materials(
            ["FbxMat0", "FbxMat1", "FbxMat2"], ModelImporterInfo(), self.mats, "Assets/X/Model.fbx", self.table, object_slots
        )
        self.assertEqual((res["FbxMat0"].guid, res["FbxMat0"].method), (MAT_A, "prefab"))
        self.assertEqual((res["FbxMat1"].guid, res["FbxMat1"].method), (MAT_B, "prefab"))
        self.assertIsNone(res["FbxMat1"].warning)
        self.assertEqual(res["FbxMat2"].method, "none")  # prefab 側が fileID 0

    def test_conflicting_slots_pick_majority_with_warning(self):
        object_slots = {"Body": ["Shared", "Other"], "Prop": ["Shared"], "Extra": ["Shared"]}
        table = dict(self.table)
        table["Extra"] = type(table["Body"])("Extra", [MAT_B])
        res = resolve_materials(["Shared"], None, self.mats, "", table, object_slots)
        self.assertEqual(res["Shared"].guid, MAT_B)  # Prop と Extra の 2 票
        self.assertIn("different materials", res["Shared"].warning)

    def test_external_objects_win_over_prefab(self):
        info = ModelImporterInfo(external_materials={"FbxMat0": MAT_B})
        res = resolve_materials(["FbxMat0"], info, self.mats, "", self.table, {"Body": ["FbxMat0"]})
        self.assertEqual((res["FbxMat0"].guid, res["FbxMat0"].method), (MAT_B, "external"))


if __name__ == "__main__":
    unittest.main()


class SlotAssignmentTests(unittest.TestCase):
    def test_slot_assignments(self):
        from unitypackage_loader.core.mapping import slot_assignments

        mats = {
            MAT_A: parse_material(LILTOON_OPAQUE, guid=MAT_A),
            MAT_B: parse_material(LILTOON_TRANS, guid=MAT_B),
        }
        table = parse_prefab_materials(PREFAB)
        object_slots = {"Body.001": ["pink", "pink"], "Prop": ["pink", "pink"], "Unknown": ["pink"]}
        result = slot_assignments(object_slots, table, mats)
        self.assertEqual(result, {("Body.001", 0): MAT_A, ("Body.001", 1): MAT_B, ("Prop", 0): MAT_B})
        self.assertEqual(slot_assignments(object_slots, None, mats), {})
