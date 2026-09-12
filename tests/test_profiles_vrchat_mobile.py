"""VRChat SDK Mobile シェーダー（Quest 向け）のプロファイル。GUID は SDK の .shader.meta に記載の公開値。"""

import unittest

from tests import _paths  # noqa: F401
from tests.test_material import TEX_A, TEX_E, TEX_N, mat_yaml
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.profiles import normalize_material, select_profile
from unitypackage_loader.core.profiles.vrchat_mobile import VRChatMobileProfile

TOON_LIT = "affc81f3d164d734d8f13053effb1c5c"
STANDARD_LITE = "0b7113dea2069fc4e8943843eff19f70"


def shader(guid: str) -> str:
    return f"{{fileID: 4800000, guid: {guid}, type: 3}}"


# Standard から Toon Lit に切り替えた後の典型: _Color 黒 / _EmissionColor 白 / _EMISSION が無効キーワードに残る
TOON_LIT_STALE = mat_yaml(
    "Body", shader(TOON_LIT),
    tex=[("_MainTex", TEX_A, (2, 1), (0.25, 0)), ("_BumpMap", TEX_N, (1, 1), (0, 0)), ("_EmissionMap", None, (1, 1), (0, 0))],
    floats=[("_Mode", 0), ("_Cull", 0), ("_Metallic", 0), ("_Glossiness", 0.06), ("_Cutoff", 0.5)],
    colors=[("_Color", (0, 0, 0, 1)), ("_EmissionColor", (1, 1, 1, 1))],
    invalid_keywords=["_EMISSION"],
)

STANDARD_LITE_EMISSION_OFF = mat_yaml(
    "Metal", shader(STANDARD_LITE),
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_BumpMap", TEX_N, (1, 1), (0, 0)), ("_MetallicGlossMap", TEX_E, (1, 1), (0, 0)), ("_EmissionMap", None, (1, 1), (0, 0))],
    floats=[("_Metallic", 0.75), ("_Glossiness", 0.4), ("_BumpScale", 0.5), ("_EnableEmission", 0), ("_Mode", 2), ("_Cull", 0)],
    colors=[("_Color", (0.7, 0.1, 0.1, 1)), ("_EmissionColor", (1, 1, 1, 1))],
)

STANDARD_LITE_EMISSION_ON = mat_yaml(
    "Lamp", shader(STANDARD_LITE),
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_EmissionMap", TEX_E, (1, 1), (0, 0))],
    floats=[("_Metallic", 0), ("_Glossiness", 0.4), ("_EnableEmission", 1)],
    colors=[("_Color", (1, 1, 1, 1)), ("_EmissionColor", (1, 0.5, 0, 1))],
    keywords=["_EMISSION"],
)


class ToonLitTests(unittest.TestCase):
    def test_only_main_tex_is_used(self):
        mat = parse_material(TOON_LIT_STALE)
        profile, info = select_profile(mat)
        self.assertIsInstance(profile, VRChatMobileProfile)
        self.assertEqual(info.name, "VRChat/Mobile/Toon Lit")
        n = normalize_material(mat)
        self.assertEqual((n.family, n.lighting, n.alpha_mode), ("vrchat_mobile", "unlit", "opaque"))
        self.assertEqual(n.extras["variant"], "toon_lit")
        self.assertEqual(n.base_color_tex.guid, TEX_A)
        self.assertEqual((n.uv_scale, n.uv_offset), ((2.0, 1.0), (0.25, 0.0)))
        self.assertEqual(n.base_color, (1.0, 1.0, 1.0, 1.0))  # 残骸の _Color 黒は無視
        self.assertFalse(n.has_emission)                      # 残骸の _EmissionColor 白も無視
        self.assertIsNone(n.normal_tex)                        # 法線マップは参照しないシェーダー
        self.assertTrue(n.cull_backface)                       # _Cull も参照しない
        self.assertEqual(n.roughness, 1.0)
        self.assertEqual({t.guid for t in n.texture_refs()}, {TEX_A})
        self.assertEqual(n.warnings, [])


class StandardLiteTests(unittest.TestCase):
    def test_pbr_values_and_emission_gated_by_keyword(self):
        n = normalize_material(parse_material(STANDARD_LITE_EMISSION_OFF))
        self.assertEqual((n.family, n.lighting, n.alpha_mode), ("vrchat_mobile", "pbr", "opaque"))
        self.assertEqual(n.shader_name, "VRChat/Mobile/Standard Lite")
        self.assertEqual(n.base_color, (0.7, 0.1, 0.1, 1.0))
        self.assertEqual(n.normal_tex.guid, TEX_N)
        self.assertAlmostEqual(n.normal_strength, 0.5)
        self.assertAlmostEqual(n.metallic, 0.75)
        self.assertAlmostEqual(n.roughness, 0.6)
        self.assertEqual(n.metallic_tex.guid, TEX_E)
        self.assertFalse(n.has_emission)        # _EMISSION キーワードが無ければ _EmissionColor 白でも光らない
        self.assertTrue(n.cull_backface)        # 不透明のみ・カリング設定無し
        self.assertFalse(n.alpha_from_texture)  # _Mode 2 の残骸は無視

    def test_emission_on(self):
        n = normalize_material(parse_material(STANDARD_LITE_EMISSION_ON))
        self.assertTrue(n.has_emission)
        self.assertEqual(n.emission_tex.guid, TEX_E)
        self.assertEqual(n.emission_color, (1.0, 0.5, 0.0, 1.0))
        self.assertEqual(n.emission_strength, 1.0)
