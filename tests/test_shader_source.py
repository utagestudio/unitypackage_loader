"""パッケージに入っているシェーダーの定義から、宣言されたプロパティを読む（#118）。"""

import json
import unittest

from tests import _paths  # noqa: F401
from tests.test_material import TEX_A, TEX_N, mat_yaml
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.profiles import normalize_material
from unitypackage_loader.core.shader_source import (
    GRAPH_TARGET_PROPERTIES,
    ShaderDefaults,
    ShaderSourceError,
    declared_properties,
    restrict_to_declared,
    shader_defaults,
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

    def test_custom_shader_with_own_texture_names(self):
        # Amplify Shader Editor の自作シェーダー（_Base_Color / _Normal）。残りの _MainTex / _BumpMap を外したあとも、
        # 独自の名前からテクスチャを取る（#118 のあと、Japanese Street の AE_Flower が白くなった）
        shader = """Shader "Custom/Leaves" { Properties {
            _Cutoff( "Mask Clip Value", Float ) = 0.6
            _Color("Color", Color) = (1,1,1,0)
            _Scale_Normal("Scale_Normal", Range( 0 , 3)) = 0
            _Base_Color("Base_Color", 2D) = "white" {}
            _Mask("Mask", 2D) = "white" {}
            [Normal]_Normal("Normal", 2D) = "bump" {}
        } SubShader { } }"""
        text = mat_yaml(
            "Flower", CUSTOM,
            tex=[("_MainTex", TEX_N, (1, 1), (0, 0)), ("_BumpMap", TEX_N, (1, 1), (0, 0)), ("_Mask", TEX_N, (1, 1), (0, 0)),
                 ("_Base_Color", TEX_A, (2, 3), (0, 0)), ("_Normal", TEX_A, (1, 1), (0, 0))],
            floats=[("_Cutoff", 0.3), ("_Scale_Normal", 0.5), ("_BumpScale", 1), ("_Mode", 1)],
            colors=[("_Color", (0.75, 0.75, 0.75, 1))],
        )
        n = normalize_material(parse_material(text), declared=declared_properties(shader, ".shader"))
        self.assertEqual(n.family, "unknown")
        self.assertEqual(n.base_color_tex.guid, TEX_A)
        self.assertEqual(n.uv_scale, (2.0, 3.0))
        self.assertEqual(n.base_color, (0.75, 0.75, 0.75, 1.0))
        self.assertEqual(n.normal_tex.guid, TEX_A)
        self.assertEqual(n.normal_strength, 0.5)  # 残りの _BumpScale ではなく _Scale_Normal

    def test_generic_prefers_common_names_over_aliases(self):
        text = mat_yaml("Both", CUSTOM, tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_Albedo", TEX_N, (1, 1), (0, 0))])
        self.assertEqual(normalize_material(parse_material(text)).base_color_tex.guid, TEX_A)
        text = mat_yaml("AlbedoOnly", CUSTOM, tex=[("_Albedo", TEX_N, (1, 1), (0, 0)), ("_ColorMask", TEX_A, (1, 1), (0, 0))])
        self.assertEqual(normalize_material(parse_material(text)).base_color_tex.guid, TEX_N)

    def test_table_shader_is_not_restricted(self):
        # シェーダー表で分かるシェーダーは、定義があっても絞り込まない（各プロファイルの読み方に任せる）
        text = mat_yaml("Std", STANDARD, tex=[("_MainTex", TEX_A, (1, 1), (0, 0))], floats=[("_Metallic", 1), ("_Glossiness", 0.5)],
                        colors=[("_Color", (1, 0, 0, 1))])
        n = normalize_material(parse_material(text), declared=frozenset({"_Color"}))
        self.assertEqual(n.family, "standard")
        self.assertEqual(n.base_color_tex.guid, TEX_A)
        self.assertEqual(n.metallic, 1.0)



# Amplify Shader Editor で作った葉のシェーダー（Tokyo Street / Japanese Street の AE/Leaves と同じ形）。
# 切り抜くかどうかはシェーダーのタグと clip() だけで決まり、マテリアルの m_CustomRenderQueue は -1
LEAVES = """Shader "AE/Leaves"
{
	Properties
	{
		_Cutoff( "Mask Clip Value", Float ) = 0.73
		_Base_Color("Base_Color", 2D) = "white" {}
		[HideInInspector] __dirty( "", Int ) = 1
	}
	SubShader
	{
		// Tags{ "Queue" = "Transparent" }
		Tags{ "RenderType" = "TransparentCutout"  "Queue" = "AlphaTest+0" }
		Cull Off
		CGPROGRAM
		#pragma surface surf Standard keepalpha addshadow fullforwardshadows
		void surf( Input i , inout SurfaceOutputStandard o ) { clip( tex2D( _Base_Color, i.uv ).a - _Cutoff ); }
		ENDCG
	}
	Fallback "Diffuse"
}
"""


class ShaderDefaultsTests(unittest.TestCase):
    """.shader の SubShader の Queue と固定の Cull（#202）。"""

    def test_alpha_test_queue_and_cull_off(self):
        defaults = shader_defaults(LEAVES, ".shader")
        self.assertEqual(
            defaults, ShaderDefaults(queue=2450, render_type="TransparentCutout", cull_off=True, hidden=frozenset({"__dirty"}))
        )
        self.assertEqual(defaults.alpha_mode, "cutout")

    def test_render_type_wins_over_queue(self):
        # AE/AE_Decal は AlphaTest のキューで decal:blend（半透明に重ねる）
        text = 'Shader "X" { SubShader { Tags { "Queue" = "AlphaTest" "IgnoreProjector" = "True" "RenderType" = "Transparent" } } }'
        self.assertEqual(shader_defaults(text, ".shader").alpha_mode, "blend")
        self.assertEqual(ShaderDefaults(queue=2450).alpha_mode, "cutout")
        self.assertEqual(ShaderDefaults(queue=3000).alpha_mode, "blend")
        self.assertIsNone(ShaderDefaults(queue=2450, render_type="Opaque").alpha_mode)
        self.assertIsNone(ShaderDefaults(queue=2000).alpha_mode)

    def test_queue_offset_and_names(self):
        for tag, queue in (("Transparent-1", 2999), ("Geometry", 2000), ("AlphaTest+51", 2501), ("Overlay", 4000)):
            text = 'Shader "X" { SubShader { Tags { "RenderType"="Opaque" "Queue" = "%s" } Pass { } } }' % tag
            self.assertEqual(shader_defaults(text, ".shader").queue, queue, tag)
            self.assertEqual(shader_defaults(text, ".shader").render_type, "Opaque")

    def test_pass_tags_and_property_cull_are_not_defaults(self):
        # Pass の Tags の Queue は Unity が使わない。Cull [_Cull] はマテリアルのプロパティで決まる
        text = 'Shader "X" { SubShader { Pass { Tags { "Queue"="Transparent" "LightMode"="ForwardBase" } Cull [_Cull] } } }'
        self.assertEqual(shader_defaults(text, ".shader"), ShaderDefaults())

    def test_without_subshader_or_graph(self):
        self.assertEqual(shader_defaults('Shader "X" { Properties { } }', ".shader"), ShaderDefaults())
        self.assertEqual(shader_defaults(graph_v2("_Color"), ".shadergraph"), ShaderDefaults())

    def test_cutout_from_shader_queue(self):
        # 前のシェーダー（Standard の Opaque）の _Mode と _SrcBlend が残っていても、宣言されていないので読まず、
        # シェーダーの Queue（AlphaTest）で切り抜く。_Cutoff はマテリアルの値。両面
        text = mat_yaml(
            "Flowers", CUSTOM, tex=[("_Base_Color", TEX_A, (1, 1), (0, 0)), ("_MainTex", TEX_N, (1, 1), (0, 0))],
            floats=[("_Cutoff", 0.5), ("_Mode", 0), ("_SrcBlend", 1), ("_DstBlend", 0)],
        )
        mat = parse_material(text)
        self.assertEqual(mat.render_queue, -1)  # シェーダーの既定のまま
        n = normalize_material(mat, declared=declared_properties(LEAVES, ".shader"), defaults=shader_defaults(LEAVES, ".shader"))
        self.assertEqual((n.alpha_mode, n.alpha_cutoff, n.alpha_from_texture), ("cutout", 0.5, True))
        self.assertFalse(n.cull_backface)
        self.assertEqual(n.base_color_tex.guid, TEX_A)
        # 定義が無ければこれまでどおり（不透明・片面）
        n = normalize_material(mat)
        self.assertEqual((n.alpha_mode, n.cull_backface), ("opaque", True))

    def test_material_queue_and_cull_win(self):
        # マテリアルが Render Queue を上書きしていればそれを使い、カリングのプロパティがあればそれに従う
        text = mat_yaml("Solid", CUSTOM, tex=[("_Base_Color", TEX_A, (1, 1), (0, 0))], floats=[("_Cull", 2)], queue=2000)
        shader = LEAVES.replace("_Cutoff(", '_Cull("Cull", Float) = 2\n\t\t_Cutoff(')
        n = normalize_material(parse_material(text), declared=declared_properties(shader, ".shader"), defaults=shader_defaults(shader, ".shader"))
        self.assertEqual((n.alpha_mode, n.cull_backface), ("opaque", True))

    def test_table_shader_ignores_defaults(self):
        text = mat_yaml("Std", STANDARD, tex=[("_MainTex", TEX_A, (1, 1), (0, 0))], floats=[("_Mode", 0)])
        n = normalize_material(parse_material(text), defaults=ShaderDefaults(queue=2450, cull_off=True))
        self.assertEqual((n.alpha_mode, n.cull_backface), ("opaque", True))


# Amplify Shader Editor の URP テンプレートで作ったシェーダー（The Basilica Cathedral の Tripolygon(URP)/Leaf、
# Corals の ASE_StandartCutout2Sided と同じ形）。タグは Opaque のまま、パスの中で常に切り抜く。
# テンプレートは使わなくても白い _EmissionColor を隠して宣言する
AMPLIFY = """Shader "Tripolygon(URP)/Leaf"
{
	Properties
	{
		[HideInInspector] _EmissionColor("Emission Color", Color) = (1,1,1,1)
		[ASEBegin]_Color("Color", Color) = (1,1,1,1)
		_AlphaCutoff("Alpha Cutoff", Range( 0 , 1)) = 0.5
		[ASEEnd]_MainTex("MainTex", 2D) = "white" {}
	}
	SubShader
	{
		Tags { "RenderPipeline"="UniversalPipeline" "RenderType"="Opaque" "Queue"="Geometry" }
		Cull Back
		Pass
		{
			Tags { "LightMode"="UniversalForward" }
			HLSLPROGRAM
			ALPHA_TEST_LINE
			#pragma vertex vert
			float3 Emission = 0;
			#ifdef _ALPHATEST_ON
				clip(Alpha - AlphaClipThreshold);
			#endif
			ENDHLSL
		}
	}
}
"""


class AmplifyTemplateTests(unittest.TestCase):
    """パスが常に切り抜く・白い発光色を隠して宣言する Amplify Shader Editor のシェーダー（#235 / #241）。"""

    def shader(self, line: str) -> str:
        return AMPLIFY.replace("ALPHA_TEST_LINE", line)

    def test_always_alpha_test(self):
        for line in (
            "#define _ALPHATEST_ON 1",  # Basilica の葉
            "#pragma multi_compile_local_fragment _ALPHATEST_ON",  # Corals（キーワードが 1 つだけなので常に有効）
            "#pragma multi_compile _ALPHATEST_ON",
        ):
            with self.subTest(line=line):
                defaults = shader_defaults(self.shader(line), ".shader")
                self.assertTrue(defaults.alpha_test)
                self.assertEqual(defaults.alpha_mode, "cutout")

    def test_switchable_alpha_test_is_not_always_on(self):
        # 無効の選択肢がある multi_compile と shader_feature は、マテリアルのキーワード次第
        for line in (
            "#pragma multi_compile _ _ALPHATEST_ON",
            "#pragma multi_compile_local __ _ALPHATEST_ON",
            "#pragma shader_feature_local _ALPHATEST_ON",
            "// #define _ALPHATEST_ON 1",
        ):
            with self.subTest(line=line):
                defaults = shader_defaults(self.shader(line), ".shader")
                self.assertFalse(defaults.alpha_test)
                self.assertIsNone(defaults.alpha_mode)

    def test_hidden_properties(self):
        self.assertEqual(shader_defaults(self.shader(""), ".shader").hidden, frozenset({"_EmissionColor"}))

    def test_leaf_material(self):
        # マテリアルには前のシェーダーの _Cutoff は無く、Amplify の _AlphaCutoff がしきい値。白い _EmissionColor は光らせない
        shader = self.shader("#define _ALPHATEST_ON 1")
        text = mat_yaml(
            "BC_Leaf_A", CUSTOM, tex=[("_MainTex", TEX_A, (1, 1), (0, 0))],
            floats=[("_AlphaCutoff", 0.679)], colors=[("_EmissionColor", (1, 1, 1, 1)), ("_Color", (0.58, 0.79, 0.26, 1))],
        )
        n = normalize_material(parse_material(text), declared=declared_properties(shader, ".shader"), defaults=shader_defaults(shader, ".shader"))
        self.assertEqual((n.alpha_mode, n.alpha_from_texture), ("cutout", True))
        self.assertAlmostEqual(n.alpha_cutoff, 0.679)
        self.assertFalse(n.has_emission)
        self.assertEqual(n.base_color_tex.guid, TEX_A)
        # 隠していなければ、今までどおり発光として読む
        visible = shader.replace("[HideInInspector] _EmissionColor", "_EmissionColor")
        n = normalize_material(parse_material(text), declared=declared_properties(visible, ".shader"), defaults=shader_defaults(visible, ".shader"))
        self.assertTrue(n.has_emission)

    def test_emission_multiplier_scales_standard_emission(self):
        # Corals: Emission = _EmissionColor × _EmissionMultiplayer × 画像。強さが 0 なら光らない（#241）
        shader = 'Shader "ASE/C" { Properties { _EmissionColor("E", Color) = (0,0,0,0) _EmissionMultiplayer("M", Float) = 0 } SubShader { } }'
        for value, strength in ((0.0, 0.0), (2.5, 2.5)):
            text = mat_yaml("Corals4", CUSTOM, floats=[("_EmissionMultiplayer", value)], colors=[("_EmissionColor", (1, 1, 1, 1))])
            n = normalize_material(parse_material(text), declared=declared_properties(shader, ".shader"), defaults=shader_defaults(shader, ".shader"))
            self.assertAlmostEqual(n.emission_strength, strength)
        # 独自の名前で読んだ発光の強さは 1 回だけ掛ける
        text = mat_yaml("Synty", CUSTOM, floats=[("_Emission_Intensity", 3.0)], colors=[("_Emission_Color", (1, 0, 0, 1))])
        self.assertAlmostEqual(normalize_material(parse_material(text)).emission_strength, 3.0)


if __name__ == "__main__":
    unittest.main()
