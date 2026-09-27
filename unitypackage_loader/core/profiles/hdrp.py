"""HDRP の Lit / LitTessellation / LayeredLit / LayeredLitTessellation / Unlit と、HDRP/Lit と同じ名前を使うシェーダー。

HDRP のシェーダーは互換用に ``_Color`` / ``_MainTex`` を宣言していて、Standard から変換したマテリアルにはそれらや ``_Mode`` /
``_Glossiness`` が残っている（#118。UnityJapanOffice の LayeredLit で、残った黒い ``_Color`` を読んで面が黒くなった）。
HDRP の名前だけを読み、Standard の残りは見ない。値の意味は HDRP 17.6 の ``Lit.shader`` / ``LitDataIndividualLayer.hlsl`` /
``Unlit.shader`` / ``MaterialBlendModeEnum.cs`` / ``MaterialExtension.cs`` に合わせた（#119）。
"""

from __future__ import annotations

from ..material import BLACK, BlendMode, NormalizedMaterial, UnityMaterial
from .base import ShaderInfo, ShaderProfile, cull_backface, texture_transform

# _SurfaceType
SURFACE_OPAQUE, SURFACE_TRANSPARENT = 0, 1
# _BlendMode（MaterialBlendModeEnum.BlendMode）
_BLEND_MODES: dict[int, BlendMode] = {0: "alpha", 1: "additive", 4: "premultiply"}
# _MaterialID（MaterialExtension.MaterialId）
_MATERIAL_TYPES = {0: "subsurface_scattering", 1: "standard", 2: "anisotropy", 3: "iridescence", 4: "specular_color", 5: "translucent"}
MATERIAL_SPECULAR_COLOR = 4
# シェーダー表の名前 → 読み方。表に無いもの（指紋で選んだ HDRP/Lit と同じ名前を使うシェーダー）は Lit として読む
_KINDS = {
    "HDRP/Lit": "lit",
    "HDRP/LitTessellation": "lit",
    "HDRP/LayeredLit": "layered",
    "HDRP/LayeredLitTessellation": "layered",
    "HDRP/Unlit": "unlit",
}


class HdrpLitProfile(ShaderProfile):
    family = "hdrp"
    lighting = "pbr"

    def matches(self, mat: UnityMaterial) -> bool:
        # HDRP にしか無い _SurfaceType と、HDRP/Lit の名前のベース画像。_MaskMap だけでは決めない（Shader Graph が
        # 自分の名前として _MaskMap を持ち、ベース画像は _MainTex で読むことがある。UnityJapanOffice の BaseMapTiling など）
        return mat.has("_SurfaceType") and mat.has("_BaseColorMap")

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        n = self._base(mat, info)
        kind = _KINDS.get(info.name, "lit") if info is not None else "lit"
        n.cull_backface = not mat.flag("_DoubleSidedEnable") if mat.has("_DoubleSidedEnable") else cull_backface(mat, default=True)
        _alpha(mat, n)
        hdrp_emission(mat, n)
        if kind == "unlit":
            n.base_color_tex = mat.tex("_UnlitColorMap")
            n.base_color = mat.color("_UnlitColor")
            n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)
            return n
        # LayeredLit はレイヤーごとの名前（_BaseColor0 など）を持つ。レイヤーを重ねる処理は再現せず、レイヤー 0 を読む
        layer = "0" if kind == "layered" else ""
        _surface(mat, n, layer)
        if kind == "layered":
            n.extras["hdrp"] = {**n.extras.get("hdrp", {}), "layer_count": int(mat.f("_LayerCount", 2))}
        else:
            _material_type(mat, n)
        _extras(mat, n, layer)
        return n


def _alpha(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """``_SurfaceType`` / ``_AlphaCutoffEnable`` / ``_AlphaCutoff`` / ``_BlendMode``。"""
    transparent = int(mat.f("_SurfaceType", SURFACE_OPAQUE)) == SURFACE_TRANSPARENT
    if mat.flag("_AlphaCutoffEnable"):
        n.alpha_mode = "cutout"
    elif transparent:
        n.alpha_mode = "blend"
    else:
        n.alpha_mode = "opaque"
    n.alpha_cutoff = mat.f("_AlphaCutoff", 0.5)
    n.alpha_from_texture = n.alpha_mode != "opaque"
    if transparent:
        n.blend_mode = _BLEND_MODES.get(int(mat.f("_BlendMode", 0)), "alpha")
        if n.blend_mode == "additive":
            n.warnings.append("HDRP blend mode 'additive' is approximated as alpha blending")


def _surface(mat: UnityMaterial, n: NormalizedMaterial, layer: str) -> None:
    """色・ノーマル・Metallic・Smoothness（``LitDataIndividualLayer.hlsl``）。``layer`` は LayeredLit のレイヤーの番号。

    マスクマップ（``_MaskMap``。R = Metallic、G = AO、B = Detail マスク、A = Smoothness）があれば、Metallic と Smoothness は
    ``_MetallicRemapMin``〜``Max`` / ``_SmoothnessRemapMin``〜``Max`` をマップの値で補間したもので、``_Metallic`` / ``_Smoothness`` は
    使わない。無ければ ``_Metallic`` / ``_Smoothness``。
    """
    n.base_color_tex = mat.tex(f"_BaseColorMap{layer}")
    n.base_color = mat.color(f"_BaseColor{layer}")
    n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)
    n.normal_tex = mat.tex(f"_NormalMap{layer}")
    if n.normal_tex is not None:
        n.normal_strength = mat.f(f"_NormalScale{layer}", 1.0)
    n.metallic = mat.f(f"_Metallic{layer}", 0.0)
    smoothness = mat.f(f"_Smoothness{layer}", 0.5)
    n.roughness = max(0.0, min(1.0, 1.0 - smoothness))
    mask = mat.tex(f"_MaskMap{layer}")
    if mask is not None:
        n.metallic_tex = mask
        n.metallic_remap = (mat.f(f"_MetallicRemapMin{layer}", 0.0), mat.f(f"_MetallicRemapMax{layer}", 1.0))
        low, high = mat.f(f"_SmoothnessRemapMin{layer}", 0.0), mat.f(f"_SmoothnessRemapMax{layer}", 1.0)
        n.smoothness_offset, n.smoothness_scale = low, high - low


def _material_type(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """``_MaterialID``。Specular Color は F0 = ``_SpecularColor``（× ``_SpecularColorMap``）の非金属として読む。

    Subsurface Scattering / Anisotropy / Iridescence / Translucent は Standard として組み、種類を extras に残す。
    """
    material_id = int(mat.f("_MaterialID", 1))
    if material_id == MATERIAL_SPECULAR_COLOR:
        n.metallic, n.metallic_remap = 0.0, (0.0, 0.0)
        color = mat.color("_SpecularColor")
        n.specular_color = (color[0], color[1], color[2], 1.0)
        n.specular_tex = mat.tex("_SpecularColorMap")
    if material_id != 1:
        n.extras["hdrp"] = {**n.extras.get("hdrp", {}), "material_type": _MATERIAL_TYPES.get(material_id, str(material_id))}


def _extras(mat: UnityMaterial, n: NormalizedMaterial, layer: str) -> None:
    """組み立てには使わない HDRP の設定（``unity_props`` に残す）。"""
    hdrp: dict[str, object] = dict(n.extras.get("hdrp", {}))
    if n.alpha_mode == "blend":
        hdrp["blend"] = n.blend_mode
    if mat.tex(f"_MaskMap{layer}") is not None:
        hdrp["ao_remap"] = [mat.f(f"_AORemapMin{layer}", 0.0), mat.f(f"_AORemapMax{layer}", 1.0)]
    for prop, key in (("_DetailMap", "detail"), ("_HeightMap", "height"), ("_CoatMaskMap", "coat_map")):
        if mat.tex(f"{prop}{layer}") is not None:
            hdrp[key] = True
    if mat.f("_CoatMask", 0.0) > 0.0:
        hdrp["coat_mask"] = mat.f("_CoatMask", 0.0)
    if hdrp:
        n.extras["hdrp"] = hdrp


def hdrp_emission(mat: UnityMaterial, n: NormalizedMaterial) -> None:
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
