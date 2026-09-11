import unittest

from tests import _paths
from unitypackage_loader.core.material import TexRef, parse_material
from unitypackage_loader.core.mapping import resolve_materials
from unitypackage_loader.core.meta import ModelImporterInfo
from unitypackage_loader.core.profiles import ShaderTable, normalize_material, select_profile
from unitypackage_loader.core.profiles.liltoon import LilToonProfile
from unitypackage_loader.core.profiles.standard import GenericProfile, StandardProfile
from unitypackage_loader.core.unity_yaml import UnityRef

TEX_A = "a" * 32
TEX_N = "b" * 32
TEX_E = "c" * 32
LTS_O = "efa77a80ca0344749b4f19fdd5891cbe"       # lilToon lts_o（公開 GUID）
LTS_TRANS = "165365ab7100a044ca85fc8c33548a62"   # lilToon lts_trans（公開 GUID）


def mat_yaml(name, shader, tex=(), floats=(), colors=(), queue=-1, keywords=()):
    lines = [
        "%YAML 1.1",
        "%TAG !u! tag:unity3d.com,2011:",
        "--- !u!21 &2100000",
        "Material:",
        f"  m_Name: {name}",
        f"  m_Shader: {shader}",
        "  m_ValidKeywords:" + (" []" if not keywords else ""),
        *[f"  - {k}" for k in keywords],
        f"  m_CustomRenderQueue: {queue}",
        "  m_SavedProperties:",
        "    serializedVersion: 3",
        "    m_TexEnvs:",
    ]
    for prop, guid, scale, offset in tex:
        ref = f"{{fileID: 2800000, guid: {guid}, type: 3}}" if guid else "{fileID: 0}"
        lines += [
            f"    - {prop}:",
            f"        m_Texture: {ref}",
            f"        m_Scale: {{x: {scale[0]}, y: {scale[1]}}}",
            f"        m_Offset: {{x: {offset[0]}, y: {offset[1]}}}",
        ]
    lines.append("    m_Floats:")
    lines += [f"    - {k}: {v}" for k, v in floats]
    lines.append("    m_Colors:")
    lines += [f"    - {k}: {{r: {c[0]}, g: {c[1]}, b: {c[2]}, a: {c[3]}}}" for k, c in colors]
    return "\n".join(lines) + "\n"


LILTOON_OPAQUE = mat_yaml(
    "Skin", f"{{fileID: 4800000, guid: {LTS_O}, type: 3}}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_BumpMap", TEX_N, (1, 1), (0, 0)), ("_MatCapTex", TEX_E, (1, 1), (0, 0)), ("_AlphaMask", None, (1, 1), (0, 0))],
    floats=[("_lilToonVersion", 45), ("_Cutoff", 0.5), ("_Cull", 2), ("_UseBumpMap", 1), ("_BumpScale", 0.66), ("_UseEmission", 0), ("_UseReflection", 1), ("_Metallic", 0), ("_Smoothness", 0.6), ("_UseMatCap", 1), ("_UseShadow", 1), ("_ShadowStrength", 0.6), ("_UseOutline", 0), ("_OutlineWidth", 0.07)],
    colors=[("_Color", (1, 1, 1, 1)), ("_ShadowColor", (0.8, 0.7, 0.7, 1)), ("_OutlineColor", (0.5, 0.4, 0.4, 1)), ("_MatCapColor", (1, 1, 1, 1))],
)

LILTOON_TRANS = mat_yaml(
    "Glass", f"{{fileID: 4800000, guid: {LTS_TRANS}, type: 3}}",
    tex=[("_MainTex", TEX_A, (2, 2), (0.5, 0))],
    floats=[("_lilToonVersion", 45), ("_Cutoff", 0.001), ("_Cull", 0), ("_SrcBlend", 1), ("_DstBlend", 10), ("_UseReflection", 0)],
    colors=[("_Color", (0.9, 0.9, 0.9, 1))],
    queue=2450,
)

UNKNOWN_LILTOON_LIKE = mat_yaml(
    "Cloth", "{fileID: 4800000, guid: 12345678123456781234567812345678, type: 3}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0))],
    floats=[("_lilToonVersion", 45), ("_Cutoff", 0.5), ("_AlphaToMask", 1), ("_UseReflection", 0)],
    colors=[("_Color", (1, 1, 1, 1))],
)

STANDARD_CUTOUT = mat_yaml(
    "Leaf", "{fileID: 46, guid: 0000000000000000f000000000000000, type: 0}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_MetallicGlossMap", TEX_N, (1, 1), (0, 0)), ("_EmissionMap", TEX_E, (1, 1), (0, 0))],
    floats=[("_Mode", 1), ("_Cutoff", 0.3), ("_Metallic", 0.2), ("_Glossiness", 0.8), ("_BumpScale", 1)],
    colors=[("_Color", (0.5, 1, 0.5, 1)), ("_EmissionColor", (1, 0.5, 0, 1))],
    keywords=["_ALPHATEST_ON", "_EMISSION", "_METALLICGLOSSMAP"],
)

URP_TRANSPARENT = mat_yaml(
    "Window", "{fileID: 4800000, guid: 933532a4fcc9baf4fa0491de14d08ed7, type: 3}",
    tex=[("_BaseMap", TEX_A, (1, 1), (0, 0))],
    floats=[("_Surface", 1), ("_AlphaClip", 0), ("_Metallic", 0), ("_Smoothness", 0.9), ("_Cull", 2)],
    colors=[("_BaseColor", (1, 1, 1, 0.5))],
)

UNKNOWN_TOON = mat_yaml(
    "Mystery", "{fileID: 4800000, guid: 99999999999999999999999999999999, type: 3}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_ShadeTexture", TEX_N, (1, 1), (0, 0))],
    floats=[("_Cutoff", 0.5)],
    colors=[("_Color", (1, 1, 1, 1)), ("_ShadeColor", (0.5, 0.5, 0.5, 1))],
)


class ParseMaterialTests(unittest.TestCase):
    def test_parse_fields(self):
        mat = parse_material(LILTOON_OPAQUE, guid="m" * 32, pathname="Assets/X/Skin.mat")
        self.assertEqual(mat.name, "Skin")
        self.assertEqual(mat.shader, UnityRef(4800000, LTS_O, 3))
        self.assertEqual(mat.shader_guid, LTS_O)
        self.assertIsNone(mat.shader_builtin_id)
        self.assertEqual(mat.tex("_MainTex"), TexRef(TEX_A))
        self.assertIsNone(mat.tex("_AlphaMask"))            # fileID 0 は無視
        self.assertIn("_AlphaMask", mat.texture_slots)      # スロット自体は記録
        self.assertEqual(mat.f("_BumpScale"), 0.66)
        self.assertTrue(mat.flag("_UseBumpMap"))
        self.assertEqual(mat.color("_ShadowColor"), (0.8, 0.7, 0.7, 1.0))
        self.assertEqual(mat.render_queue, -1)

    def test_builtin_shader_id_and_keywords(self):
        mat = parse_material(STANDARD_CUTOUT)
        self.assertEqual(mat.shader_builtin_id, 46)
        self.assertIn("_EMISSION", mat.keywords)

    def test_texture_transform(self):
        mat = parse_material(LILTOON_TRANS)
        ref = mat.tex("_MainTex")
        self.assertEqual((ref.scale, ref.offset), ((2.0, 2.0), (0.5, 0.0)))
        self.assertTrue(ref.has_transform)


class ProfileSelectionTests(unittest.TestCase):
    def test_guid_table_selects_liltoon(self):
        profile, info = select_profile(parse_material(LILTOON_OPAQUE))
        self.assertIsInstance(profile, LilToonProfile)
        self.assertEqual((info.family, info.name, info.alpha, info.outline), ("liltoon", "lts_o", "opaque", True))

    def test_fingerprint_selects_liltoon_for_unknown_guid(self):
        profile, info = select_profile(parse_material(UNKNOWN_LILTOON_LIKE))
        self.assertIsInstance(profile, LilToonProfile)
        self.assertIsNone(info)

    def test_builtin_standard(self):
        profile, info = select_profile(parse_material(STANDARD_CUTOUT))
        self.assertIsInstance(profile, StandardProfile)
        self.assertEqual(info.name, "Standard")

    def test_unknown_falls_back_to_generic(self):
        profile, info = select_profile(parse_material(UNKNOWN_TOON))
        self.assertIsInstance(profile, GenericProfile)
        self.assertIsNone(info)

    def test_custom_table(self):
        table = ShaderTable()
        self.assertIsNotNone(table.lookup(parse_material(LILTOON_TRANS)))
        self.assertEqual(len([k for k in table.by_guid if table.by_guid[k].family == "liltoon"]), 65)


class NormalizeTests(unittest.TestCase):
    def test_liltoon_opaque(self):
        n = normalize_material(parse_material(LILTOON_OPAQUE, guid="m" * 32))
        self.assertEqual(n.family, "liltoon")
        self.assertEqual(n.lighting, "toon")
        self.assertEqual(n.alpha_mode, "opaque")
        self.assertFalse(n.alpha_from_texture)
        self.assertEqual(n.base_color_tex.guid, TEX_A)
        self.assertEqual(n.normal_tex.guid, TEX_N)
        self.assertAlmostEqual(n.normal_strength, 0.66)
        self.assertFalse(n.has_emission)
        self.assertAlmostEqual(n.roughness, 0.4)
        self.assertTrue(n.cull_backface)
        self.assertEqual(n.extras["shadow"]["strength"], 0.6)
        self.assertEqual(n.extras["outline"]["width"], 0.07)   # lts_o はアウトライン付きバリアント
        self.assertEqual(n.extras["matcap"]["tex"], TEX_E)
        self.assertEqual(n.source_guid, "m" * 32)
        self.assertEqual({t.guid for t in n.texture_refs()}, {TEX_A, TEX_N})

    def test_liltoon_transparent_variant(self):
        n = normalize_material(parse_material(LILTOON_TRANS))
        self.assertEqual(n.alpha_mode, "blend")
        self.assertTrue(n.alpha_from_texture)
        self.assertAlmostEqual(n.alpha_cutoff, 0.001)
        self.assertFalse(n.cull_backface)
        self.assertEqual(n.roughness, 1.0)  # 反射 OFF はマット扱い
        self.assertEqual((n.uv_scale, n.uv_offset), ((2.0, 2.0), (0.5, 0.0)))
        self.assertEqual(n.base_color, (0.9, 0.9, 0.9, 1.0))

    def test_liltoon_unknown_guid_uses_blend_state(self):
        n = normalize_material(parse_material(UNKNOWN_LILTOON_LIKE))
        self.assertEqual(n.alpha_mode, "cutout")  # _AlphaToMask: 1

    def test_standard_cutout(self):
        n = normalize_material(parse_material(STANDARD_CUTOUT))
        self.assertEqual((n.family, n.lighting, n.alpha_mode), ("standard", "pbr", "cutout"))
        self.assertAlmostEqual(n.alpha_cutoff, 0.3)
        self.assertAlmostEqual(n.metallic, 0.2)
        self.assertAlmostEqual(n.roughness, 0.2)
        self.assertEqual(n.metallic_tex.guid, TEX_N)
        self.assertEqual(n.emission_tex.guid, TEX_E)
        self.assertEqual(n.emission_color, (1.0, 0.5, 0.0, 1.0))
        self.assertTrue(n.has_emission)

    def test_urp_transparent(self):
        n = normalize_material(parse_material(URP_TRANSPARENT))
        self.assertEqual((n.family, n.alpha_mode), ("urp", "blend"))
        self.assertEqual(n.base_color, (1.0, 1.0, 1.0, 0.5))
        self.assertTrue(any("unverified" in w for w in n.warnings))

    def test_unknown_toon_hint(self):
        n = normalize_material(parse_material(UNKNOWN_TOON))
        self.assertEqual(n.family, "unknown")
        self.assertEqual(n.lighting, "toon")
        self.assertTrue(any("unknown shader" in w for w in n.warnings))


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.mats = {
            "1" * 32: parse_material(LILTOON_OPAQUE, guid="1" * 32, pathname="Assets/Chara/Materials/Skin.mat"),
            "2" * 32: parse_material(LILTOON_TRANS, guid="2" * 32, pathname="Assets/Chara/Materials/Glass.mat"),
            "3" * 32: parse_material(UNKNOWN_LILTOON_LIKE, guid="3" * 32, pathname="Assets/Other/Cloth.mat"),
            "4" * 32: parse_material(UNKNOWN_LILTOON_LIKE, guid="4" * 32, pathname="Assets/Chara/Materials/Cloth.mat"),
        }
        self.model_info = ModelImporterInfo(external_materials={"Skin": "1" * 32, "Skin.001": "1" * 32, "Missing": "9" * 32})

    def test_external_then_name_then_none(self):
        res = resolve_materials(
            ["Skin", "Skin.002", "Glass", "Cloth", "Cloth.001", "Nothing", "Missing"],
            self.model_info,
            self.mats,
            model_pathname="Assets/Chara/FBX/Chara.fbx",
        )
        self.assertEqual((res["Skin"].guid, res["Skin"].method), ("1" * 32, "external"))
        self.assertEqual((res["Skin.002"].guid, res["Skin.002"].method), ("1" * 32, "external"))
        self.assertEqual((res["Glass"].guid, res["Glass"].method), ("2" * 32, "name"))
        # 同名候補が複数ならモデルと同じフォルダ階層に近いものを選ぶ
        self.assertEqual(res["Cloth"].guid, "4" * 32)
        self.assertEqual(res["Cloth.001"].guid, "4" * 32)
        self.assertEqual((res["Nothing"].guid, res["Nothing"].method), (None, "none"))
        # externalObjects が指す .mat がパッケージに無ければ名前一致へフォールバック → 無ければ none
        self.assertEqual(res["Missing"].method, "none")

    def test_without_model_info(self):
        res = resolve_materials(["Skin"], None, self.mats)
        self.assertEqual(res["Skin"].method, "name")


class LocalSampleTests(unittest.TestCase):
    def test_every_local_material_normalizes(self):
        packages = _paths.local_packages()
        if not packages:
            self.skipTest("no local sample packages")
        from unitypackage_loader.core.package import UnityPackage

        for path in packages:
            pkg = UnityPackage(path)
            pkg.scan()
            mats = {e.guid: parse_material(pkg.read_text(e.guid), e.guid, e.pathname) for e in pkg.materials()}
            self.assertTrue(mats)
            for mat in mats.values():
                n = normalize_material(mat)
                self.assertIsNotNone(n.base_color_tex, f"{mat.name}: no base color texture")
                self.assertIn(n.alpha_mode, ("opaque", "cutout", "blend"))
            for model in pkg.models():
                info = ModelImporterInfo.from_meta(model.meta_text or "")
                if info.external_materials:
                    res = resolve_materials(list(info.external_materials), info, mats, model.pathname)
                    self.assertTrue(all(r.guid for r in res.values()), "all externalObjects should resolve")


if __name__ == "__main__":
    unittest.main()
