"""lilToon 系シェーダーの変換規則。"""

from __future__ import annotations

from ..material import BLACK, NormalizedMaterial, UnityMaterial, toon_values_from_extras
from .base import ShaderInfo, ShaderProfile, alpha_mode_from_blend_state, cull_backface, is_black, texture_transform


class LilToonProfile(ShaderProfile):
    family = "liltoon"
    lighting = "toon"

    def matches(self, mat: UnityMaterial) -> bool:
        if mat.has("_lilToonVersion"):
            return True
        return mat.has("_UseShadow", "_ShadowStrength") and mat.has("_MainTex", "_BaseMap") and mat.has("_MatCapTex", "_UseMatCap")

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        n = self._base(mat, info)

        n.base_color_tex = mat.tex("_MainTex", "_BaseMap", "_BaseColorMap")
        n.base_color = mat.color("_Color", "_BaseColor")
        n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)

        # 法線: lilToon は _UseBumpMap で ON/OFF
        if mat.flag("_UseBumpMap") and mat.tex("_BumpMap"):
            n.normal_tex = mat.tex("_BumpMap")
            n.normal_strength = mat.f("_BumpScale", 1.0)

        # エミッション
        if mat.flag("_UseEmission"):
            n.emission_tex = mat.tex("_EmissionMap")
            n.emission_color = mat.color("_EmissionColor", default=BLACK)
            n.emission_strength = max(mat.f("_EmissionBlend", 1.0), 0.0)
            if n.emission_tex is None and is_black(n.emission_color):
                n.emission_color = BLACK

        # 反射（PBR 的パラメータ）は _UseReflection が ON のときだけ意味を持つ
        if mat.flag("_UseReflection"):
            n.metallic = mat.f("_Metallic", 0.0)
            n.roughness = 1.0 - mat.f("_Smoothness", 0.0)
            n.metallic_tex = mat.tex("_MetallicGlossMap")
        else:
            n.metallic = 0.0
            n.roughness = 1.0

        n.cull_backface = cull_backface(mat, default=True)

        # アルファ: シェーダーのバリアント名が最優先、無ければブレンド状態から推定
        if info is not None and info.alpha is not None:
            n.alpha_mode = info.alpha
        else:
            n.alpha_mode = alpha_mode_from_blend_state(mat, default="opaque")
        n.alpha_cutoff = mat.f("_Cutoff", 0.5)
        n.alpha_from_texture = n.alpha_mode != "opaque"

        # Blender 側で 1:1 に再現できないが後で参考にできる値
        extras = n.extras
        extras["lilToonVersion"] = mat.f("_lilToonVersion", 0)
        if mat.flag("_UseShadow", True):
            extras["shadow"] = {
                "color": mat.color("_ShadowColor"),
                "color2nd": mat.color("_Shadow2ndColor"),
                "strength": mat.f("_ShadowStrength", 1.0),
                "border": mat.f("_ShadowBorder", 0.5),
                "blur": mat.f("_ShadowBlur", 0.1),
            }
        if (info is not None and info.outline) or mat.flag("_UseOutline"):
            extras["outline"] = {
                "color": mat.color("_OutlineColor"),
                "width": mat.f("_OutlineWidth", 0.08),
                "tex": mat.tex("_OutlineTex").guid if mat.tex("_OutlineTex") else None,
                "width_mask": mat.tex("_OutlineWidthMask").guid if mat.tex("_OutlineWidthMask") else None,
            }
        if mat.flag("_UseMatCap") and mat.tex("_MatCapTex"):
            extras["matcap"] = {
                "tex": mat.tex("_MatCapTex").guid,
                "color": mat.color("_MatCapColor"),
                "blend_mode": int(mat.f("_MatCapBlendMode", 1)),
                "blend": mat.f("_MatCapBlend", 1.0),
            }
        if mat.flag("_UseMatCap2nd") and mat.tex("_MatCap2ndTex"):
            extras["matcap2nd"] = {
                "tex": mat.tex("_MatCap2ndTex").guid,
                "color": mat.color("_MatCap2ndColor"),
                "blend_mode": int(mat.f("_MatCap2ndBlendMode", 1)),
                "blend": mat.f("_MatCap2ndBlend", 1.0),
            }
        if mat.flag("_UseRim"):
            extras["rim"] = {"color": mat.color("_RimColor"), "border": mat.f("_RimBorder", 0.5)}
        if mat.tex("_AlphaMask"):
            extras["alpha_mask"] = {"tex": mat.tex("_AlphaMask").guid, "mode": int(mat.f("_AlphaMaskMode", 0))}
        hsvg = mat.color("_MainTexHSVG", default=(0.0, 1.0, 1.0, 1.0))
        if hsvg != (0.0, 1.0, 1.0, 1.0):
            extras["main_tex_hsvg"] = hsvg
            n.warnings.append("_MainTexHSVG adjustment is not reproduced")
        n.shadow, n.matcap, n.rim = toon_values_from_extras(extras)  # 1.7.4 までと同じ換算（#73）
        return n
