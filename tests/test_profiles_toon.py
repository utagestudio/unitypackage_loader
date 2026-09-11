import unittest

from tests import _paths  # noqa: F401
from tests.test_material import TEX_A, TEX_E, TEX_N, mat_yaml
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.profiles import normalize_material, select_profile
from unitypackage_loader.core.profiles.mtoon import MToonProfile
from unitypackage_loader.core.profiles.poiyomi import PoiyomiProfile

MTOON_LEGACY = mat_yaml(
    "Skin", "{fileID: 4800000, guid: 1a97144e4ad27a04aafd70f7b915cedb, type: 3}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_ShadeTexture", TEX_A, (1, 1), (0, 0)), ("_BumpMap", TEX_N, (1, 1), (0, 0)), ("_SphereAdd", TEX_E, (1, 1), (0, 0))],
    floats=[("_BlendMode", 1), ("_Cutoff", 0.6), ("_CullMode", 0), ("_BumpScale", 0.8), ("_OutlineWidthMode", 1), ("_OutlineWidth", 0.2), ("_ShadeShift", -0.1), ("_ShadeToony", 0.9)],
    colors=[("_Color", (1, 0.9, 0.9, 1)), ("_ShadeColor", (0.6, 0.5, 0.5, 1)), ("_EmissionColor", (0, 0, 0, 1)), ("_OutlineColor", (0, 0, 0, 1)), ("_RimColor", (0, 0, 0, 1))],
)

MTOON10_UNKNOWN_GUID = mat_yaml(
    "Hair", "{fileID: 4800000, guid: 77777777777777777777777777777777, type: 3}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_ShadeTex", TEX_A, (1, 1), (0, 0)), ("_EmissionMap", TEX_E, (1, 1), (0, 0)), ("_MatcapTex", TEX_N, (1, 1), (0, 0))],
    floats=[("_AlphaMode", 2), ("_TransparentWithZWrite", 1), ("_Cutoff", 0.5), ("_DoubleSided", 1), ("_OutlineWidthMode", 0)],
    colors=[("_Color", (1, 1, 1, 1)), ("_ShadeColor", (0.7, 0.7, 0.8, 1)), ("_EmissionColor", (1, 1, 1, 1)), ("_MatcapColor", (1, 1, 1, 1))],
)

POIYOMI = mat_yaml(
    "Outfit", "{fileID: 4800000, guid: 88888888888888888888888888888888, type: 3}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_BumpMap", TEX_N, (1, 1), (0, 0)), ("_EmissionMap", TEX_E, (1, 1), (0, 0))],
    floats=[("_ShaderOptimizerEnabled", 0), ("_LightingMode", 1), ("_Mode", 1), ("_Cutoff", 0.4), ("_Cull", 2), ("_BumpScale", 1), ("_EnableEmission", 1), ("_EmissionStrength", 2.5), ("_Metallic", 0.8), ("_Glossiness", 0.9), ("_EnableOutlines", 1), ("_LineWidth", 0.02), ("_ShadowStrength", 0.7)],
    colors=[("_Color", (1, 1, 1, 1)), ("_EmissionColor", (1, 0.2, 0.2, 1)), ("_LineColor", (0.1, 0.1, 0.1, 1))],
)

POIYOMI_ADDITIVE_NO_EMISSION = mat_yaml(
    "Glow", "{fileID: 4800000, guid: 88888888888888888888888888888888, type: 3}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0))],
    floats=[("_ShaderOptimizerEnabled", 1), ("_ShadingEnabled", 1), ("_Mode", 4), ("_EnableEmission", 0)],
    colors=[("_Color", (1, 1, 1, 1)), ("_EmissionColor", (1, 1, 1, 1))],
)


class MToonTests(unittest.TestCase):
    def test_legacy_by_guid(self):
        mat = parse_material(MTOON_LEGACY)
        profile, info = select_profile(mat)
        self.assertIsInstance(profile, MToonProfile)
        self.assertEqual(info.name, "VRM/MToon")
        n = normalize_material(mat)
        self.assertEqual((n.family, n.lighting, n.alpha_mode), ("mtoon", "toon", "cutout"))
        self.assertAlmostEqual(n.alpha_cutoff, 0.6)
        self.assertFalse(n.cull_backface)  # _CullMode 0 = Off
        self.assertEqual(n.normal_tex.guid, TEX_N)
        self.assertAlmostEqual(n.normal_strength, 0.8)
        self.assertFalse(n.has_emission)
        self.assertEqual(n.roughness, 1.0)
        self.assertEqual(n.extras["shade"]["color"], (0.6, 0.5, 0.5, 1.0))
        self.assertAlmostEqual(n.extras["shade"]["shift"], -0.1)
        self.assertEqual(n.extras["matcap"]["tex"], TEX_E)
        self.assertEqual(n.extras["outline"]["mode"], 1)

    def test_mtoon10_by_fingerprint(self):
        mat = parse_material(MTOON10_UNKNOWN_GUID)
        profile, info = select_profile(mat)
        self.assertIsInstance(profile, MToonProfile)
        self.assertIsNone(info)
        n = normalize_material(mat)
        self.assertEqual(n.alpha_mode, "blend")
        self.assertFalse(n.cull_backface)  # _DoubleSided 1
        self.assertTrue(n.has_emission)
        self.assertEqual(n.emission_tex.guid, TEX_E)
        self.assertEqual(n.extras["matcap"]["tex"], TEX_N)
        self.assertNotIn("outline", n.extras)


class PoiyomiTests(unittest.TestCase):
    def test_fingerprint_and_values(self):
        mat = parse_material(POIYOMI)
        profile, info = select_profile(mat)
        self.assertIsInstance(profile, PoiyomiProfile)
        self.assertIsNone(info)
        n = normalize_material(mat)
        self.assertEqual((n.family, n.lighting, n.alpha_mode), ("poiyomi", "toon", "cutout"))
        self.assertAlmostEqual(n.alpha_cutoff, 0.4)
        self.assertTrue(n.cull_backface)
        self.assertEqual(n.emission_tex.guid, TEX_E)
        self.assertAlmostEqual(n.emission_strength, 2.5)
        # 反射が無効なので Metallic/Smoothness は無視してマット扱い
        self.assertEqual((n.metallic, n.roughness), (0.0, 1.0))
        self.assertEqual(n.extras["outline"]["width"], 0.02)
        self.assertEqual(n.extras["shading"]["shadow_strength"], 0.7)

    def test_additive_mode_and_disabled_emission(self):
        n = normalize_material(parse_material(POIYOMI_ADDITIVE_NO_EMISSION))
        self.assertEqual(n.alpha_mode, "blend")
        self.assertFalse(n.has_emission)
        self.assertTrue(any("approximated" in w for w in n.warnings))


if __name__ == "__main__":
    unittest.main()
