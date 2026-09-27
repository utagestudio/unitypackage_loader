"""パッケージに入っているシェーダーの定義から、宣言されたプロパティを読む（#118）。"""

import json
import unittest

from tests import _paths  # noqa: F401
from tests.test_material import TEX_A, TEX_N, mat_yaml
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.profiles import normalize_material
from unitypackage_loader.core.shader_source import (
    GRAPH_TARGET_PROPERTIES,
    ShaderSourceError,
    declared_properties,
    restrict_to_declared,
)

# 自作の .shader。コメント、属性、文字列の中の括弧、既定値の {} を含む
SHADERLAB = """﻿Shader "Custom/Tint" {
    // _Commented ("Not a property", Float) = 0
    Properties {
        [HDR] _Color ("Color {tint}", Color) = (1, 1, 1, 1)
        [NoScaleOffset] [Toggle(_USE_MAP)] _TintMap ("Map (RGB)", 2D) = "white" {}
        /* _Hidden ("Hidden", Float) = 1 */
        _Strength("Strength", Range(0, 1)) = 0.5
    }
    SubShader {
        Tags { "Queue" = "Transparent" }
        Blend DstColor Zero
        Pass { CGPROGRAM
            fixed4 _Color;
            fixed4 frag () : SV_Target { return _Color; }
        ENDCG }
    }
}
"""


def graph_v1(*names):
    """古い形式の Shader Graph（1 つの JSON。各プロパティは JSONnodeData の文字列）。"""
    props = [
        {"typeInfo": {"fullName": "UnityEditor.ShaderGraph.TextureShaderProperty"},
         "JSONnodeData": json.dumps({"m_Name": name.strip("_"), "m_DefaultReferenceName": f"Texture2D_{i:08X}",
                                     "m_OverrideReferenceName": name})}
        for i, name in enumerate(names)
    ]
    return json.dumps({"m_SerializedProperties": props, "m_SerializedKeywords": []}, indent=4)


def graph_v2(*names):
    """新しい形式の Shader Graph（JSON のオブジェクトを空行で並べたもの）。上書きが空なら既定の名前を使う。"""
    objects = [{"m_SGVersion": 3, "m_Type": "UnityEditor.ShaderGraph.GraphData", "m_Properties": [{"m_Id": str(i)} for i in range(len(names))]}]
    for i, name in enumerate(names):
        default, override = (name, "") if i % 2 else ("Color_1234", name)
        objects.append({"m_Type": "UnityEditor.ShaderGraph.Internal.ColorShaderProperty", "m_ObjectId": str(i),
                        "m_Name": name, "m_DefaultReferenceName": default, "m_OverrideReferenceName": override})
    return "\n\n".join(json.dumps(o, indent=4) for o in objects)


GRAPH = "{fileID: -6465566751694194690, guid: " + "e" * 32 + ", type: 3}"  # 表に無い Shader Graph
CUSTOM = "{fileID: 4800000, guid: " + "f" * 32 + ", type: 3}"  # 表に無い自作 .shader
STANDARD = "{fileID: 46, guid: 0000000000000000f000000000000000, type: 0}"

# HDRP/Lit から、_MainTex / _Color を使う Shader Graph に切り替えたマテリアル。HDRP/Lit と Standard の値が残っている
GRAPH_MAT = mat_yaml(
    "Frames", GRAPH,
    tex=[("_MainTex", TEX_A, (2, 2), (0, 0)), ("_BaseColorMap", TEX_N, (1, 1), (0, 0)), ("_EmissionMap", TEX_N, (1, 1), (0, 0))],
    floats=[("_Metallic", 1), ("_Smoothness", 1), ("_Glossiness", 1), ("_SurfaceType", 0), ("_Mode", 3), ("_CullMode", 0)],
    colors=[("_Color", (0.5, 0.25, 0.125, 1)), ("_BaseColor", (0, 0, 0, 1)), ("_EmissionColor", (1, 1, 1, 1))],
)


class DeclaredPropertiesTests(unittest.TestCase):
    def test_shaderlab(self):
        self.assertEqual(declared_properties(SHADERLAB.encode(), ".shader"), {"_Color", "_TintMap", "_Strength"})

    def test_shaderlab_without_properties(self):
        self.assertEqual(declared_properties('Shader "X" { SubShader { Pass { } } }', ".shader"), frozenset())

    def test_shaderlab_unterminated(self):
        with self.assertRaises(ShaderSourceError):
            declared_properties('Shader "X" { Properties { _A ("A", Float) = 0 ', ".shader")

    def test_graph_formats_add_target_render_state(self):
        for label, text in (("v1", graph_v1("_MainTex", "_Color")), ("v2", graph_v2("_MainTex", "_Color"))):
            with self.subTest(label):
                declared = declared_properties(text, ".ShaderGraph")
                self.assertTrue({"_MainTex", "_Color"} <= declared)
                self.assertTrue(GRAPH_TARGET_PROPERTIES <= declared)
                self.assertNotIn("Color_1234", declared)  # 上書きがあれば既定の名前は使われない
                self.assertNotIn("_EmissionColor", declared)

    def test_graph_without_properties_is_empty(self):
        # 宣言が 1 つも取れなければ、ターゲットの描画設定も足さない（呼ぶ側は絞り込まない）
        self.assertEqual(declared_properties(graph_v2(), ".shadergraph"), frozenset())

    def test_broken_graph(self):
        for text in ("{", "{} {", '{"m_SerializedProperties": [{"JSONnodeData": "{"}]}', "[" * 100000):
            with self.subTest(text=text[:20]), self.assertRaises(ShaderSourceError):
                declared_properties(text, ".shadergraph")

    def test_unknown_extension(self):
        with self.assertRaises(ShaderSourceError):
            declared_properties("", ".hlsl")


class RestrictTests(unittest.TestCase):
    def test_keeps_only_declared(self):
        mat = parse_material(GRAPH_MAT)
        restricted, dropped = restrict_to_declared(mat, frozenset({"_MainTex", "_Color", "_CullMode"}))
        self.assertEqual(set(restricted.textures), {"_MainTex"})
        self.assertEqual(set(restricted.colors), {"_Color"})
        self.assertEqual(set(restricted.floats), {"_CullMode"})
        self.assertIn("_BaseColorMap", dropped)
        self.assertIn("_Mode", dropped)
        self.assertEqual(set(mat.colors), {"_Color", "_BaseColor", "_EmissionColor"})  # 元は変えない

    def test_nothing_to_drop_returns_same(self):
        mat = parse_material(GRAPH_MAT)
        everything = frozenset({*mat.textures, *mat.floats, *mat.colors, *mat.texture_slots})
        self.assertIs(restrict_to_declared(mat, everything)[0], mat)


class NormalizeWithDeclaredTests(unittest.TestCase):
    def test_graph_ignores_leftovers(self):
        declared = declared_properties(graph_v1("_MainTex", "_Color"), ".shadergraph")
        n = normalize_material(parse_material(GRAPH_MAT), declared=declared)
        self.assertEqual(n.base_color_tex.guid, TEX_A)
        self.assertEqual(n.uv_scale, (2.0, 2.0))
        self.assertEqual(n.base_color, (0.5, 0.25, 0.125, 1.0))
        self.assertEqual(n.metallic, 0.0)  # 残った _Metallic: 1 で金属にしない
        self.assertAlmostEqual(n.roughness, 0.5)  # 残った _Smoothness / _Glossiness: 1 で鏡面にしない
        self.assertFalse(n.has_emission)  # 残った白い _EmissionColor で光らせない
        self.assertEqual(n.alpha_mode, "opaque")  # 残った _Mode: 3 ではなく、ターゲットの _SurfaceType: 0
        self.assertFalse(n.cull_backface)  # ターゲットの _CullMode: 0（両面）
        self.assertIn("_BaseColor", n.extras["undeclared_properties"])

    def test_graph_without_color_is_not_tinted_by_leftover_black(self):
        # 色を宣言しないグラフでは、残った黒い _BaseColor / _Color を掛けない（#118: 黒い面）
        declared = declared_properties(graph_v2("_MainTex"), ".shadergraph")
        text = GRAPH_MAT.replace("r: 0.5, g: 0.25, b: 0.125", "r: 0, g: 0, b: 0")
        n = normalize_material(parse_material(text), declared=declared)
        self.assertEqual(n.base_color, (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(n.base_color_tex.guid, TEX_A)

    def test_without_declarations_reads_as_before(self):
        n = normalize_material(parse_material(GRAPH_MAT))
        self.assertEqual(n.metallic, 1.0)
        self.assertNotIn("undeclared_properties", n.extras)

    def test_custom_shader_ignores_leftover_surface_type(self):
        declared = declared_properties(SHADERLAB, ".shader")
        text = mat_yaml("Glass", CUSTOM, floats=[("_SurfaceType", 1), ("_Mode", 0)], colors=[("_Color", (0.9, 0.95, 0.9, 1))])
        n = normalize_material(parse_material(text), declared=declared)
        self.assertEqual(n.alpha_mode, "opaque")
        self.assertEqual(n.base_color, (0.9, 0.95, 0.9, 1.0))

    def test_table_shader_is_not_restricted(self):
        # シェーダー表で分かるシェーダーは、定義があっても絞り込まない（各プロファイルの読み方に任せる）
        text = mat_yaml("Std", STANDARD, tex=[("_MainTex", TEX_A, (1, 1), (0, 0))], floats=[("_Metallic", 1), ("_Glossiness", 0.5)],
                        colors=[("_Color", (1, 0, 0, 1))])
        n = normalize_material(parse_material(text), declared=frozenset({"_Color"}))
        self.assertEqual(n.family, "standard")
        self.assertEqual(n.base_color_tex.guid, TEX_A)
        self.assertEqual(n.metallic, 1.0)


if __name__ == "__main__":
    unittest.main()
