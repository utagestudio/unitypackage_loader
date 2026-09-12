"""VRChat SDK 同梱の Mobile（Quest / Android 向け）シェーダーの変換規則。

これらのシェーダーは機能が少なく、Standard から切り替えたマテリアルには Standard 時代の
_Color や _EmissionColor がそのまま残っていることが多い。そのため各シェーダーが実際に
参照するプロパティだけを読み、それ以外は無視する。バリアントは shader_guids.json の
``variant`` で指定する（GUID 表に無いものはプロパティ指紋で Toon Standard だけ判定できる）。
"""

from __future__ import annotations

from ..material import NormalizedMaterial, UnityMaterial
from .base import ShaderInfo, ShaderProfile, texture_transform


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
