import math
import tarfile
import unittest

from tests import _paths  # noqa: F401  (sys.path 設定)
from unitypackage_loader.core.unity_yaml import (
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
