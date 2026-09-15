import struct
import unittest

from tests import _paths  # noqa: F401  (sys.path 設定)
from tests import unity_binary_writer as w
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.hierarchy import parse_asset
from unitypackage_loader.core.unity_binary import (
    COMMON_STRINGS,
    UnityBinaryError,
    is_serialized_file,
    load_documents,
    parse_serialized_file,
)
from unitypackage_loader.core.unity_yaml import UnityRef

# 手書きの合成フィクスチャ。実在アセットの値は使わない。
TEX = "0123456789abcdef0123456789abcdef"
SHADER = "a" * 32
MAT_A = "1" * 32
MODEL = "9" * 32

LEGACY_TEXTURES = [("_BumpMap", None, (1.0, 1.0), (0.0, 0.0)), ("_MainTex", TEX, (2.0, 1.0), (0.0, 0.5))]
LEGACY_FLOATS = [("_Cutoff", 0.25), ("_Mode", 1.0)]
LEGACY_COLORS = [("_Color", (0.5, 0.75, 1.0, 1.0))]

# LEGACY_* から作ったバイナリと同じ内容の Unity YAML
LEGACY_YAML = f"""\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!21 &2100000
Material:
  m_ObjectHideFlags: 0
  m_Name: BinaryExample
  m_Shader: {{fileID: 46, guid: 0000000000000000f000000000000000, type: 0}}
  m_ShaderKeywords: _ALPHATEST_ON _NORMALMAP
  m_CustomRenderQueue: 2450
  m_SavedProperties:
    serializedVersion: 3
    m_TexEnvs:
    - _BumpMap:
        m_Texture: {{fileID: 0}}
        m_Scale: {{x: 1, y: 1}}
        m_Offset: {{x: 0, y: 0}}
    - _MainTex:
        m_Texture: {{fileID: 2800000, guid: {TEX}, type: 3}}
        m_Scale: {{x: 2, y: 1}}
        m_Offset: {{x: 0, y: 0.5}}
    m_Floats:
    - _Cutoff: 0.25
    - _Mode: 1
    m_Colors:
    - _Color: {{r: 0.5, g: 0.75, b: 1, a: 1}}
"""


def legacy_material(**kwargs) -> bytes:
    return w.material(
        "BinaryExample", keywords=["_ALPHATEST_ON", "_NORMALMAP"], render_queue=2450,
        textures=LEGACY_TEXTURES, floats=LEGACY_FLOATS, colors=LEGACY_COLORS, **kwargs,
    )


class CommonStringTest(unittest.TestCase):
    def test_offsets_match_published_table(self):
        self.assertEqual(COMMON_STRINGS[0], "AABB")
        self.assertEqual(COMMON_STRINGS[427], "m_Name")
        self.assertEqual(COMMON_STRINGS[934], "unsigned int")
        self.assertEqual(COMMON_STRINGS[1161], "Hash128")
        for text, offset in w.COMMON_OFFSETS.items():
            self.assertEqual(COMMON_STRINGS[offset], text)


class DetectionTest(unittest.TestCase):
    def test_binary_and_text(self):
        self.assertTrue(is_serialized_file(legacy_material()))
        self.assertTrue(is_serialized_file(legacy_material(version=22)))
        self.assertFalse(is_serialized_file(LEGACY_YAML.encode()))
        self.assertFalse(is_serialized_file(b"\0" * 8))
        self.assertFalse(is_serialized_file(legacy_material()[:-1]))

    def test_load_documents_dispatches(self):
        for data in (LEGACY_YAML, LEGACY_YAML.encode(), legacy_material()):
            docs = load_documents(data)
            self.assertEqual([d.type_name for d in docs], ["Material"])
            self.assertEqual(docs[0].class_id, 21)
            self.assertEqual(docs[0].file_id, 2100000)


class BinaryMaterialTest(unittest.TestCase):
    def test_legacy_layout_matches_yaml(self):
        expected = parse_material(LEGACY_YAML)
        for kwargs in ({}, {"big_endian": True}, {"version": 22}):
            with self.subTest(**kwargs):
                self.assertEqual(parse_material(legacy_material(**kwargs)), expected)

    def test_values(self):
        mat = parse_material(legacy_material())
        self.assertEqual(mat.name, "BinaryExample")
        self.assertEqual(mat.shader_builtin_id, 46)
        self.assertEqual(mat.keywords, ["_ALPHATEST_ON", "_NORMALMAP"])
        self.assertEqual(mat.textures["_MainTex"].guid, TEX)
        self.assertEqual(mat.textures["_MainTex"].scale, (2.0, 1.0))
        self.assertNotIn("_BumpMap", mat.textures)
        self.assertIn("_BumpMap", mat.texture_slots)
        self.assertEqual(mat.floats, {"_Cutoff": 0.25, "_Mode": 1.0})
        self.assertEqual(mat.colors["_Color"], (0.5, 0.75, 1.0, 1.0))

    def test_new_layout(self):
        data = w.material(
            "NewLayout", shader=(SHADER, 4800000, 3), keywords=["_EMISSION"], invalid_keywords=["_OLD"],
            textures=[("_BaseMap", TEX, (1.0, 1.0), (0.0, 0.0))], ints=[("_Cull", 2)], floats=[("_Surface", 1.0)],
            colors=[("_BaseColor", (1.0, 0.5, 0.25, 0.5))], legacy=False, version=22, big_endian=True,
        )
        mat = parse_material(data)
        self.assertEqual(mat.shader, UnityRef(4800000, SHADER, 3))
        self.assertEqual(mat.keywords, ["_EMISSION"])
        self.assertEqual(mat.invalid_keywords, ["_OLD"])
        self.assertEqual(mat.ints, {"_Cull": 2})
        self.assertEqual(mat.textures["_BaseMap"].guid, TEX)
        self.assertEqual(mat.colors["_BaseColor"], (1.0, 0.5, 0.25, 0.5))


class BinaryPrefabTest(unittest.TestCase):
    def test_renderers(self):
        for kwargs in ({}, {"version": 22, "big_endian": True}):
            with self.subTest(**kwargs):
                raw = parse_asset(w.prefab_with_renderers([("Body", MODEL, [MAT_A, None])], **kwargs))
                ((go, info),) = raw.renderers.values()
                self.assertEqual(raw.names[go], "Body")
                self.assertEqual(info.materials, [MAT_A, None])
                self.assertEqual(info.renderer_class, 23)
                self.assertEqual(info.mesh_guid, MODEL)

    def test_local_reference(self):
        docs = parse_serialized_file(w.prefab_with_renderers([("Body", MODEL, [MAT_A])]))
        renderer = next(d for d in docs if d.type_name == "MeshRenderer")
        self.assertEqual(renderer.body["m_GameObject"], UnityRef(file_id=100))
        self.assertEqual(renderer.body["m_Materials"], [UnityRef(2100000, MAT_A, 2)])


class RobustnessTest(unittest.TestCase):
    def test_unsupported_version(self):
        data = bytearray(legacy_material())
        for version in (13, 23):
            data[8:12] = struct.pack(">I", version)
            with self.assertRaisesRegex(UnityBinaryError, "unsupported"):
                parse_serialized_file(bytes(data))

    def test_without_type_tree(self):
        root = w.cls("Material", "Base", w.string("m_Name"))
        data = w.build_serialized_file([(1, 21, root, {"m_Name": "x"})], type_tree=False)
        with self.assertRaisesRegex(UnityBinaryError, "type tree"):
            parse_serialized_file(data)

    def test_unreadable_object_is_skipped(self):
        good = w.cls("Material", "Base", w.string("m_Name"))
        managed = w.cls("MonoBehaviour", "Base", w.string("m_Name"),
                        w.cls("ManagedReferencesRegistry", "references", w.prim("int", "version")))
        data = w.build_serialized_file([
            (1, 21, good, {"m_Name": "kept"}),
            (2, 114, managed, {"m_Name": "skipped", "references": {"version": 2}}),
        ])
        docs = parse_serialized_file(data)
        self.assertEqual([d.body["m_Name"] for d in docs], ["kept"])
        only_bad = w.build_serialized_file([(2, 114, managed, {"m_Name": "x", "references": {"version": 2}})])
        with self.assertRaises(UnityBinaryError):
            parse_serialized_file(only_bad)

    def test_huge_array_count(self):
        root = w.cls("Holder", "Base", w.vector("m_Values", w.prim("float", "data")))
        data = w.build_serialized_file([(1, 50, root, {"m_Values": [1.5]})])
        marker = struct.pack("<if", 1, 1.5)
        self.assertEqual(data.count(marker), 1)
        corrupt = data.replace(marker, struct.pack("<if", 0x7FFFFFFF, 1.5))
        with self.assertRaisesRegex(UnityBinaryError, "count"):
            parse_serialized_file(corrupt)

    def test_deep_type_tree(self):
        node, value = w.prim("int", "leaf"), 7
        for _ in range(254):  # ルートを含めて TypeTree の階層（1 バイト）の上限 255 まで
            node, value = w.cls("Nest", "child", node), {node.name: value}
        root = w.cls("Deep", "Base", node)
        (doc,) = parse_serialized_file(w.build_serialized_file([(1, 50, root, {"child": value})]))
        self.assertEqual(doc.type_name, "Deep")

    def test_truncated_and_corrupted_raise_value_error(self):
        """どこを切り詰めても、どのバイトを壊しても、ValueError 以外の例外を出さない。"""
        data = w.material("Fuzz", textures=LEGACY_TEXTURES[1:], floats=LEGACY_FLOATS[:1], colors=LEGACY_COLORS)
        for size in range(len(data)):
            try:
                parse_serialized_file(data[:size])
            except ValueError:
                pass
        for i in range(len(data)):
            for mask in (0xFF, 0x80, 0x01):
                corrupt = bytearray(data)
                corrupt[i] ^= mask
                for parse in (parse_serialized_file, parse_material):
                    try:
                        parse(bytes(corrupt))
                    except ValueError:
                        pass


if __name__ == "__main__":
    unittest.main()
