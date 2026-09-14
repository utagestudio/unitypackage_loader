import unittest

from tests import _paths
from unitypackage_loader.core.material import NormalizedMaterial, TexRef, matcap_blend_mode, parse_material
from unitypackage_loader.core.mapping import resolve_materials
from unitypackage_loader.core.meta import ModelImporterInfo
from unitypackage_loader.core.profiles import ShaderTable, normalize_material, select_profile
from unitypackage_loader.core.profiles.base import cull_backface
from unitypackage_loader.core.profiles.liltoon import LilToonProfile
from unitypackage_loader.core.profiles.standard import GenericProfile, StandardProfile
from unitypackage_loader.core.unity_yaml import UnityRef

TEX_A = "a" * 32
TEX_N = "b" * 32
TEX_E = "c" * 32
LTS_O = "efa77a80ca0344749b4f19fdd5891cbe"       # lilToon lts_o（公開 GUID）
LTS_TRANS = "165365ab7100a044ca85fc8c33548a62"   # lilToon lts_trans（公開 GUID）


def mat_yaml(name, shader, tex=(), floats=(), colors=(), queue=-1, keywords=(), invalid_keywords=()):
    lines = [
        "%YAML 1.1",
        "%TAG !u! tag:unity3d.com,2011:",
        "--- !u!21 &2100000",
        "Material:",
        f"  m_Name: {name}",
        f"  m_Shader: {shader}",
        "  m_ValidKeywords:" + (" []" if not keywords else ""),
        *[f"  - {k}" for k in keywords],
        "  m_InvalidKeywords:" + (" []" if not invalid_keywords else ""),
        *[f"  - {k}" for k in invalid_keywords],
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

# Standard から別シェーダーへ切り替えた後の残骸: _Color 黒 / _EmissionColor 白 が残り、_EMISSION は無効キーワードに移っている
STALE_EMISSION = mat_yaml(
    "Stale", "{fileID: 4800000, guid: 55555555555555555555555555555555, type: 3}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_EmissionMap", None, (1, 1), (0, 0))],
    floats=[("_Mode", 0), ("_Metallic", 0), ("_Glossiness", 0.1)],
    colors=[("_Color", (0, 0, 0, 1)), ("_EmissionColor", (1, 1, 1, 1))],
    invalid_keywords=["_EMISSION"],
)

# 発光を切った Standard（#52）: _EmissionColor は白く残っているが、キーワードに _EMISSION が無い
EMISSION_OFF = mat_yaml(
    "Pole", "{fileID: 46, guid: 0000000000000000f000000000000000, type: 0}",
    tex=[("_MainTex", TEX_A, (1, 1), (0, 0)), ("_EmissionMap", None, (1, 1), (0, 0))],
    floats=[("_Mode", 0), ("_Metallic", 0), ("_Glossiness", 0.5)],
    colors=[("_Color", (1, 1, 1, 1)), ("_EmissionColor", (3.48, 3.48, 3.48, 1))],
    keywords=["_METALLICGLOSSMAP", "_NORMALMAP"],
)
# 古い形式（m_ShaderKeywords の文字列）で _EMISSION を持つもの
LEGACY_EMISSION_ON = EMISSION_OFF.replace("  m_ValidKeywords:\n  - _METALLICGLOSSMAP\n  - _NORMALMAP\n", "  m_ShaderKeywords: _EMISSION _NORMALMAP\n")
# キーワードの記述が無いもの（手書き・古いツールの出力）
NO_KEYWORD_DATA = EMISSION_OFF.replace("  m_ValidKeywords:\n  - _METALLICGLOSSMAP\n  - _NORMALMAP\n", "")

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
        self.assertEqual(mat.invalid_keywords, [])

    def test_invalid_keywords(self):
        mat = parse_material(STALE_EMISSION)
        self.assertEqual(mat.keywords, [])
        self.assertEqual(mat.invalid_keywords, ["_EMISSION"])

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

    def test_cull_backface_property_names(self):
        base = parse_material(STANDARD_CUTOUT)
        self.assertTrue(cull_backface(base))                 # プロパティ無しは既定値
        self.assertFalse(cull_backface(base, default=False))
        for name in ("_Cull", "_CullMode", "_Culling"):
            for value, expected in ((0, False), (1, False), (2, True)):
                mat = parse_material(STANDARD_CUTOUT)
                mat.floats[name] = value
                self.assertEqual(cull_backface(mat), expected, f"{name}={value}")

    def test_invalid_emission_keyword_disables_emission(self):
        n = normalize_material(parse_material(STALE_EMISSION))
        self.assertEqual(n.family, "standard")
        self.assertFalse(n.has_emission)
        self.assertIsNone(n.emission_tex)
        self.assertTrue(any("_EMISSION" in w for w in n.warnings))

    def test_emission_needs_keyword_when_keywords_are_recorded(self):
        n = normalize_material(parse_material(EMISSION_OFF))
        self.assertFalse(n.has_emission)
        self.assertTrue(parse_material(EMISSION_OFF).keywords_known)

    def test_legacy_shader_keywords_enable_emission(self):
        mat = parse_material(LEGACY_EMISSION_ON)
        self.assertEqual((mat.keywords_known, "_EMISSION" in mat.keywords), (True, True))
        self.assertTrue(normalize_material(mat).has_emission)

    def test_missing_keyword_data_keeps_emission(self):
        mat = parse_material(NO_KEYWORD_DATA)
        self.assertFalse(mat.keywords_known)
        self.assertTrue(normalize_material(mat).has_emission)

    def test_urp_transparent(self):
        n = normalize_material(parse_material(URP_TRANSPARENT))
        self.assertEqual((n.family, n.alpha_mode), ("urp", "blend"))
        self.assertEqual(n.base_color, (1.0, 1.0, 1.0, 0.5))
        self.assertFalse(any("unverified" in w for w in n.warnings))  # URP Lit は実パッケージで GUID 確認済み

    def test_unverified_table_entry_warns(self):
        n = normalize_material(parse_material(URP_TRANSPARENT.replace("933532a4fcc9baf4fa0491de14d08ed7", "6e4ae4064600d784cac1e41a9e6f2e59")))
        self.assertEqual(n.family, "hdrp")
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
            mats = {e.guid: parse_material(pkg.read_asset(e.guid), e.guid, e.pathname) for e in pkg.materials()}
            self.assertTrue(mats)
            for mat in mats.values():
                n = normalize_material(mat)
                # 単色マテリアル（テクスチャ無し）も正当なので、正規化が通ることと値域だけ確認する
                self.assertIn(n.alpha_mode, ("opaque", "cutout", "blend"))
                self.assertEqual(len(n.base_color), 4)
                self.assertTrue(0.0 <= n.roughness <= 1.0)
            for model in pkg.models():
                info = ModelImporterInfo.from_meta(model.meta_text or "")
                if info.external_materials:
                    res = resolve_materials(list(info.external_materials), info, mats, model.pathname)
                    self.assertTrue(all(r.guid for r in res.values()), "all externalObjects should resolve")


HDRP_LIT = "{fileID: 4800000, guid: 6e4ae4064600d784cac1e41a9e6f2e59, type: 3}"  # HDRP/Lit（公開 GUID）
HDRP_GRAPH = "{fileID: -6465566751694194690, guid: " + "d" * 32 + ", type: 3}"  # 表に無い HDRP 向け Shader Graph


def hdrp_mat_yaml(shader, emissive=(0, 0, 0, 1), ldr=(0, 0, 0, 1), intensity=1, unit=0, use_intensity=0, emissive_map=None):
    """HDRP のマテリアル。発光の有無に関わらず _EmissionColor は白で持っている。"""
    return mat_yaml(
        "HdrpMat", shader,
        tex=[("_BaseColorMap", TEX_A, (1, 1), (0, 0)), ("_EmissiveColorMap", emissive_map, (1, 1), (0, 0))],
        floats=[("_EmissiveIntensity", intensity), ("_EmissiveIntensityUnit", unit), ("_UseEmissiveIntensity", use_intensity),
                ("_Metallic", 0), ("_Smoothness", 0.5)],
        colors=[("_BaseColor", (1, 1, 1, 1)), ("_EmissionColor", (1, 1, 1, 1)), ("_EmissiveColor", emissive),
                ("_EmissiveColorLDR", ldr)],
    )


class HdrpEmissionTests(unittest.TestCase):
    def test_white_emission_color_is_ignored(self):
        for shader in (HDRP_LIT, HDRP_GRAPH):
            with self.subTest(shader=shader):
                n = normalize_material(parse_material(hdrp_mat_yaml(shader)))
                self.assertEqual(n.base_color_tex.guid, TEX_A)
                self.assertFalse(n.has_emission)
                self.assertIsNone(n.emission_tex)
                self.assertNotIn("hdrp_emissive", n.extras)

    def test_emissive_map_without_color_does_not_emit(self):
        n = normalize_material(parse_material(hdrp_mat_yaml(HDRP_LIT, emissive_map=TEX_E)))
        self.assertFalse(n.has_emission)
        self.assertIsNone(n.emission_tex)

    def test_emissive_color_uses_hue_and_keeps_intensity(self):
        yaml = hdrp_mat_yaml(HDRP_LIT, emissive=(30000, 15000, 7500, 1), ldr=(1, 0.73, 0.53, 1), intensity=30000,
                             use_intensity=1, emissive_map=TEX_E)
        n = normalize_material(parse_material(yaml))
        self.assertEqual(n.family, "hdrp")
        self.assertTrue(n.has_emission)
        self.assertEqual(n.emission_tex.guid, TEX_E)
        self.assertEqual(n.emission_color, (1.0, 0.5, 0.25, 1.0))
        self.assertEqual(n.emission_strength, 1.0)
        self.assertEqual(n.extras["hdrp_emissive"],
                         {"color": [30000.0, 15000.0, 7500.0], "intensity": 30000.0, "unit": "nits", "use_intensity": True})

    def test_hdr_color_without_intensity_mode(self):
        n = normalize_material(parse_material(hdrp_mat_yaml(HDRP_GRAPH, emissive=(8, 8, 4, 1), intensity=32, unit=1)))
        self.assertEqual(n.emission_color, (1.0, 1.0, 0.5, 1.0))
        self.assertIsNone(n.emission_tex)
        self.assertEqual(n.extras["hdrp_emissive"]["unit"], "ev100")
        self.assertFalse(n.extras["hdrp_emissive"]["use_intensity"])

    def test_extras_survive_round_trip(self):
        n = normalize_material(parse_material(hdrp_mat_yaml(HDRP_LIT, emissive=(2, 1, 0, 1))))
        restored = NormalizedMaterial.from_dict(n.to_dict())
        self.assertEqual(restored.emission_color, n.emission_color)
        self.assertEqual(restored.extras["hdrp_emissive"], n.extras["hdrp_emissive"])


if __name__ == "__main__":
    unittest.main()


class RoundTripTests(unittest.TestCase):
    def test_normalized_to_dict_from_dict(self):
        from unitypackage_loader.core.material import NormalizedMaterial

        n = normalize_material(parse_material(LILTOON_OPAQUE, guid="m" * 32, pathname="Assets/X/Skin.mat"))
        n.warnings.append("dropped on purpose")
        data = n.to_dict()
        self.assertNotIn("warnings", data)
        self.assertEqual(data["base_color_tex"]["guid"], TEX_A)
        restored = NormalizedMaterial.from_dict(data)
        self.assertEqual(restored.name, n.name)
        self.assertEqual(restored.base_color_tex, n.base_color_tex)
        self.assertEqual(restored.normal_tex, n.normal_tex)
        self.assertIsNone(restored.emission_tex)
        self.assertEqual(restored.base_color, n.base_color)
        self.assertEqual(restored.alpha_mode, n.alpha_mode)
        self.assertEqual(restored.extras["shadow"]["strength"], 0.6)
        self.assertEqual(restored.extras["matcap"]["tex"], TEX_E)
        self.assertEqual(restored.extra_texture_guids(), [TEX_E])
        self.assertEqual(restored.warnings, [])
        # JSON 経由でも同じ（tuple → list になる）
        import json

        again = NormalizedMaterial.from_dict(json.loads(json.dumps(data)))
        self.assertEqual(again.uv_scale, (1.0, 1.0))
        self.assertEqual(again.base_color_tex.scale, (1.0, 1.0))

    def test_from_dict_ignores_unknown_keys(self):
        from unitypackage_loader.core.material import NormalizedMaterial

        n = NormalizedMaterial.from_dict({"name": "X", "future_field": 1, "base_color_tex": None})
        self.assertEqual(n.name, "X")
        self.assertIsNone(n.base_color_tex)


class MatCapBlendModeTests(unittest.TestCase):
    """MatCap のブレンドモード決定（Toon ノードグループの MatCap Mode 入力）。"""

    def test_liltoon_blend_mode_is_used_as_is(self) -> None:
        for value in (0, 1, 2, 3):
            self.assertEqual(matcap_blend_mode({"blend_mode": value}), value)

    def test_additive_flag_selects_add_when_blend_mode_is_absent(self) -> None:
        # MToon と VRChat Mobile の MatCap は blend_mode を持たず additive で来る。
        # ここで Normal を選ぶと MatCap がマテリアル色を置き換えてしまう。
        self.assertEqual(matcap_blend_mode({"additive": True}), 1)
        self.assertEqual(matcap_blend_mode({"additive": False}), 0)

    def test_out_of_range_blend_mode_falls_back_to_additive_flag(self) -> None:
        self.assertEqual(matcap_blend_mode({"blend_mode": 9, "additive": True}), 1)
        self.assertEqual(matcap_blend_mode({"blend_mode": -1, "additive": False}), 0)

    def test_unusable_blend_mode_falls_back(self) -> None:
        self.assertEqual(matcap_blend_mode({"blend_mode": None, "additive": True}), 1)
        self.assertEqual(matcap_blend_mode({"blend_mode": "x", "additive": True}), 1)

    def test_empty_matcap_is_normal(self) -> None:
        self.assertEqual(matcap_blend_mode({}), 0)
