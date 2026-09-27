"""Unity Built-in Standard、および未知シェーダー向けの一般規則（URP は urp.py、HDRP は hdrp.py）。"""

from __future__ import annotations

from ..material import BLACK, NormalizedMaterial, UnityMaterial
from .base import ShaderInfo, ShaderProfile, alpha_mode_from_blend_state, cull_backface, is_black, texture_transform
from .hdrp import hdrp_emission

# Standard シェーダーの _Mode
MODE_OPAQUE, MODE_CUTOUT, MODE_FADE, MODE_TRANSPARENT = 0, 1, 2, 3

_TOON_HINTS = ("_ShadeTexture", "_ShadeColor", "_1st_ShadeMap", "_ShadowColor", "_ShadeMap", "_SssTex")
# _EMISSION キーワードで発光を切り替えるシェーダーの系統（Built-in の Standard、URP Lit）。
# Poiyomi（_EnableEmission）や判定できないシェーダーには、キーワードの有無の条件を当てない
_EMISSION_KEYWORD_FAMILIES = ("standard", "urp")


class StandardProfile(ShaderProfile):
    family = "standard"
    aliases = ("legacy",)
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
            hdrp_emission(mat, n)  # HDRP 向けの Shader Graph など
        else:
            read_emission(mat, n)

        n.metallic = mat.f("_Metallic", 0.0)
        n.metallic_tex = mat.tex("_MetallicGlossMap")
        _smoothness(mat, n)
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


def read_emission(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """``_EmissionColor`` / ``_EmissionMap`` の発光（Built-in Standard、URP Lit と、同じ名前を使うシェーダー）。"""
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


def _smoothness(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """Smoothness（Blender では 1 − Roughness）。

    Built-in Standard の Smoothness は ``_Glossiness``。マップ（``_MetallicGlossMap``）かアルベドの A
    （``_SmoothnessTextureChannel`` = 1）から取るときは、その A に ``_GlossMapScale`` を掛ける
    （#112: 倍率を掛けず、艶の無い面が鏡面になっていた）。URP Lit は urp.py。
    """
    smoothness = mat.f("_Glossiness", mat.f("_Smoothness", 0.5))
    n.smoothness_scale = mat.f("_GlossMapScale", 1.0)
    n.roughness = max(0.0, min(1.0, 1.0 - smoothness))
    n.smoothness_scale = max(0.0, min(1.0, n.smoothness_scale))
    n.smoothness_from_albedo = int(mat.f("_SmoothnessTextureChannel", 0.0)) == 1 and n.base_color_tex is not None


# 一般名（_MainTex など）を持たない自作シェーダー（Amplify Shader Editor など）でよく使われる名前。
# 大文字小文字と "_" を無視して完全一致で比べる（_Mask や _Color_Tilling のような別の役割の名前を拾わないよう、部分一致にはしない）。
# #118 で宣言されていない残りの _MainTex を読まなくなり、独自の名前でしか色を持たないシェーダーが白くなった
_BASE_COLOR_ALIASES = (
    "basecolor", "basecolormap", "basecolortex", "basecolortexture", "basetex", "basetexture",
    "albedo", "albedomap", "albedotex", "albedotexture", "diffuse", "diffusemap", "diffusetex", "diffusetexture",
    "maintexture", "colormap", "colortex", "colortexture",
)
_NORMAL_ALIASES = ("normal", "normals", "normaltex", "normaltexture", "bump", "bumptex", "bumptexture")
_NORMAL_SCALE_ALIASES = (
    "normalscale", "scalenormal", "normalstrength", "normalintensity", "normalpower", "bumpstrength", "bumppower",
)


def _alias_key(name: str) -> str:
    return name.replace("_", "").lower()


def _find_texture(mat: UnityMaterial, aliases: tuple[str, ...]):
    """``aliases`` の順に、値の入ったテクスチャを探す。"""
    by_key = {_alias_key(name): ref for name, ref in mat.textures.items()}
    for alias in aliases:
        if by_key.get(alias) is not None:
            return by_key[alias]
    return None


def _find_float(mat: UnityMaterial, aliases: tuple[str, ...], default: float) -> float:
    names = {_alias_key(name): name for name in (*mat.floats, *mat.ints)}
    for alias in aliases:
        if alias in names:
            return mat.f(names[alias], default)
    return default


class GenericProfile(StandardProfile):
    """どのプロファイルにも一致しないシェーダー向け。一般的なプロパティ名だけで最善努力する。

    一般名が無ければ、自作シェーダーでよく使われる名前（``_Base_Color`` / ``_Albedo`` / ``_Normal`` など）で
    ベースカラーと法線のテクスチャを探す。
    """

    family = "unknown"
    aliases = ()

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        n = super().normalize(mat, info)
        if n.base_color_tex is None:
            n.base_color_tex = _find_texture(mat, _BASE_COLOR_ALIASES)
            n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)
        if n.normal_tex is None:
            n.normal_tex = _find_texture(mat, _NORMAL_ALIASES)
            if n.normal_tex is not None:
                n.normal_strength = _find_float(mat, _NORMAL_SCALE_ALIASES, 1.0)
        if info is None:
            n.family = "unknown"
            n.warnings.append("unknown shader; used generic property mapping")
        if mat.has(*_TOON_HINTS) and (info is None or info.lighting is None):
            n.lighting = "toon"
        if n.base_color_tex is None and not mat.textures:
            n.warnings.append("material has no textures")
        return n
