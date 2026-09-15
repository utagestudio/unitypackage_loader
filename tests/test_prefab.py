import unittest

from tests import _paths  # noqa: F401
from tests.test_material import LILTOON_OPAQUE, LILTOON_TRANS
from unitypackage_loader.core.mapping import resolve_materials
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.meta import ModelImporterInfo
from unitypackage_loader.core.hierarchy import Expander, parse_asset
from unitypackage_loader.core.prefab import RendererMaterials, merge_prefab_tables, tables_from_hierarchy

MAT_A = "1" * 32
MAT_B = "2" * 32
MAT_C = "5" * 32
MODEL_A = "9" * 32
MODEL_B = "8" * 32
BASE = "7" * 32
VARIANT = "6" * 32
MODELS = {MODEL_A: "ModelA", MODEL_B: "ModelB"}

PREFAB = f"""\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &100
GameObject:
  m_Name: Root
  m_Component:
  - component: {{fileID: 101}}
--- !u!4 &101
Transform:
  m_GameObject: {{fileID: 100}}
  m_Father: {{fileID: 0}}
--- !u!1 &200
GameObject:
  m_Name: Body
--- !u!4 &202
Transform:
  m_GameObject: {{fileID: 200}}
  m_Father: {{fileID: 101}}
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
--- !u!4 &303
Transform:
  m_GameObject: {{fileID: 300}}
  m_Father: {{fileID: 101}}
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
--- !u!4 &603
Transform:
  m_GameObject: {{fileID: 600}}
  m_Father: {{fileID: 101}}
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
--- !u!4 &702
Transform:
  m_GameObject: {{fileID: 700}}
  m_Father: {{fileID: 101}}
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


def reader(assets: dict[str, str]):
    def read(guid: str):
        text = assets.get(guid)
        return parse_asset(text) if text is not None else None

    return read


def expand(guid: str, assets: dict[str, str], models=MODELS):
    return Expander(reader(assets), models).expand_asset(guid)


def tables_of(guid: str, assets: dict[str, str], models=MODELS) -> dict[str, dict[str, RendererMaterials]]:
    return tables_from_hierarchy(expand(guid, assets, models), models)


def table_of(text: str = PREFAB, model: str = MODEL_A) -> dict[str, RendererMaterials]:
    return tables_of(BASE, {BASE: text})[model]


class PrefabTableTests(unittest.TestCase):
    def test_renderers_with_mesh_reference(self):
        table = table_of()
        self.assertEqual(set(table), {"Body", "Prop"})  # メッシュ参照の無い NoMesh と stripped は使わない
        body, prop = table["Body"], table["Prop"]
        self.assertEqual((body.materials, body.renderer_class, body.mesh_guid, body.mesh_file_id), ([MAT_A, MAT_B], 137, MODEL_A, 4300000))
        self.assertEqual((prop.materials, prop.renderer_class, prop.mesh_file_id), ([MAT_B, None], 23, 4300002))  # MeshFilter から

    def test_tables_split_by_model(self):
        tables = tables_of(BASE, {BASE: PREFAB})
        self.assertEqual(set(tables), {MODEL_A, MODEL_B})
        # 別モデルの同名 GameObject は、それぞれのモデルの表に入る
        self.assertEqual(tables[MODEL_A]["Body"].materials, [MAT_A, MAT_B])
        self.assertEqual(tables[MODEL_B]["Body"].materials, [MAT_B])

    def test_tables_skip_meshes_outside_package(self):
        self.assertEqual(set(tables_of(BASE, {BASE: PREFAB}, {MODEL_B: "ModelB"})), {MODEL_B})
        self.assertEqual(tables_of(BASE, {BASE: PREFAB}, {}), {})

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


def variant_yaml(instance_id: int, source: str, modifications: list[tuple[int, str, str, str | None]]) -> str:
    """(対象 fileID, propertyPath, value, objectReference の GUID) を並べた PrefabInstance だけの prefab。"""
    lines = [
        "%YAML 1.1",
        "%TAG !u! tag:unity3d.com,2011:",
        f"--- !u!1001 &{instance_id}",
        "PrefabInstance:",
        "  serializedVersion: 2",
        "  m_Modification:",
        "    serializedVersion: 3",
        "    m_TransformParent: {fileID: 0}",
        "    m_Modifications:",
    ]
    for target, path, value, ref in modifications:
        obj = f"{{fileID: 2100000, guid: {ref}, type: 2}}" if ref else "{fileID: 0}"
        lines += [
            f"    - target: {{fileID: {target}, guid: {source}, type: 3}}",
            f"      propertyPath: {path}",
            f"      value: {value}",
            f"      objectReference: {obj}",
        ]
    lines += ["    m_RemovedComponents: []", f"  m_SourcePrefab: {{fileID: 100100000, guid: {source}, type: 3}}"]
    return "\n".join(lines) + "\n"


class PrefabVariantTests(unittest.TestCase):
    def test_variant_overrides_base_materials(self):
        variant = variant_yaml(5000, BASE, [
            (201, "m_Materials.Array.data[1]", "", MAT_C),
            (301, "m_Materials.Array.data[0]", "", None),  # 参照を外す上書き
        ])
        expander = Expander(reader({BASE: PREFAB, VARIANT: variant}), MODELS)
        tables = tables_from_hierarchy(expander.expand_asset(VARIANT), MODELS)
        body = tables[MODEL_A]["Body"]
        self.assertEqual((body.game_object, body.materials, body.mesh_guid), ("Body", [MAT_A, MAT_C], MODEL_A))
        self.assertEqual(tables[MODEL_A]["Prop"].materials, [None, None])
        self.assertEqual(tables[MODEL_B]["Body"].materials, [MAT_B])  # 上書きの無い Renderer はそのまま
        # 展開結果をキャッシュしている元 prefab 自体の表は変わらない
        self.assertEqual(tables_from_hierarchy(expander.expand_asset(BASE), MODELS)[MODEL_A]["Body"].materials, [MAT_A, MAT_B])

    def test_renamed_game_object_keeps_mesh_reference(self):
        variant = variant_yaml(5000, BASE, [(200, "m_Name", "Renamed", None)])
        table = tables_of(VARIANT, {BASE: PREFAB, VARIANT: variant})[MODEL_A]
        self.assertEqual(set(table), {"Renamed", "Prop"})
        self.assertEqual(table["Renamed"].mesh_file_id, 4300000)  # 名前で引けないとき mapping がハッシュで照合する

    def test_variant_of_variant_uses_combined_file_ids(self):
        outer = "4" * 32
        assets = {
            BASE: PREFAB,
            VARIANT: variant_yaml(5000, BASE, [(201, "m_Materials.Array.data[0]", "", MAT_C)]),
            # Variant の Variant は、中間の Variant の中での fileID（5000 XOR 201）を対象にする
            outer: variant_yaml(9000, VARIANT, [(5000 ^ 201, "m_Materials.Array.data[1]", "", MAT_A)]),
        }
        self.assertEqual(tables_of(outer, assets)[MODEL_A]["Body"].materials, [MAT_C, MAT_A])

    def test_array_size_override(self):
        # Unity は propertyPath の順に書き出すので、data[N] が size より先に来る
        variant = variant_yaml(5000, BASE, [
            (201, "m_Materials.Array.data[1]", "", MAT_C),  # 縮めた長さの外は使わない
            (201, "m_Materials.Array.size", "1", None),
            (601, "m_Materials.Array.data[1]", "", MAT_A),
            (601, "m_Materials.Array.size", "2", None),
        ])
        tables = tables_of(VARIANT, {BASE: PREFAB, VARIANT: variant})
        self.assertEqual(tables[MODEL_A]["Body"].materials, [MAT_A])
        self.assertEqual(tables[MODEL_B]["Body"].materials, [MAT_B, MAT_A])

    def test_huge_slot_index_is_ignored(self):
        variant = variant_yaml(5000, BASE, [(201, "m_Materials.Array.data[99999999]", "", MAT_C)])
        self.assertEqual(tables_of(VARIANT, {BASE: PREFAB, VARIANT: variant})[MODEL_A]["Body"].materials, [MAT_A, MAT_B])

    def test_cyclic_instances_terminate(self):
        assets = {BASE: variant_yaml(5000, VARIANT, []), VARIANT: variant_yaml(6000, BASE, [])}
        self.assertEqual(tables_of(BASE, assets), {})

    def test_overrides_on_model_objects_are_counted(self):
        # 元がモデルなので、中のオブジェクトの fileID を名前に結び付けられない（名前の上書きは数えない）
        on_model = expand(VARIANT, {VARIANT: variant_yaml(5000, MODEL_A, [
            (123456789, "m_Materials.Array.data[0]", "", MAT_C),
            (123456789, "m_Name", "Renamed", None),
        ])})
        self.assertEqual((on_model.unresolved_overrides, on_model.unresolved_material_overrides), (1, 1))
        self.assertEqual(tables_from_hierarchy(on_model, MODELS), {})  # FBX を置いただけの prefab はモデルを使うものとして数えない
        moved = expand(VARIANT, {VARIANT: variant_yaml(5000, MODEL_A, [(123456789, "m_LocalPosition.x", "1", None)])})
        self.assertEqual((moved.unresolved_overrides, moved.unresolved_material_overrides), (1, 0))
        resolvable = expand(VARIANT, {BASE: PREFAB, VARIANT: variant_yaml(5000, BASE, [(201, "m_Materials.Array.data[0]", "", MAT_C)])})
        self.assertEqual(resolvable.unresolved_overrides, 0)


class MergeByMeshTests(unittest.TestCase):
    def test_earlier_table_claims_the_mesh(self):
        renamed = {"Renamed": RendererMaterials("Renamed", [MAT_C], mesh_file_id=5)}
        base = {"Body": RendererMaterials("Body", [MAT_A], mesh_file_id=5), "Prop": RendererMaterials("Prop", [MAT_B], mesh_file_id=6)}
        self.assertEqual(set(merge_prefab_tables([renamed, base])), {"Renamed", "Prop"})

    def test_rows_sharing_a_mesh_in_one_table_are_kept(self):
        wheels = {"WheelL": RendererMaterials("WheelL", [MAT_A], mesh_file_id=7), "WheelR": RendererMaterials("WheelR", [MAT_B], mesh_file_id=7)}
        self.assertEqual(set(merge_prefab_tables([wheels, dict(wheels)])), {"WheelL", "WheelR"})


class RenamedGameObjectMappingTests(unittest.TestCase):
    def test_slot_assignments_fall_back_to_mesh_file_id(self):
        from unitypackage_loader.core.mapping import slot_assignments
        from unitypackage_loader.core.unity_ids import mesh_file_id

        mats = {MAT_A: parse_material(LILTOON_OPAQUE, guid=MAT_A)}
        table = {"Renamed": RendererMaterials("Renamed", [MAT_A], mesh_guid=MODEL_A, mesh_file_id=mesh_file_id("Body"))}
        self.assertEqual(slot_assignments({"Body.001": ["x"]}, table, mats), {("Body.001", 0): MAT_A})
        self.assertEqual(slot_assignments({"Other": ["x"]}, table, mats), {})


if __name__ == "__main__":
    unittest.main()
