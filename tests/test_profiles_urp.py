"""URP Lit のプロファイル（#116）。値の意味は URP の Lit.shader / LitInput.hlsl / LitGUI.cs に合わせている。"""

import unittest

from tests import _paths  # noqa: F401
from tests.test_material import TEX_A, TEX_E, TEX_N, mat_yaml
from unitypackage_loader.core.material import NormalizedMaterial, parse_material
from unitypackage_loader.core.profiles import normalize_material, select_profile
from unitypackage_loader.core.profiles.urp import UrpLitProfile

URP_LIT = "{fileID: 4800000, guid: 933532a4fcc9baf4fa0491de14d08ed7, type: 3}"
URP_UNLIT = "{fileID: 4800000, guid: 650dd9526735d5b46b79224bc6e94025, type: 3}"
URP_DERIVED = "{fileID: 4800000, guid: " + "7" * 32 + ", type: 3}"  # 表に無い URP Lit 派生のシェーダー
TEX_M = "d" * 32
TEX_S = "e" * 32


def urp_mat(tex=(), floats=(), colors=(), keywords=(), shader=URP_LIT, base=TEX_A):
    """URP Lit の .mat。Standard から変換したときの値（_MainTex / _Color / _Glossiness / _GlossMapScale / _Mode）が残っている。"""
    return mat_yaml(
        "Urp", shader,
        tex=[("_BaseMap", base, (2, 3), (0.5, 0)), ("_MainTex", TEX_E, (1, 1), (0, 0)), *tex],
        floats=[("_WorkflowMode", 1), ("_Surface", 0), ("_Metallic", 0.25), ("_Smoothness", 0.6), ("_Glossiness", 0.1),
                ("_GlossMapScale", 1), ("_Mode", 3), *floats],
        colors=[("_BaseColor", (0.5, 0.5, 1, 1)), ("_Color", (0, 0, 0, 1)), *colors],
        keywords=keywords,
    )


class UrpLitTests(unittest.TestCase):
    def normalize(self, text):
        return normalize_material(parse_material(text))

    def test_reads_only_urp_names(self):
        n = self.normalize(urp_mat())
        self.assertIsInstance(select_profile(parse_material(urp_mat()))[0], UrpLitProfile)
        self.assertEqual(n.family, "urp")
        self.assertEqual(n.base_color_tex.guid, TEX_A)  # 残った _MainTex ではない
        self.assertEqual((n.uv_scale, n.uv_offset), ((2.0, 3.0), (0.5, 0.0)))
        self.assertEqual(n.base_color, (0.5, 0.5, 1.0, 1.0))  # 残った黒い _Color ではない
        self.assertEqual(n.alpha_mode, "opaque")  # 残った _Mode: 3 ではない
        self.assertAlmostEqual(n.metallic, 0.25)
        self.assertAlmostEqual(n.roughness, 0.4)  # 残った _Glossiness ではない
        self.assertAlmostEqual(n.smoothness_scale, 0.6)
        self.assertEqual(n.blend_mode, "alpha")
        self.assertEqual(n.warnings, [])

    def test_old_material_without_base_map_reads_main_tex(self):
        text = urp_mat().replace("    - _BaseMap:", "    - _Unused:").replace("    - _BaseColor:", "    - _UnusedColor:")
        n = self.normalize(text)
        self.assertEqual(n.base_color_tex.guid, TEX_E)
        self.assertEqual(n.base_color, (0.0, 0.0, 0.0, 1.0))

    def test_empty_base_map_does_not_fall_back(self):
        # _BaseMap が空なら URP ではテクスチャ無し。残った _MainTex は使わない
        n = self.normalize(urp_mat(base=None))
        self.assertIsNone(n.base_color_tex)

    def test_metallic_map(self):
        # マップがあれば Metallic はその R（_Metallic は掛けない）、Smoothness は A × _Smoothness
        n = self.normalize(urp_mat(tex=[("_MetallicGlossMap", TEX_M, (1, 1), (0, 0))], keywords=["_METALLICSPECGLOSSMAP"]))
        self.assertEqual(n.metallic_tex.guid, TEX_M)
        self.assertAlmostEqual(n.smoothness_scale, 0.6)
        self.assertFalse(n.smoothness_from_albedo)

    def test_smoothness_from_albedo_only_when_opaque(self):
        n = self.normalize(urp_mat(floats=[("_SmoothnessTextureChannel", 1)]))
        self.assertTrue(n.smoothness_from_albedo)
        text = urp_mat(floats=[("_SmoothnessTextureChannel", 1)]).replace("_Surface: 0", "_Surface: 1")
        self.assertFalse(self.normalize(text).smoothness_from_albedo)

    def test_transparent_and_blend_modes(self):
        base = urp_mat(floats=[("_Blend", 0)]).replace("_Surface: 0", "_Surface: 1")
        n = self.normalize(base)
        self.assertEqual((n.alpha_mode, n.blend_mode), ("blend", "alpha"))
        self.assertTrue(n.alpha_from_texture)
        self.assertEqual(n.extras["urp"]["blend"], "alpha")
        for value, mode, warns in ((1, "premultiply", False), (2, "additive", True), (3, "multiply", True)):
            with self.subTest(mode=mode):
                n = self.normalize(base.replace("_Blend: 0", f"_Blend: {value}"))
                self.assertEqual(n.blend_mode, mode)
                self.assertEqual(any("blend mode" in w for w in n.warnings), warns)
        # 不透明なら _Blend は使わない
        self.assertEqual(self.normalize(urp_mat(floats=[("_Blend", 2)])).blend_mode, "alpha")

    def test_alpha_clip(self):
        n = self.normalize(urp_mat(floats=[("_AlphaClip", 1), ("_Cutoff", 0.3)]))
        self.assertEqual(n.alpha_mode, "cutout")
        self.assertAlmostEqual(n.alpha_cutoff, 0.3)

    def test_cull_and_normal(self):
        n = self.normalize(urp_mat(tex=[("_BumpMap", TEX_N, (1, 1), (0, 0))], floats=[("_Cull", 0), ("_BumpScale", 0.5)]))
        self.assertFalse(n.cull_backface)
        self.assertEqual(n.normal_tex.guid, TEX_N)
        self.assertAlmostEqual(n.normal_strength, 0.5)

    def test_emission_needs_keyword(self):
        emission = dict(tex=[("_EmissionMap", TEX_E, (1, 1), (0, 0))], colors=[("_EmissionColor", (2, 1, 0, 1))])
        self.assertFalse(self.normalize(urp_mat(**emission)).has_emission)  # キーワードの記述があって _EMISSION が無い
        n = self.normalize(urp_mat(**emission, keywords=["_EMISSION"]))
        self.assertEqual(n.emission_tex.guid, TEX_E)
        self.assertEqual(n.emission_color, (2.0, 1.0, 0.0, 1.0))

    def test_specular_workflow_is_read_as_non_metallic(self):
        text = urp_mat(tex=[("_SpecGlossMap", TEX_S, (1, 1), (0, 0)), ("_MetallicGlossMap", TEX_M, (1, 1), (0, 0))],
                       colors=[("_SpecColor", (0.2, 0.2, 0.2, 1))]).replace("_WorkflowMode: 1", "_WorkflowMode: 0")
        n = self.normalize(text)
        self.assertEqual(n.metallic, 0.0)
        self.assertIsNone(n.metallic_tex)
        self.assertTrue(any("specular workflow" in w for w in n.warnings))
        self.assertEqual(n.extras["urp"]["specular"]["tex"], TEX_S)

    def test_extras(self):
        text = urp_mat(tex=[("_OcclusionMap", TEX_N, (1, 1), (0, 0))], floats=[("_OcclusionStrength", 0.7), ("_ReceiveShadows", 0)],
                       keywords=["_DETAIL_MULX2", "_PARALLAXMAP"])
        n = self.normalize(text)
        self.assertEqual(n.occlusion_tex.guid, TEX_N)
        self.assertEqual(n.extras["urp"], {"occlusion_strength": 0.7, "receive_shadows": False, "detail": True, "parallax": True})

    def test_fingerprint_for_derived_shader(self):
        n = self.normalize(urp_mat(shader=URP_DERIVED))
        self.assertEqual(n.family, "urp")
        self.assertIsNone(n.shader_name)  # 表に無い（ライトのパイプラインの判定には使わない）
        self.assertEqual(n.base_color_tex.guid, TEX_A)

    def test_unlit_keeps_lighting(self):
        n = self.normalize(urp_mat(shader=URP_UNLIT))
        self.assertEqual((n.family, n.lighting), ("urp", "unlit"))

    def test_old_json_has_alpha_blend(self):
        data = self.normalize(urp_mat()).to_dict()
        del data["blend_mode"]  # 1.9.x までに保存した中間表現
        self.assertEqual(NormalizedMaterial.from_dict(data).blend_mode, "alpha")


if __name__ == "__main__":
    unittest.main()
