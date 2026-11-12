"""モデルの中のオブジェクトの fileID の計算と、読み込んだオブジェクトとの突き合わせ（Issue #31）。

fileID の期待値は、Unity 6000.6.0f1 に合成 FBX を読み込ませて書き出した表の値。
"""

import struct
import unittest
from collections import Counter

from tests import _paths  # noqa: F401
from unitypackage_loader.core.hierarchy import (
    MODEL_ROOT_GAME_OBJECT,
    MODEL_ROOT_TRANSFORM,
    NAMED_MODEL_ROOT_GAME_OBJECT,
    NAMED_MODEL_ROOT_TRANSFORM,
)
from unitypackage_loader.core.model_ids import ModelObject, object_claims, parse_fbx_node_names
from unitypackage_loader.core.unity_ids import hierarchy_file_id, named_file_id

MASK = 0x7FFF_FFFF_FFFF_FFFF


def uid(file_id: int) -> int:
    return file_id & MASK


def obj(*path: str, mesh: bool = True) -> ModelObject:
    return ModelObject(path[-1], [tuple(path)], mesh)


class FileIdTests(unittest.TestCase):
    def test_hierarchy_generation_matches_unity(self):
        # fileIdsGeneration: 2。GameObject はパスだけ、コンポーネントはパスの後ろに型の名前が付く
        for type_name, path, expected in (
            ("GameObject", ["Body"], 635547659545568360),
            ("Transform", ["Body"], 4167280743394806330),
            ("MeshFilter", ["Body"], -3387599253219119134),
            ("MeshRenderer", ["Body"], -6652725513331149018),
            ("SkinnedMeshRenderer", ["SkinnedBody"], -4846179264645043155),
            ("GameObject", ["Group", "Child"], -7444120488523187281),
            ("MeshRenderer", ["Group", "Child"], -4832035873489392071),
            ("Transform", ["Armature", "Hips", "Spine"], -9162484748627739507),
            ("MeshRenderer", ["Mesh With Space"], 5604173093713927214),
            ("MeshRenderer", ["日本語メッシュ"], -8254092097463626329),
            ("MeshRenderer", ["DupA 1"], 3167895427419945141),  # 兄弟の同名は Unity が「名前 1」に変える
        ):
            self.assertEqual(hierarchy_file_id(type_name, path), expected, (type_name, path))

    def test_named_generation_matches_unity(self):
        # fileIdsGeneration: 1（キーの無い古い .meta も）。階層によらず名前だけで決まる
        for type_name, name, expected in (
            ("GameObject", "GrpA", 5455556844399779698),
            ("Transform", "GrpA", 3982066435450333063),
            ("GameObject", "Deep", 3963281400172713605),
            ("MeshRenderer", "PartQ 1", -2378933403572008389),
        ):
            self.assertEqual(named_file_id(type_name, name), expected, (type_name, name))

    def test_root_ids(self):
        self.assertEqual(hierarchy_file_id("GameObject", []), MODEL_ROOT_GAME_OBJECT)
        self.assertEqual(hierarchy_file_id("Transform", []), MODEL_ROOT_TRANSFORM)
        self.assertEqual(named_file_id("GameObject"), NAMED_MODEL_ROOT_GAME_OBJECT)
        self.assertEqual(named_file_id("Transform"), NAMED_MODEL_ROOT_TRANSFORM)
        # 1 メッシュの FBX でルートに付く Renderer（Shibuya.unitypackage の SM_MetroEntrance_02 の値）
        self.assertEqual(hierarchy_file_id("MeshRenderer", []), -7511558181221131132)


class ClaimTests(unittest.TestCase):
    def test_both_generations_are_claimed(self):
        objects = [obj("Body"), obj("Group", mesh=False), obj("Group", "Child"), obj("SkinnedBody")]
        claims = object_claims(objects)
        self.assertEqual(claims[uid(-6652725513331149018)], "Body")
        self.assertEqual(claims[uid(-4832035873489392071)], "Child")
        self.assertEqual(claims[uid(-4846179264645043155)], "SkinnedBody")
        self.assertEqual(claims[uid(named_file_id("MeshRenderer", "Child"))], "Child")
        self.assertNotIn("Group", claims.values())  # メッシュでないオブジェクトは返さない

    def test_single_top_level_node_is_collapsed_into_the_root(self):
        # 最上位が 1 つだけなら Unity はルートに畳むので、子のパスは 1 段浅くなる（UnityJapanOffice の Office.fbx の形）
        claims = object_claims([obj("Office", mesh=False), obj("Office", "Wall_A")])
        self.assertEqual(claims[uid(hierarchy_file_id("MeshRenderer", ["Wall_A"]))], "Wall_A")
        self.assertEqual(claims[uid(hierarchy_file_id("MeshRenderer", ["Office", "Wall_A"]))], "Wall_A")  # 畳まない形も試す
        # メッシュ 1 つだけのモデルは、Renderer がルートに付く
        claims = object_claims([obj("Kiosk")])
        self.assertEqual(claims[uid(hierarchy_file_id("MeshRenderer", []))], "Kiosk")
        self.assertEqual(claims[uid(named_file_id("MeshRenderer"))], "Kiosk")
        # 最上位が複数あれば畳まれない
        claims = object_claims([obj("Kiosk"), obj("Sign")])
        self.assertNotIn(uid(hierarchy_file_id("MeshRenderer", [])), claims)

    def test_renamed_objects_use_the_fbx_name(self):
        # 先に読み込んだものと名前がぶつかって Blender が連番を付けても、FBX の名前が 1 つならそれで計算する
        native = Counter({"Body": 1, "Hair": 1})
        claims = object_claims([obj("Body.001"), obj("Hair.001")], native=native, taken={"Body", "Hair"})
        self.assertEqual(claims[uid(hierarchy_file_id("MeshRenderer", ["Body"]))], "Body.001")
        self.assertEqual(claims[uid(named_file_id("MeshRenderer", "Hair"))], "Hair.001")

    def test_native_numeric_suffix_is_kept(self):
        # FBX にもともと「Ring」と「Ring.001」がある。ぶつかる相手がいなければ Blender の名前がそのまま FBX の名前
        native = Counter({"Ring": 1, "Ring.001": 1})
        claims = object_claims([obj("Ring"), obj("Ring.001")], native=native)
        self.assertEqual(claims[uid(hierarchy_file_id("MeshRenderer", ["Ring"]))], "Ring")
        self.assertEqual(claims[uid(hierarchy_file_id("MeshRenderer", ["Ring.001"]))], "Ring.001")
        # 2 回目の読み込みで「Ring.002」「Ring.003」になると、どちらがどちらか決められない
        again = object_claims([obj("Ring.002"), obj("Ring.003")], native=native, taken={"Ring"})
        self.assertEqual(again, {})

    def test_duplicate_names_are_not_claimed_unless_the_path_differs(self):
        # 同じ名前のノードが複数ある FBX。兄弟の中の順も、名前で決まる世代のオフセットの順も Blender からは分からない
        native = Counter({"GrpA": 1, "GrpB": 1, "PartQ": 3})
        objects = [
            obj("GrpA", mesh=False), obj("GrpB", mesh=False),
            obj("GrpA", "PartQ"), obj("GrpA", "PartQ.001"), obj("GrpB", "PartQ.002"),
        ]
        claims = object_claims(objects, native=native)
        # 親が違えば階層パスで決まる（Unity の表の値）
        self.assertEqual(claims[uid(6093080660470075438)], "PartQ.002")
        self.assertEqual(set(claims.values()), {"PartQ.002"})

    def test_without_fbx_names_only_unique_names_are_claimed(self):
        claims = object_claims([obj("Body"), obj("Ring"), obj("Ring.001")])
        self.assertEqual(set(claims.values()), {"Body"})
        self.assertEqual(object_claims([obj("Body"), obj("Hair")], taken={"Body"}).get(
            uid(hierarchy_file_id("MeshRenderer", ["Body"]))), None)

    def test_same_file_id_from_two_objects_is_dropped(self):
        # メッシュでないオブジェクトとぶつかっても返さない
        native = Counter({"Part": 2, "Grp": 1})
        claims = object_claims([obj("Grp", mesh=False), obj("Grp", "Part"), obj("Grp", "Part.001", mesh=False)], native=native)
        self.assertEqual(claims, {})

    def test_bone_parent_paths(self):
        # ボーンの子のメッシュ。アーマチュアのオブジェクトを含む並びと含まない並びの両方を渡せる
        prop = ModelObject("Prop", [("Armature", "Hips", "Spine", "Prop"), ("Hips", "Spine", "Prop")])
        claims = object_claims([ModelObject("Armature", [("Armature",)], False), obj("Body"), prop])
        self.assertEqual(claims[uid(hierarchy_file_id("MeshRenderer", ["Armature", "Hips", "Spine", "Prop"]))], "Prop")
        self.assertEqual(claims[uid(hierarchy_file_id("MeshRenderer", ["Hips", "Spine", "Prop"]))], "Prop")


class FbxNodeNameTests(unittest.TestCase):
    @staticmethod
    def binary(names):
        data = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7400)
        for index, name in enumerate(names):
            value = name.encode() + b"\x00\x01Model"
            data += b"\x05Model" + b"L" + struct.pack("<q", index) + b"S" + struct.pack("<I", len(value)) + value
            data += b"S" + struct.pack("<I", 4) + b"Mesh"
        return data

    def test_binary(self):
        names = parse_fbx_node_names(self.binary(["Body", "日本語メッシュ", "Part", "Part", ""]))
        self.assertEqual(names, Counter({"Body": 1, "日本語メッシュ": 1, "Part": 2, "": 1}))

    def test_ascii(self):
        text = (
            '; FBX 7.4.0 project file\nObjects:  {\n'
            '\tModel: 1234, "Model::Body", "Mesh" {\n\t}\n'
            '\tModel: 1235, "Model::Mesh With Space", "Null" {\n\t}\n'
            '    Model: "Model::Old", "Mesh" {\n    }\n'
            '\tMaterial: 1, "Material::Model", "" {\n\t}\n'
        )
        self.assertEqual(parse_fbx_node_names(text.encode()), Counter({"Body": 1, "Mesh With Space": 1, "Old": 1}))

    def test_broken_input(self):
        for data in (b"", b"Kaydara FBX Binary", b"Kaydara FBX Binary  \x00\x01Model", b"\x00\x01Model", b"S\xff\xff\xff\xffx\x00\x01Model"):
            self.assertIsNone(parse_fbx_node_names(data))


if __name__ == "__main__":
    unittest.main()
