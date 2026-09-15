import math
import tarfile
import unittest

from tests import _paths  # noqa: F401  (sys.path 設定)
from unitypackage_loader.core.unity_yaml import (
    MAX_DEPTH,
    UnityRef,
    UnityYamlError,
    parse_documents,
    parse_text,
)

# 手書きの合成フィクスチャ。実在アセットの値は使わない。
MATERIAL_YAML = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!21 &2100000
Material:
  serializedVersion: 8
  m_ObjectHideFlags: 0
  m_Name: ExampleMat
  m_Shader: {fileID: 4800000, guid: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, type: 3}
  m_ValidKeywords: []
  m_InvalidKeywords:
  - _ALPHATEST_ON
  m_CustomRenderQueue: -1
  stringTagMap: {}
  m_LockedProperties: 
  m_SavedProperties:
    serializedVersion: 3
    m_TexEnvs:
    - _MainTex:
        m_Texture: {fileID: 2800000, guid: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, type: 3}
        m_Scale: {x: 2, y: 1}
        m_Offset: {x: 0, y: 0.5}
    - _BumpMap:
        m_Texture: {fileID: 0}
        m_Scale: {x: 1, y: 1}
        m_Offset: {x: 0, y: 0}
    m_Floats:
    - _Cutoff: 0.5
    - _Mode: 1
    - _Tiny: 1e-05
    - _Neg: -0.25
    m_Colors:
    - _Color: {r: 1, g: 0.5, b: 0.25, a: 1}
    - _EmissionColor: {r: 0, g: 0, b: 0, a: 1}
"""

META_YAML = """\
fileFormatVersion: 2
guid: cccccccccccccccccccccccccccccccc
ModelImporter:
  serializedVersion: 22200
  internalIDToNameTable: []
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
  materials:
    materialImportMode: 2
    materialName: 0
  meshes:
    globalScale: 1
    useFileScale: 1
  userData: 
  assetBundleName: 
"""

MULTILINE_FLOW_YAML = """\
--- !u!137 &123 stripped
SkinnedMeshRenderer:
  m_CorrespondingSourceObject: {fileID: 1630794972795428178, guid: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee,
    type: 3}
  m_PrefabInstance: {fileID: 6303372428556562487}
--- !u!114 &-3932805451751445030
MonoBehaviour:
  m_Name: 
  m_EditorClassIdentifier: 
  quoted: 'it''s here'
  dq: "tab\\tsep"
  list_of_refs:
  - {fileID: 1}
  - {fileID: 2, guid: ffffffffffffffffffffffffffffffff, type: 2}
  nested_seq:
  - - 1
    - 2
  - - 3
"""


class ParseDocumentsTests(unittest.TestCase):
    def test_material_header_and_body(self):
        docs = parse_documents(MATERIAL_YAML)
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual((doc.class_id, doc.file_id, doc.stripped), (21, 2100000, False))
        self.assertEqual(doc.type_name, "Material")
        body = doc.body
        self.assertEqual(body["m_Name"], "ExampleMat")
        self.assertEqual(body["m_Shader"], UnityRef(4800000, "a" * 32, 3))
        self.assertEqual(body["m_ValidKeywords"], [])
        self.assertEqual(body["m_InvalidKeywords"], ["_ALPHATEST_ON"])
        self.assertEqual(body["m_CustomRenderQueue"], -1)
        self.assertEqual(body["stringTagMap"], {})
        self.assertEqual(body["m_LockedProperties"], "")

    def test_material_saved_properties(self):
        props = parse_documents(MATERIAL_YAML)[0].body["m_SavedProperties"]
        tex = props["m_TexEnvs"]
        self.assertEqual(len(tex), 2)
        main = tex[0]["_MainTex"]
        self.assertEqual(main["m_Texture"], UnityRef(2800000, "b" * 32, 3))
        self.assertEqual(main["m_Scale"], {"x": 2, "y": 1})
        self.assertEqual(main["m_Offset"], {"x": 0, "y": 0.5})
        self.assertTrue(tex[1]["_BumpMap"]["m_Texture"].is_null)
        floats = {k: v for item in props["m_Floats"] for k, v in item.items()}
        self.assertEqual(floats, {"_Cutoff": 0.5, "_Mode": 1, "_Tiny": 1e-05, "_Neg": -0.25})
        colors = {k: v for item in props["m_Colors"] for k, v in item.items()}
        self.assertEqual(colors["_Color"], {"r": 1, "g": 0.5, "b": 0.25, "a": 1})

    def test_multiline_flow_and_stripped(self):
        docs = parse_documents(MULTILINE_FLOW_YAML)
        self.assertEqual(len(docs), 2)
        smr = docs[0]
        self.assertTrue(smr.stripped)
        self.assertEqual(smr.class_id, 137)
        ref = smr.body["m_CorrespondingSourceObject"]
        self.assertEqual(ref, UnityRef(1630794972795428178, "e" * 32, 3))
        self.assertEqual(smr.body["m_PrefabInstance"], UnityRef(6303372428556562487))
        mono = docs[1]
        self.assertEqual(mono.file_id, -3932805451751445030)
        self.assertEqual(mono.body["m_Name"], "")
        self.assertEqual(mono.body["quoted"], "it's here")
        self.assertEqual(mono.body["dq"], "tab\tsep")
        self.assertEqual(mono.body["list_of_refs"][1].guid, "f" * 32)
        self.assertEqual(mono.body["nested_seq"], [[1, 2], [3]])

    def test_string_keys_stay_strings(self):
        docs = parse_documents("--- !u!1 &1\nGameObject:\n  m_Name: 12345\n  m_Layer: 12345\n")
        self.assertEqual(docs[0].body["m_Name"], "12345")
        self.assertEqual(docs[0].body["m_Layer"], 12345)

    def test_special_floats(self):
        body = parse_text("a: .inf\nb: -.inf\nc: .nan\n")
        self.assertEqual(body["a"], math.inf)
        self.assertEqual(body["b"], -math.inf)
        self.assertTrue(math.isnan(body["c"]))

    def test_non_mapping_document_body_is_empty(self):
        # 本文がリストの壊れたドキュメントは、例外にせず空として扱う（#69）
        docs = parse_documents("%YAML 1.1\n--- !u!1 &1\n- a\n- b\n--- !u!4 &2\nTransform:\n  m_Father: {fileID: 0}\n")
        self.assertEqual((docs[0].type_name, docs[0].body), (None, {}))
        self.assertEqual(docs[1].body, {"m_Father": UnityRef(0, None, None)})

    def test_value_glued_to_flow_mapping_is_yaml_error(self):
        # 「y:{」のように空白の無い値の後で値が空になると、IndexError になっていた（#69 のファズテストで発見）
        with self.assertRaises(UnityYamlError):
            parse_text("a: {x: 1, y:{fileID: 1, guid: , type: 3} 1}\n")

    def test_bad_indentation_raises(self):
        with self.assertRaises(UnityYamlError):
            parse_text("a:\n  b: 1\n c: 2\n")

    def test_crlf_and_bom(self):
        body = parse_text("\ufeffa: 1\r\nb:\r\n  c: x\r\n")
        self.assertEqual(body, {"a": 1, "b": {"c": "x"}})


class ParseTextTests(unittest.TestCase):
    def test_meta_external_objects(self):
        meta = parse_text(META_YAML)
        self.assertEqual(meta["fileFormatVersion"], 2)
        self.assertEqual(meta["guid"], "c" * 32)
        importer = meta["ModelImporter"]
        ext = importer["externalObjects"]
        self.assertEqual(len(ext), 2)
        self.assertEqual(ext[0]["first"]["name"], "MatA")
        self.assertEqual(ext[0]["first"]["type"], "UnityEngine:Material")
        self.assertEqual(ext[0]["second"].guid, "d" * 32)
        self.assertEqual(ext[1]["first"]["name"], "MatA.001")
        self.assertEqual(importer["materials"]["materialImportMode"], 2)
        self.assertEqual(importer["meshes"]["useFileScale"], 1)
        self.assertEqual(importer["userData"], "")
        self.assertEqual(importer["internalIDToNameTable"], [])


class FoldedScalarTests(unittest.TestCase):
    """Unity が長い値を次の行に折り返したもの（YAML の複数行スカラー）を 1 つの値につなぐ。"""

    def test_plain_value_in_mapping(self):
        body = parse_documents(
            "--- !u!21 &2100000\nMaterial:\n"
            "  m_Name: FoldedMat\n"
            "  m_ShaderKeywords: _KEYWORD_A _KEYWORD_B\n"
            "    _KEYWORD_C\n"
            "  m_CustomRenderQueue: 3000\n"
        )[0].body
        self.assertEqual(body["m_ShaderKeywords"], "_KEYWORD_A _KEYWORD_B _KEYWORD_C")
        self.assertEqual(body["m_CustomRenderQueue"], 3000)

    def test_plain_value_in_sequence_items(self):
        body = parse_text(
            "calls:\n"
            "- m_TypeName: Example.Type, Example.Assembly,\n"
            "    Version=1.0.0.0\n"
            "  m_Mode: 1\n"
            "- first part\n"
            "  second part\n"
            "after: 2\n"
        )
        self.assertEqual(
            body,
            {
                "calls": [
                    {"m_TypeName": "Example.Type, Example.Assembly, Version=1.0.0.0", "m_Mode": 1},
                    "first part second part",
                ],
                "after": 2,
            },
        )

    def test_quoted_values(self):
        body = parse_text(
            "sq: 'first ''line''\n  second'\n"
            'dq: "joined \\\n  here"\n'
            'dq2: "a\\\\\n  b"\n'
            "closed: 'one'\n"
            "next: 1\n"
        )
        self.assertEqual(body["sq"], "first 'line' second")
        self.assertEqual(body["dq"], "joined here")
        self.assertEqual(body["dq2"], "a\\ b")
        self.assertEqual(body["closed"], "one")
        self.assertEqual(body["next"], 1)

    def test_mapping_like_continuation_still_raises(self):
        with self.assertRaises(UnityYamlError):
            parse_text("a: 1\n  b: 2\n")


class DepthLimitTests(unittest.TestCase):
    """ネストが深すぎる入力は RecursionError ではなく UnityYamlError（ValueError）で止まる。"""

    DEEP = 3000

    def assert_too_deep(self, text: str) -> None:
        with self.assertRaises(UnityYamlError) as cm:
            parse_text(text)
        self.assertIsInstance(cm.exception, ValueError)
        self.assertIn("nested", str(cm.exception))

    def test_deep_flow_mapping(self):
        self.assert_too_deep("a: " + "{a: " * self.DEEP + "1" + "}" * self.DEEP)

    def test_deep_flow_sequence(self):
        self.assert_too_deep("a: " + "[" * self.DEEP + "1" + "]" * self.DEEP)

    def test_deep_block_mapping(self):
        lines = [" " * i + f"k{i}:" for i in range(self.DEEP)] + [" " * self.DEEP + "leaf: 1"]
        self.assert_too_deep("\n".join(lines))

    def test_deep_block_sequence(self):
        lines = [" " * i + "-" for i in range(self.DEEP)] + [" " * self.DEEP + "- 1"]
        self.assert_too_deep("\n".join(lines))

    def test_deep_inline_dashes(self):
        # ``- - - - x`` はダッシュごとにブロックを読み直すので、1 行でも深くなる
        self.assert_too_deep("- " * self.DEEP + "x")

    def test_deep_documents_raise_yaml_error(self):
        text = "%YAML 1.1\n--- !u!21 &1\nMaterial:\n  m_X: " + "[" * self.DEEP + "]" * self.DEEP
        with self.assertRaises(UnityYamlError):
            parse_documents(text)

    def test_within_limit_parses(self):
        depth = MAX_DEPTH // 2
        value = parse_text("a: " + "[" * depth + "1" + "]" * depth)["a"]
        for _ in range(depth):
            value = value[0]
        self.assertEqual(value, 1)
        lines = [" " * i + f"k{i}:" for i in range(depth)] + [" " * depth + "leaf: 1"]
        value = parse_text("\n".join(lines))
        for i in range(depth):
            value = value[f"k{i}"]
        self.assertEqual(value, {"leaf": 1})


class LocalSampleTests(unittest.TestCase):
    """``_local/`` にサンプルがあるときだけ、実データを全件パースして例外が出ないことを確認する。"""

    def test_parse_every_mat_and_meta_in_local_packages(self):
        packages = _paths.local_packages()
        if not packages:
            self.skipTest("no local sample packages")
        parsed = 0
        for pkg in packages:
            with tarfile.open(pkg, "r:gz") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    name = member.name.rsplit("/", 1)[-1]
                    if name == "asset.meta":
                        parse_text(tar.extractfile(member).read().decode("utf-8"))
                        parsed += 1
                    elif name == "asset" and member.size < 2_000_000:
                        head = tar.extractfile(member)
                        data = head.read()
                        if data.startswith(b"%YAML"):
                            docs = parse_documents(data.decode("utf-8"))
                            self.assertTrue(docs)
                            parsed += 1
        self.assertGreater(parsed, 0)


if __name__ == "__main__":
    unittest.main()
