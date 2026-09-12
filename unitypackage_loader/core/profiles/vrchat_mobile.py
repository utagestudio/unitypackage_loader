"""VRChat SDK 同梱の Mobile（Quest / Android 向け）シェーダーの変換規則。

これらのシェーダーは機能が少なく、Standard から切り替えたマテリアルには Standard 時代の
_Color や _EmissionColor がそのまま残っていることが多い。そのため各シェーダーが実際に
参照するプロパティだけを読み、それ以外は無視する。バリアントは shader_guids.json の
``variant`` で指定する（GUID 表に無いものはプロパティ指紋で Toon Standard だけ判定できる）。
"""

from __future__ import annotations

from ..material import BLACK, NormalizedMaterial, UnityMaterial
from .base import ShaderInfo, ShaderProfile, is_black, texture_transform


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
