"""Poiyomi Toon の変換規則。バージョンごとに GUID が違うためプロパティ指紋で判定する。"""

from __future__ import annotations

from ..material import BLACK, NormalizedMaterial, UnityMaterial
from .base import ShaderInfo
from .standard import StandardProfile

# Poiyomi の _Mode（Standard と同じ並びに Additive 等が続く）
MODE_OPAQUE, MODE_CUTOUT, MODE_FADE, MODE_TRANSPARENT = 0, 1, 2, 3


class PoiyomiProfile(StandardProfile):
    family = "poiyomi"
    aliases = ()
    lighting = "toon"

    def matches(self, mat: UnityMaterial) -> bool:
        if mat.has("_PoiVersion", "_PoiyomiVersion"):
            return True
        return mat.has("_ShaderOptimizerEnabled") and mat.has("_MainTex") and mat.has("_ShadingEnabled", "_LightingMode", "_LightingColorMode")

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        n = super().normalize(mat, info)
        n.family = "poiyomi"
        n.lighting = "toon"

        # エミッションは _EnableEmission でゲートされ、_EmissionStrength が強度
        if mat.has("_EnableEmission") and not mat.flag("_EnableEmission"):
            n.emission_tex, n.emission_color = None, BLACK
        elif n.has_emission:
            n.emission_strength = max(mat.f("_EmissionStrength", 1.0), 0.0)

        # Metallic/Smoothness は _MochieMetallicMaps / 反射が有効なときだけ意味がある
        if not mat.flag("_MochieMetallicMaps", mat.flag("_ReflectionsEnabled", False)) and n.metallic_tex is None:
            n.metallic, n.roughness = 0.0, 1.0

        mode = int(mat.f("_Mode", -1))
        if mode >= 4:  # Additive / Soft Additive / Multiply など
            n.alpha_mode = "blend"
            n.alpha_from_texture = True
            n.warnings.append(f"Poiyomi blend mode {mode} approximated as alpha blend")

        n.extras["shading"] = {
            "mode": int(mat.f("_LightingMode", 0)),
            "shadow_strength": mat.f("_ShadowStrength", 1.0),
            "shadow_offset": mat.f("_ShadowOffset", 0.0),
        }
        if mat.flag("_EnableOutlines"):
            n.extras["outline"] = {"color": mat.color("_LineColor"), "width": mat.f("_LineWidth", 0.0)}
        return n
