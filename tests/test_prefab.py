import unittest

from tests import _paths  # noqa: F401
from tests.test_material import LILTOON_OPAQUE, LILTOON_TRANS
from unitypackage_loader.core.mapping import resolve_materials
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.meta import ModelImporterInfo
from unitypackage_loader.core.prefab import RendererMaterials, merge_prefab_tables, parse_prefab, tables_by_model

MAT_A = "1" * 32
MAT_B = "2" * 32
MODEL_A = "9" * 32
MODEL_B = "8" * 32

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
  m_Mesh: {{fileID: 4300000, guid: {MODEL_A}, type: 3}}
--- !u!1 &300
GameObject:
  m_Name: Prop
--- !u!33 &302
MeshFilter:
  m_GameObject: {{fileID: 300}}
  m_Mesh: {{fileID: 4300002, guid: {MODEL_A}, type: 3}}
--- !u!23 &301
MeshRenderer:
  m_GameObject: {{fileID: 300}}
  m_Materials:
  - {{fileID: 2100000, guid: {MAT_B}, type: 2}}
  - {{fileID: 0}}
--- !u!1 &600
GameObject:
  m_Name: Body
--- !u!33 &602
MeshFilter:
  m_GameObject: {{fileID: 600}}
  m_Mesh: {{fileID: 4300000, guid: {MODEL_B}, type: 3}}
--- !u!23 &601
MeshRenderer:
  m_GameObject: {{fileID: 600}}
  m_Materials:
  - {{fileID: 2100000, guid: {MAT_B}, type: 2}}
--- !u!1 &700
GameObject:
  m_Name: NoMesh
--- !u!23 &701
MeshRenderer:
  m_GameObject: {{fileID: 700}}
  m_Materials:
  - {{fileID: 2100000, guid: {MAT_A}, type: 2}}
--- !u!1 &400 stripped
GameObject:
  m_CorrespondingSourceObject: {{fileID: 1, guid: 88888888888888888888888888888888, type: 3}}
  m_PrefabInstance: {{fileID: 500}}
--- !u!137 &401 stripped
SkinnedMeshRenderer:
  m_CorrespondingSourceObject: {{fileID: 2, guid: 88888888888888888888888888888888, type: 3}}
  m_PrefabInstance: {{fileID: 500}}
"""


def table_of(text: str = PREFAB, model: str = MODEL_A) -> dict[str, RendererMaterials]:
    return tables_by_model(parse_prefab(text).renderers.values(), [MODEL_A, MODEL_B])[model]


class ParsePrefabTests(unittest.TestCase):
    def test_renderers_with_mesh_reference(self):
        renderers = parse_prefab(PREFAB).renderers
        self.assertEqual(set(renderers), {201, 301, 601, 701})  # stripped は読まない
        self.assertEqual(renderers[201].materials, [MAT_A, MAT_B])
        self.assertEqual((renderers[201].renderer_class, renderers[201].mesh_guid), (137, MODEL_A))
        self.assertEqual(renderers[301].materials, [MAT_B, None])
        self.assertEqual((renderers[301].renderer_class, renderers[301].mesh_guid), (23, MODEL_A))  # MeshFilter から
        self.assertIsNone(renderers[701].mesh_guid)

    def test_tables_split_by_model(self):
        tables = tables_by_model(parse_prefab(PREFAB).renderers.values(), [MODEL_A, MODEL_B])
        self.assertEqual(set(tables), {MODEL_A, MODEL_B})
        self.assertEqual(set(tables[MODEL_A]), {"Body", "Prop"})  # メッシュ参照の無い NoMesh は使わない
        # 別モデルの同名 GameObject は、それぞれのモデルの表に入る
        self.assertEqual(tables[MODEL_A]["Body"].materials, [MAT_A, MAT_B])
        self.assertEqual(tables[MODEL_B]["Body"].materials, [MAT_B])

    def test_tables_skip_meshes_outside_package(self):
        self.assertEqual(set(tables_by_model(parse_prefab(PREFAB).renderers.values(), [MODEL_B])), {MODEL_B})
        self.assertEqual(tables_by_model(parse_prefab(PREFAB).renderers.values(), []), {})

    def test_merge_keeps_first(self):
        merged = merge_prefab_tables([table_of(), table_of(PREFAB.replace(MAT_A, "3" * 32))])
        self.assertEqual(merged["Body"].materials[0], MAT_A)


class PrefabMappingTests(unittest.TestCase):
    def setUp(self):
        self.mats = {
            MAT_A: parse_material(LILTOON_OPAQUE, guid=MAT_A, pathname="Assets/X/Materials/A.mat"),
            MAT_B: parse_material(LILTOON_TRANS, guid=MAT_B, pathname="Assets/X/Materials/B.mat"),
        }
        self.table = table_of()

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
        table["Extra"] = RendererMaterials("Extra", [MAT_B])
        res = resolve_materials(["Shared"], None, self.mats, "", table, object_slots)
        self.assertEqual(res["Shared"].guid, MAT_B)  # Prop と Extra の 2 票
        self.assertIn("different materials", res["Shared"].warning)

    def test_external_objects_win_over_prefab(self):
        info = ModelImporterInfo(external_materials={"FbxMat0": MAT_B})
        res = resolve_materials(["FbxMat0"], info, self.mats, "", self.table, {"Body": ["FbxMat0"]})
        self.assertEqual((res["FbxMat0"].guid, res["FbxMat0"].method), (MAT_B, "external"))

    def test_prefab_fallback_follows_submesh_order(self):
        # ポリゴンがスロット 1 から使われていれば、m_Materials[0] はスロット 1 のマテリアル
        object_slots = {"Body": ["FbxMat0", "FbxMat1"]}
        res = resolve_materials(
            ["FbxMat0", "FbxMat1"], None, self.mats, "", self.table, object_slots, {"Body": [1, 0]}
        )
        self.assertEqual(res["FbxMat0"].guid, MAT_B)
        self.assertEqual(res["FbxMat1"].guid, MAT_A)


class SlotAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.mats = {
            MAT_A: parse_material(LILTOON_OPAQUE, guid=MAT_A),
            MAT_B: parse_material(LILTOON_TRANS, guid=MAT_B),
        }

    def test_slot_assignments(self):
        from unitypackage_loader.core.mapping import slot_assignments

        object_slots = {"Body.001": ["pink", "pink"], "Prop": ["pink", "pink"], "Unknown": ["pink"]}
        result = slot_assignments(object_slots, table_of(), self.mats)
        self.assertEqual(result, {("Body.001", 0): MAT_A, ("Body.001", 1): MAT_B, ("Prop", 0): MAT_B})
        self.assertEqual(slot_assignments(object_slots, None, self.mats), {})

    def test_slot_assignments_use_only_the_models_table(self):
        from unitypackage_loader.core.mapping import slot_assignments

        # 別モデルの表では、同名の Body に別モデル側の割り当てが使われる
        result = slot_assignments({"Body": ["pink", "pink"]}, table_of(model=MODEL_B), self.mats)
        self.assertEqual(result, {("Body", 0): MAT_B})

    def test_submesh_slot_order(self):
        from unitypackage_loader.core.mapping import submesh_slot_order

        # 最初に使われた順。使われないスロット（1）と範囲外の番号は含めない
        self.assertEqual(submesh_slot_order([0, 0, 3, 2, 3, 0, 9, -1], 4), [0, 3, 2])
        self.assertEqual(submesh_slot_order([], 2), [])

    def test_slot_assignments_follow_submesh_order(self):
        from unitypackage_loader.core.mapping import slot_assignments

        # Body のポリゴンはスロット 2 から使われ、スロット 1 はポリゴンが無い（Unity ではサブメッシュにならない）
        object_slots = {"Body": ["Skin", "Unused", "Hair"]}
        result = slot_assignments(object_slots, table_of(), self.mats, {"Body": [2, 0]})
        self.assertEqual(result, {("Body", 2): MAT_A, ("Body", 0): MAT_B})


if __name__ == "__main__":
    unittest.main()
