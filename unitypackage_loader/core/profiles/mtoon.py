"""VRM MToon（0.x / MToon10）の変換規則。"""

from __future__ import annotations

from ..material import BLACK, NormalizedMaterial, UnityMaterial
from .base import CULL_BACK, ShaderInfo, ShaderProfile, alpha_mode_from_blend_state, texture_transform

# MToon 0.x の _BlendMode
BLEND_OPAQUE, BLEND_CUTOUT, BLEND_TRANSPARENT, BLEND_TRANSPARENT_ZWRITE = 0, 1, 2, 3


class MToonProfile(ShaderProfile):
    family = "mtoon"
    lighting = "toon"

    def matches(self, mat: UnityMaterial) -> bool:
        if mat.has("_MToonVersion"):
            return True
        legacy = mat.has("_ShadeTexture") and mat.has("_ShadeColor") and mat.has("_BlendMode")
        mtoon10 = mat.has("_ShadeTex") and mat.has("_AlphaMode")
        return legacy or mtoon10

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        n = self._base(mat, info)
        is_mtoon10 = mat.has("_AlphaMode") or mat.has("_ShadeTex")

        n.base_color_tex = mat.tex("_MainTex")
        n.base_color = mat.color("_Color")
        n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)

        if mat.tex("_BumpMap"):
            n.normal_tex = mat.tex("_BumpMap")
            n.normal_strength = mat.f("_BumpScale", 1.0)

        emission_color = mat.color("_EmissionColor", default=BLACK)
        if mat.tex("_EmissionMap") or any(c > 0 for c in emission_color[:3]):
            n.emission_tex = mat.tex("_EmissionMap")
            n.emission_color = emission_color

        n.metallic, n.roughness = 0.0, 1.0

        if is_mtoon10:
            mode = int(mat.f("_AlphaMode", 0))
            n.alpha_mode = ("opaque", "cutout", "blend")[mode] if 0 <= mode <= 2 else "opaque"
            if mat.has("_DoubleSided"):
                n.cull_backface = not mat.flag("_DoubleSided")
            else:
                n.cull_backface = int(mat.f("_M_CullMode", mat.f("_CullMode", CULL_BACK))) == CULL_BACK
        else:
            mode = int(mat.f("_BlendMode", -1))
            if mode == BLEND_CUTOUT:
                n.alpha_mode = "cutout"
            elif mode in (BLEND_TRANSPARENT, BLEND_TRANSPARENT_ZWRITE):
                n.alpha_mode = "blend"
            elif mode == BLEND_OPAQUE:
                n.alpha_mode = "opaque"
            else:
                n.alpha_mode = alpha_mode_from_blend_state(mat)
            n.cull_backface = int(mat.f("_CullMode", CULL_BACK)) == CULL_BACK
        n.alpha_cutoff = mat.f("_Cutoff", 0.5)
        n.alpha_from_texture = n.alpha_mode != "opaque"

        shade_tex = mat.tex("_ShadeTex", "_ShadeTexture")
        n.extras["shade"] = {
            "color": mat.color("_ShadeColor"),
            "tex": shade_tex.guid if shade_tex else None,
            "shift": mat.f("_ShadingShiftFactor", mat.f("_ShadeShift", 0.0)),
            "toony": mat.f("_ShadingToonyFactor", mat.f("_ShadeToony", 0.9)),
        }
        matcap = mat.tex("_MatcapTex", "_SphereAdd")
        if matcap:
            n.extras["matcap"] = {"tex": matcap.guid, "color": mat.color("_MatcapColor"), "additive": True}
        rim_tex = mat.tex("_RimTex", "_RimTexture")
        n.extras["rim"] = {"color": mat.color("_RimColor", default=BLACK), "tex": rim_tex.guid if rim_tex else None}
        if int(mat.f("_OutlineWidthMode", 0)) != 0:
            n.extras["outline"] = {
                "color": mat.color("_OutlineColor", default=BLACK),
                "width": mat.f("_OutlineWidth", 0.0),
                "mode": int(mat.f("_OutlineWidthMode", 0)),
            }
        return n
