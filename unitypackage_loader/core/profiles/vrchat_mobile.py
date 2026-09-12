"""VRChat SDK 同梱の Mobile（Quest / Android 向け）シェーダーの変換規則。

これらのシェーダーは機能が少なく、Standard から切り替えたマテリアルには Standard 時代の
_Color や _EmissionColor がそのまま残っていることが多い。そのため各シェーダーが実際に
参照するプロパティだけを読み、それ以外は無視する。バリアントは shader_guids.json の
``variant`` で指定する（GUID 表に無いものはプロパティ指紋で Toon Standard だけ判定できる）。
"""

from __future__ import annotations

from ..material import BLACK, NormalizedMaterial, UnityMaterial
from .base import ShaderInfo, ShaderProfile, cull_backface, is_black, texture_transform


class VRChatMobileProfile(ShaderProfile):
    family = "vrchat_mobile"
    lighting = "unlit"

    def matches(self, mat: UnityMaterial) -> bool:
        # Toon Standard だけは固有プロパティで判定できる。他は Standard の残骸と区別できないので GUID 表のみ
        return mat.has("_ShadowBoost") and mat.has("_MinBrightness") and mat.has("_MetallicStrength") and "_Ramp" in mat.texture_slots

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        variant = str((info.extra or {}).get("variant", "")) if info is not None else ""
        if not variant:
            variant = "toon_standard"  # 指紋判定で来るのは Toon Standard だけ
        n = self._base(mat, info)
        n.base_color_tex = mat.tex("_MainTex")
        n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)
        n.metallic, n.roughness = 0.0, 1.0
        n.alpha_mode = "opaque"
        n.alpha_from_texture = False
        n.extras["variant"] = variant
        handler = getattr(self, f"_{variant}", None)
        if handler is None:
            n.warnings.append(f"unknown VRChat Mobile variant {variant!r}; only _MainTex is used")
            return n
        handler(mat, info, n)
        return n

    # ---- バリアント別 ----

    def _toon_lit(self, mat: UnityMaterial, info: ShaderInfo | None, n: NormalizedMaterial) -> None:
        """VRChat/Mobile/Toon Lit: _MainTex だけを参照する Unlit。_Color も _EmissionColor も使わない。"""
        n.lighting = "unlit"

    def _standard_lite(self, mat: UnityMaterial, info: ShaderInfo | None, n: NormalizedMaterial) -> None:
        """VRChat/Mobile/Standard Lite: Standard の簡略版。不透明のみで、emission は _EMISSION キーワードでゲートされる。"""
        n.lighting = "pbr"
        n.base_color = mat.color("_Color")
        if mat.tex("_BumpMap"):
            n.normal_tex = mat.tex("_BumpMap")
            n.normal_strength = mat.f("_BumpScale", 1.0)
        # シェーダー既定値は Metallic 1.0 / Smoothness 1.0
        n.metallic = max(0.0, min(1.0, mat.f("_Metallic", 1.0)))
        n.roughness = max(0.0, min(1.0, 1.0 - mat.f("_Glossiness", 1.0)))
        n.metallic_tex = mat.tex("_MetallicGlossMap")
        n.occlusion_tex = mat.tex("_OcclusionMap")
        if "_EMISSION" in mat.keywords:
            emission_tex = mat.tex("_EmissionMap")
            emission_color = mat.color("_EmissionColor", default=BLACK)
            if emission_tex is not None or not is_black(emission_color):
                n.emission_tex = emission_tex
                n.emission_color = emission_color
        if mat.tex("_DetailAlbedoMap") or mat.tex("_DetailNormalMap"):
            n.extras["detail"] = {
                "albedo": mat.tex("_DetailAlbedoMap").guid if mat.tex("_DetailAlbedoMap") else None,
                "normal": mat.tex("_DetailNormalMap").guid if mat.tex("_DetailNormalMap") else None,
                "mask": mat.tex("_DetailMask").guid if mat.tex("_DetailMask") else None,
                "uv": int(mat.f("_UVSec", 0)),
            }

    def _toon_standard(self, mat: UnityMaterial, info: ShaderInfo | None, n: NormalizedMaterial) -> None:
        """VRChat/Mobile/Toon Standard（Outline 版含む）: 不透明のみのトゥーン。各機能は USE_* キーワードで ON/OFF。"""
        n.lighting = "toon"
        n.base_color = mat.color("_Color")
        n.cull_backface = cull_backface(mat, default=True)  # _Culling
        keywords = set(mat.keywords)

        if "USE_NORMAL_MAPS" in keywords and mat.tex("_BumpMap"):
            n.normal_tex = mat.tex("_BumpMap")
            n.normal_strength = mat.f("_BumpScale", 1.0)

        # エミッションは常に有効（キーワード無し）。_EmissionStrength は 0〜2 の乗数
        emission_tex = mat.tex("_EmissionMap")
        emission_color = mat.color("_EmissionColor", default=BLACK)
        if emission_tex is not None or not is_black(emission_color):
            n.emission_tex = emission_tex
            n.emission_color = emission_color
            n.emission_strength = max(mat.f("_EmissionStrength", 1.0), 0.0)

        # スペキュラ: _Metallic / _Glossiness は Standard の残骸で、実際の値は _MetallicStrength / _GlossStrength
        if "USE_SPECULAR" in keywords:
            n.metallic = max(0.0, min(1.0, mat.f("_MetallicStrength", 0.0)))
            n.roughness = max(0.0, min(1.0, 1.0 - mat.f("_GlossStrength", 0.5)))
            n.extras["specular"] = {
                "metallic_map": _guid(mat, "_MetallicMap"),
                "metallic_channel": int(mat.f("_MetallicMapChannel", 0)),
                "gloss_map": _guid(mat, "_GlossMap"),
                "gloss_channel": int(mat.f("_GlossMapChannel", 3)),
                "reflectance": mat.f("_Reflectance", 0.5),
                "sharpness": mat.f("_SpecularSharpness", 0.0),
            }
        if "USE_OCCLUSION_MAP" in keywords:
            n.occlusion_tex = mat.tex("_OcclusionMap")

        n.extras["shadow"] = {
            "ramp": _guid(mat, "_Ramp"),
            "boost": mat.f("_ShadowBoost", 0.0),
            "albedo": mat.f("_ShadowAlbedo", 0.5),
            "min_brightness": mat.f("_MinBrightness", 0.0),
            "limit_brightness": mat.flag("_LimitBrightness", True),
        }
        if mat.f("_RimIntensity", 0.5) > 0.0:
            n.extras["rim"] = {
                "color": mat.color("_RimColor"),
                "intensity": mat.f("_RimIntensity", 0.5),
                "range": mat.f("_RimRange", 0.3),
                "sharpness": mat.f("_RimSharpness", 0.1),
                "albedo_tint": mat.f("_RimAlbedoTint", 0.0),
            }
        if "USE_MATCAP" in keywords and mat.tex("_Matcap"):
            n.extras["matcap"] = {
                "tex": _guid(mat, "_Matcap"),
                "mask": _guid(mat, "_MatcapMask"),
                "additive": int(mat.f("_MatcapType", 0)) == 0,
                "strength": mat.f("_MatcapStrength", 1.0),
            }
        if "USE_DETAIL_MAPS" in keywords:
            n.extras["detail"] = {
                "albedo": _guid(mat, "_DetailAlbedoMap"),
                "normal": _guid(mat, "_DetailNormalMap") if "USE_NORMAL_MAPS" in keywords else None,
                "mask": _guid(mat, "_DetailMask"),
                "mode": int(mat.f("_DetailMode", 0)),
                "uv": int(mat.f("_DetailUV", 0)),
            }
        if "USE_HUE_SHIFT" in keywords:
            n.extras["hue_shift"] = {
                "albedo": mat.f("_HueShift", 0.0),
                "emission": mat.f("_EmissionHueShift", 0.0),
                "mask": _guid(mat, "_HueShiftMask"),
            }
            n.warnings.append("hue shift is not reproduced")
        if "USE_COLOR_MASK" in keywords and mat.tex("_ColorMask"):
            n.extras["color_mask"] = {
                "tex": _guid(mat, "_ColorMask"),
                "colors": [mat.color(f"_ColorMaskColor{i}") for i in range(1, 5)],
                "emission": [mat.f(f"_ColorMaskEmissionStrength{i}", 0.0) for i in range(1, 5)],
                "multiply": int(mat.f("_ColorMaskBlendMode", 0)) == 0,
            }
            n.warnings.append("color mask tinting is not reproduced")
        if info is not None and info.outline:
            n.extras["outline"] = {
                "color": mat.color("_OutlineColor", default=BLACK),
                "width": mat.f("_OutlineThickness", 0.05),
                "width_mask": _guid(mat, "_OutlineMask"),
                "from_albedo": mat.f("_OutlineFromAlbedo", 0.0),
            }

    def _diffuse(self, mat: UnityMaterial, info: ShaderInfo | None, n: NormalizedMaterial) -> None:
        """VRChat/Mobile/Diffuse, Lightmapped: _MainTex だけのライティング付きシェーダー。"""
        n.lighting = "pbr"

    def _bumped_diffuse(self, mat: UnityMaterial, info: ShaderInfo | None, n: NormalizedMaterial) -> None:
        """VRChat/Mobile/Bumped Diffuse: Diffuse + _BumpMap（強度指定無し）。"""
        self._diffuse(mat, info, n)
        if mat.tex("_BumpMap"):
            n.normal_tex = mat.tex("_BumpMap")
            n.normal_strength = 1.0

    def _bumped_specular(self, mat: UnityMaterial, info: ShaderInfo | None, n: NormalizedMaterial) -> None:
        """VRChat/Mobile/Bumped Mapped Specular: Bumped Diffuse + _Shininess / _SpecColor（グロスは _MainTex の A）。"""
        self._bumped_diffuse(mat, info, n)
        shininess = max(0.0, min(1.0, mat.f("_Shininess", 0.078125)))
        n.roughness = 1.0 - shininess
        n.extras["specular"] = {"color": mat.color("_SpecColor"), "shininess": shininess, "gloss_from_main_alpha": True}

    def _matcap_lit(self, mat: UnityMaterial, info: ShaderInfo | None, n: NormalizedMaterial) -> None:
        """VRChat/Mobile/MatCap Lit: _MainTex × _MatCap × ライティング。MatCap は乗算で extras に保存。"""
        n.lighting = "pbr"
        if mat.tex("_MatCap"):
            n.extras["matcap"] = {"tex": _guid(mat, "_MatCap"), "additive": False, "strength": 1.0}


def _guid(mat: UnityMaterial, name: str) -> str | None:
    ref = mat.tex(name)
    return ref.guid if ref is not None else None
