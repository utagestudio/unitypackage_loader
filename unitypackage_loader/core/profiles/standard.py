"""Unity Standard / URP Lit / HDRP Lit、および未知シェーダー向けの一般規則。"""

from __future__ import annotations

from ..material import BLACK, NormalizedMaterial, UnityMaterial
from .base import ShaderInfo, ShaderProfile, alpha_mode_from_blend_state, cull_backface, is_black, texture_transform

# Standard シェーダーの _Mode
MODE_OPAQUE, MODE_CUTOUT, MODE_FADE, MODE_TRANSPARENT = 0, 1, 2, 3

_TOON_HINTS = ("_ShadeTexture", "_ShadeColor", "_1st_ShadeMap", "_ShadowColor", "_ShadeMap", "_SssTex")
# _EMISSION キーワードで発光を切り替えるシェーダーの系統（Built-in の Standard、URP Lit）。
# Poiyomi（_EnableEmission）や判定できないシェーダーには、キーワードの有無の条件を当てない
_EMISSION_KEYWORD_FAMILIES = ("standard", "urp")


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
            elif n.family in _EMISSION_KEYWORD_FAMILIES and mat.keywords_known and "_EMISSION" not in mat.keywords:
                # Standard / URP Lit は _EMISSION キーワードが有効なときだけ光る。発光を切っても _EmissionColor は残るので、
                # キーワードの記述があるのに _EMISSION が無ければ光らせない（#52: 白い発光色が残った電柱が真っ白になった）
                pass
            elif emission_tex is not None or not is_black(emission_color):
                n.emission_tex = emission_tex
                n.emission_color = emission_color

        n.metallic = mat.f("_Metallic", 0.0)
        n.metallic_tex = mat.tex("_MetallicGlossMap")
        _smoothness(mat, n, info)
        n.occlusion_tex = mat.tex("_OcclusionMap")
        n.cull_backface = cull_backface(mat, default=True)

        n.alpha_mode = self._alpha_mode(mat, info, _is_urp(mat, n, info))
        n.alpha_cutoff = mat.f("_Cutoff", 0.5)
        n.alpha_from_texture = n.alpha_mode != "opaque"
        return n

    @staticmethod
    def _alpha_mode(mat: UnityMaterial, info: ShaderInfo | None, urp: bool = False):
        # URP から変換したマテリアルには Standard の _Mode が残っていることがあるので、URP では _Surface を先に見る
        # （#112: 半透明＋アルファクリップの横断歩道のデカールが、残った _Mode: 0 で不透明になっていた）
        if urp and mat.has("_Surface"):
            if mat.flag("_AlphaClip"):
                return "cutout"
            return "blend" if int(mat.f("_Surface", 0)) == 1 else "opaque"
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


def _is_urp(mat: UnityMaterial, n: NormalizedMaterial, info: ShaderInfo | None) -> bool:
    """URP Lit か、URP Lit から派生したシェーダーか（Standard の残りのプロパティより URP のものを優先して読む）。

    シェーダー表で系統が分かればそれに従う。表に無いシェーダー（プロパティの指紋で選んだもの）は、URP Lit にしか無い
    ``_WorkflowMode`` / ``_Surface`` の有無で決める。
    """
    if info is not None:
        return n.family == "urp"
    return mat.has("_WorkflowMode", "_Surface")


def _smoothness(mat: UnityMaterial, n: NormalizedMaterial, info: ShaderInfo | None) -> None:
    """Smoothness（Blender では 1 − Roughness）。

    URP Lit の Smoothness は ``_Smoothness``、Built-in Standard は ``_Glossiness``。マップ（``_MetallicGlossMap``）か
    アルベドの A（``_SmoothnessTextureChannel`` = 1）から取るときは、その A に倍率を掛ける。倍率は URP が ``_Smoothness``、
    Standard が ``_GlossMapScale``（#112: 倍率を掛けず、艶の無い面が鏡面になっていた）。URP から変換したマテリアルには
    Standard の ``_Glossiness`` / ``_GlossMapScale`` が残っていることがあるので、URP ではそれらを見ない。
    """
    if _is_urp(mat, n, info):
        smoothness = mat.f("_Smoothness", 0.5)
        n.smoothness_scale = smoothness
    else:
        smoothness = mat.f("_Glossiness", mat.f("_Smoothness", 0.5))
        n.smoothness_scale = mat.f("_GlossMapScale", 1.0)
    n.roughness = max(0.0, min(1.0, 1.0 - smoothness))
    n.smoothness_scale = max(0.0, min(1.0, n.smoothness_scale))
    n.smoothness_from_albedo = int(mat.f("_SmoothnessTextureChannel", 0.0)) == 1 and n.base_color_tex is not None


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
