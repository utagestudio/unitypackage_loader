"""Unity Standard / URP Lit / HDRP Lit、および未知シェーダー向けの一般規則。"""

from __future__ import annotations

from ..material import BLACK, NormalizedMaterial, UnityMaterial
from .base import ShaderInfo, ShaderProfile, alpha_mode_from_blend_state, cull_backface, is_black, texture_transform

# Standard シェーダーの _Mode
MODE_OPAQUE, MODE_CUTOUT, MODE_FADE, MODE_TRANSPARENT = 0, 1, 2, 3

_TOON_HINTS = ("_ShadeTexture", "_ShadeColor", "_1st_ShadeMap", "_ShadowColor", "_ShadeMap", "_SssTex")


class StandardProfile(ShaderProfile):
    family = "standard"
    aliases = ("urp", "hdrp", "legacy")
    lighting = "pbr"

    def matches(self, mat: UnityMaterial) -> bool:
        return mat.has("_Glossiness", "_Smoothness") and mat.has("_Metallic") and mat.has("_MainTex", "_BaseMap") and not mat.has(*_TOON_HINTS)

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        n = self._base(mat, info)
        n.base_color_tex = mat.tex("_MainTex", "_BaseMap", "_BaseColorMap")
        n.base_color = mat.color("_Color", "_BaseColor")
        n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)

        if mat.tex("_BumpMap", "_NormalMap"):
            n.normal_tex = mat.tex("_BumpMap", "_NormalMap")
            n.normal_strength = mat.f("_BumpScale", mat.f("_NormalScale", 1.0))

        if mat.has("_EmissiveColor"):
            _hdrp_emission(mat, n)
        else:
            # _EMISSION が m_InvalidKeywords にある = 現在のシェーダーに emission が無い。
            # 以前のシェーダーの _EmissionColor が残っていても光らせない
            emission_color = mat.color("_EmissionColor", default=BLACK)
            emission_tex = mat.tex("_EmissionMap")
            if "_EMISSION" in mat.invalid_keywords:
                if emission_tex is not None or not is_black(emission_color):
                    n.warnings.append("_EMISSION keyword is invalid for this shader; emission ignored")
            elif emission_tex is not None or not is_black(emission_color):
                n.emission_tex = emission_tex
                n.emission_color = emission_color

        n.metallic = mat.f("_Metallic", 0.0)
        smoothness = mat.f("_Glossiness", mat.f("_Smoothness", 0.5))
        n.roughness = max(0.0, min(1.0, 1.0 - smoothness))
        n.metallic_tex = mat.tex("_MetallicGlossMap")
        n.occlusion_tex = mat.tex("_OcclusionMap")
        n.cull_backface = cull_backface(mat, default=True)

        n.alpha_mode = self._alpha_mode(mat, info)
        n.alpha_cutoff = mat.f("_Cutoff", 0.5)
        n.alpha_from_texture = n.alpha_mode != "opaque"
        return n

    @staticmethod
    def _alpha_mode(mat: UnityMaterial, info: ShaderInfo | None):
        if mat.has("_Mode"):  # Built-in Standard
            mode = int(mat.f("_Mode", 0))
            if mode == MODE_CUTOUT:
                return "cutout"
            if mode in (MODE_FADE, MODE_TRANSPARENT):
                return "blend"
            if mode == MODE_OPAQUE:
                return "opaque"
        if mat.has("_Surface"):  # URP Lit
            if mat.flag("_AlphaClip"):
                return "cutout"
            return "blend" if int(mat.f("_Surface", 0)) == 1 else "opaque"
        if mat.has("_SurfaceType"):  # HDRP Lit
            if mat.flag("_AlphaCutoffEnable"):
                return "cutout"
            return "blend" if int(mat.f("_SurfaceType", 0)) == 1 else "opaque"
        if info is not None and info.alpha is not None:
            return info.alpha
        return alpha_mode_from_blend_state(mat, default="opaque")


def _hdrp_emission(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """HDRP（HDRP/Lit と HDRP 向け Shader Graph）の発光。

    HDRP の発光は ``_EmissiveColor``（線形の HDR 色。``_UseEmissiveIntensity`` なら ``_EmissiveColorLDR`` × 強度）と
    ``_EmissiveColorMap`` で決まり、色が黒なら光らない。HDRP のマテリアルは発光しなくても ``_EmissionColor`` を
    白で持っている（ベイク向けの互換用）ので、そちらは使わない。

    強度は物理単位（nits / EV100）で Blender の Emission Strength とは対応しないため、色は最大成分で割った色味だけを使い、
    強さは 1.0 にする。元の値は ``extras["hdrp_emissive"]`` に残す。
    """
    color = tuple(max(0.0, c) for c in mat.color("_EmissiveColor", default=BLACK)[:3])
    peak = max(color)
    if not 0.0 < peak < float("inf"):
        return
    n.emission_color = (color[0] / peak, color[1] / peak, color[2] / peak, 1.0)
    n.emission_tex = mat.tex("_EmissiveColorMap")
    n.emission_strength = 1.0
    n.extras["hdrp_emissive"] = {
        "color": list(color),
        "intensity": mat.f("_EmissiveIntensity", 1.0),
        "unit": "ev100" if int(mat.f("_EmissiveIntensityUnit", 0.0)) == 1 else "nits",
        "use_intensity": mat.flag("_UseEmissiveIntensity"),
    }


class GenericProfile(StandardProfile):
    """どのプロファイルにも一致しないシェーダー向け。一般的なプロパティ名だけで最善努力する。"""

    family = "unknown"
    aliases = ()

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        n = super().normalize(mat, info)
        if info is None:
            n.family = "unknown"
            n.warnings.append("unknown shader; used generic property mapping")
        if mat.has(*_TOON_HINTS) and (info is None or info.lighting is None):
            n.lighting = "toon"
        if n.base_color_tex is None and not mat.textures:
            n.warnings.append("material has no textures")
        return n
