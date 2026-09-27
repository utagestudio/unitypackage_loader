"""HDRP のプロファイル（#119）。値の意味は HDRP の Lit.shader / LitDataIndividualLayer.hlsl / Unlit.shader に合わせている。"""

import unittest

from tests import _paths  # noqa: F401
from tests.test_material import TEX_A, TEX_E, TEX_N, mat_yaml
from unitypackage_loader.core.material import NormalizedMaterial, parse_material
from unitypackage_loader.core.profiles import normalize_material, select_profile
from unitypackage_loader.core.profiles.hdrp import HdrpLitProfile

HDRP_LIT = "{fileID: 4800000, guid: 6e4ae4064600d784cac1e41a9e6f2e59, type: 3}"
HDRP_LAYERED = "{fileID: 4800000, guid: 81d02e8644315b742b154842a3a2f98c, type: 3}"
HDRP_UNLIT = "{fileID: 4800000, guid: c4edd00ff2db5b24391a4fcb1762e459, type: 3}"
HDRP_DERIVED = "{fileID: 4800000, guid: " + "6" * 32 + ", type: 3}"  # 表に無い、HDRP/Lit と同じ名前を使うシェーダー
TEX_M = "d" * 32
TEX_S = "e" * 32


def hdrp_mat(tex=(), floats=(), colors=(), keywords=(), shader=HDRP_LIT):
    """HDRP/Lit の .mat。互換用の _Color（黒）/ _MainTex と、Standard の _Mode / _Glossiness が残っている。"""
    return mat_yaml(
        "Hdrp", shader,
        tex=[("_BaseColorMap", TEX_A, (4, 2), (0, 0)), ("_MainTex", TEX_E, (1, 1), (0, 0)), *tex],
        floats=[("_SurfaceType", 0), ("_Metallic", 0.3), ("_Smoothness", 0.8), ("_Glossiness", 0.1), ("_Mode", 3), *floats],
        colors=[("_BaseColor", (0.75, 0.75, 0.75, 1)), ("_Color", (0, 0, 0, 1)), ("_EmissionColor", (1, 1, 1, 1)), *colors],
        keywords=keywords,
    )


class HdrpLitTests(unittest.TestCase):
    def normalize(self, text):
        return normalize_material(parse_material(text))

    def test_reads_only_hdrp_names(self):
        self.assertIsInstance(select_profile(parse_material(hdrp_mat()))[0], HdrpLitProfile)
        n = self.normalize(hdrp_mat())
        self.assertEqual((n.family, n.shader_name), ("hdrp", "HDRP/Lit"))
        self.assertEqual(n.base_color_tex.guid, TEX_A)  # 残った _MainTex ではない
        self.assertEqual(n.uv_scale, (4.0, 2.0))
        self.assertEqual(n.base_color, (0.75, 0.75, 0.75, 1.0))  # 残った黒い _Color ではない
        self.assertAlmostEqual(n.metallic, 0.3)
        self.assertAlmostEqual(n.roughness, 0.2)  # 残った _Glossiness ではない
        self.assertEqual(n.alpha_mode, "opaque")  # 残った _Mode: 3 ではない
        self.assertFalse(n.has_emission)  # 白い _EmissionColor はベイク用
        self.assertEqual(n.warnings, [])

    def test_mask_map_remap(self):
        # マスクマップがあれば _Metallic / _Smoothness は使わず、Remap の範囲をマップの R / A で補間する
        text = hdrp_mat(tex=[("_MaskMap", TEX_M, (1, 1), (0, 0))],
                        floats=[("_MetallicRemapMin", 0.1), ("_MetallicRemapMax", 0.9), ("_SmoothnessRemapMin", 0.2), ("_SmoothnessRemapMax", 0.7),
                                ("_AORemapMin", 0.5), ("_AORemapMax", 1)])
        n = self.normalize(text)
        self.assertEqual(n.metallic_tex.guid, TEX_M)
        self.assertEqual(n.metallic_remap, (0.1, 0.9))
        self.assertAlmostEqual(n.smoothness_offset, 0.2)
        self.assertAlmostEqual(n.smoothness_scale, 0.5)
        self.assertEqual(n.extras["hdrp"]["ao_remap"], [0.5, 1.0])

    def test_normal_and_double_sided(self):
        text = hdrp_mat(tex=[("_NormalMap", TEX_N, (1, 1), (0, 0)), ("_BumpMap", TEX_E, (1, 1), (0, 0))],
                        floats=[("_NormalScale", 0.4), ("_BumpScale", 2), ("_DoubleSidedEnable", 1), ("_CullMode", 2)])
        n = self.normalize(text)
        self.assertEqual((n.normal_tex.guid, n.normal_strength), (TEX_N, 0.4))
        self.assertFalse(n.cull_backface)  # _DoubleSidedEnable を _CullMode より優先する

    def test_transparent_and_blend_modes(self):
        base = hdrp_mat(floats=[("_BlendMode", 0)]).replace("_SurfaceType: 0", "_SurfaceType: 1")
        self.assertEqual(self.normalize(base).alpha_mode, "blend")
        for value, mode, warns in ((0, "alpha", False), (1, "additive", True), (4, "premultiply", False)):
            with self.subTest(mode=mode):
                n = self.normalize(base.replace("_BlendMode: 0", f"_BlendMode: {value}"))
                self.assertEqual(n.blend_mode, mode)
                self.assertEqual(any("blend mode" in w for w in n.warnings), warns)

    def test_alpha_cutoff(self):
        n = self.normalize(hdrp_mat(floats=[("_AlphaCutoffEnable", 1), ("_AlphaCutoff", 0.35), ("_Cutoff", 0.9)]))
        self.assertEqual(n.alpha_mode, "cutout")
        self.assertAlmostEqual(n.alpha_cutoff, 0.35)  # 残った _Cutoff ではない

    def test_specular_color_material(self):
        text = hdrp_mat(tex=[("_MaskMap", TEX_M, (1, 1), (0, 0)), ("_SpecularColorMap", TEX_S, (1, 1), (0, 0))],
                        floats=[("_MaterialID", 4)], colors=[("_SpecularColor", (0.2, 0.3, 0.1, 1))])
        n = self.normalize(text)
        self.assertEqual(n.specular_color, (0.2, 0.3, 0.1, 1.0))
        self.assertEqual(n.specular_tex.guid, TEX_S)
        self.assertFalse(n.smoothness_from_specular)  # HDRP のマップの A は Smoothness ではない
        self.assertEqual((n.metallic, n.metallic_remap), (0.0, (0.0, 0.0)))  # マスクマップの R でも金属にしない
        self.assertEqual(n.extras["hdrp"]["material_type"], "specular_color")

    def test_other_material_types_are_kept_in_extras(self):
        n = self.normalize(hdrp_mat(floats=[("_MaterialID", 2)]))
        self.assertEqual(n.extras["hdrp"]["material_type"], "anisotropy")
        self.assertIsNone(n.specular_color)
        self.assertNotIn("hdrp", self.normalize(hdrp_mat(floats=[("_MaterialID", 1)])).extras)

    def test_emission(self):
        n = self.normalize(hdrp_mat(tex=[("_EmissiveColorMap", TEX_E, (1, 1), (0, 0))], colors=[("_EmissiveColor", (8, 4, 2, 1))]))
        self.assertEqual(n.emission_color, (1.0, 0.5, 0.25, 1.0))
        self.assertEqual(n.emission_tex.guid, TEX_E)

    def test_layered_lit_reads_layer_zero(self):
        text = mat_yaml(
            "Layered", HDRP_LAYERED,
            tex=[("_BaseColorMap0", TEX_A, (5, 10), (0, 0)), ("_BaseColorMap1", TEX_E, (1, 1), (0, 0)), ("_MainTex", TEX_E, (1, 1), (0, 0)),
                 ("_NormalMap0", TEX_N, (1, 1), (0, 0)), ("_MaskMap0", TEX_M, (1, 1), (0, 0))],
            floats=[("_SurfaceType", 0), ("_LayerCount", 2), ("_Metallic0", 0.8), ("_NormalScale0", 0.6),
                    ("_SmoothnessRemapMin0", 0.1), ("_SmoothnessRemapMax0", 0.6)],
            colors=[("_BaseColor0", (0.9, 0.9, 0.9, 1)), ("_BaseColor", (0.75, 0.75, 0.75, 1)), ("_Color", (0, 0, 0, 1))],
        )
        n = self.normalize(text)
        self.assertEqual(n.shader_name, "HDRP/LayeredLit")
        self.assertEqual((n.base_color_tex.guid, n.uv_scale), (TEX_A, (5.0, 10.0)))
        self.assertEqual(n.base_color, (0.9, 0.9, 0.9, 1.0))  # 残った _Color（黒）/ _BaseColor ではない
        self.assertEqual((n.normal_tex.guid, n.normal_strength), (TEX_N, 0.6))
        self.assertEqual(n.metallic_tex.guid, TEX_M)
        self.assertAlmostEqual(n.smoothness_offset, 0.1)
        self.assertEqual(n.extras["hdrp"]["layer_count"], 2)

    def test_unlit(self):
        text = mat_yaml("Unlit", HDRP_UNLIT, tex=[("_UnlitColorMap", TEX_A, (1, 1), (0, 0)), ("_BaseColorMap", TEX_E, (1, 1), (0, 0))],
                        floats=[("_SurfaceType", 0), ("_DoubleSidedEnable", 1)],
                        colors=[("_UnlitColor", (0.5, 1, 0.5, 1)), ("_BaseColor", (0, 0, 0, 1))])
        n = self.normalize(text)
        self.assertEqual((n.lighting, n.base_color_tex.guid, n.base_color), ("unlit", TEX_A, (0.5, 1.0, 0.5, 1.0)))
        self.assertFalse(n.cull_backface)

    def test_fingerprint(self):
        n = self.normalize(hdrp_mat(shader=HDRP_DERIVED))
        self.assertEqual(n.family, "hdrp")
        self.assertIsNone(n.shader_name)
        # _MaskMap だけでは HDRP/Lit にしない（Shader Graph が自分の名前として持ち、ベース画像は _MainTex のことがある）
        text = mat_yaml("Graph", HDRP_DERIVED, tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_MaskMap", TEX_M, (1, 1), (0, 0))],
                        floats=[("_SurfaceType", 0)])
        self.assertNotEqual(self.normalize(text).family, "hdrp")

    def test_old_json(self):
        data = self.normalize(hdrp_mat(tex=[("_MaskMap", TEX_M, (1, 1), (0, 0))])).to_dict()
        again = NormalizedMaterial.from_dict(data)
        self.assertEqual(again.metallic_remap, (0.0, 1.0))
        del data["metallic_remap"], data["smoothness_offset"]  # 1.9.x までに保存した中間表現
        old = NormalizedMaterial.from_dict(data)
        self.assertEqual((old.metallic_remap, old.smoothness_offset), ((0.0, 1.0), 0.0))


if __name__ == "__main__":
    unittest.main()
