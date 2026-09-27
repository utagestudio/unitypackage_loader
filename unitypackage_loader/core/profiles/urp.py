"""URP Lit（Universal Render Pipeline/Lit）と、URP Lit から派生したシェーダー。

URP Lit の ``Properties`` では ``_MainTex`` / ``_Color`` / ``_GlossMapScale`` / ``_Glossiness`` は互換用
（ObsoleteProperties）で、Standard から変換したマテリアルにはそれらや ``_Mode`` が残っていることがある。
URP の名前だけを読み、Standard の残りは見ない（#112 / #116）。
"""

from __future__ import annotations

from ..material import BlendMode, NormalizedMaterial, UnityMaterial
from .base import ShaderInfo, ShaderProfile, cull_backface, texture_transform
from .standard import read_emission

# _WorkflowMode
WORKFLOW_SPECULAR, WORKFLOW_METALLIC = 0, 1
# _Surface
SURFACE_OPAQUE, SURFACE_TRANSPARENT = 0, 1
# _Blend（BaseShaderGUI.BlendMode）
_BLEND_MODES: dict[int, BlendMode] = {0: "alpha", 1: "premultiply", 2: "additive", 3: "multiply"}


class UrpLitProfile(ShaderProfile):
    family = "urp"
    lighting = "pbr"

    def matches(self, mat: UnityMaterial) -> bool:
        # URP Lit にしか無い _WorkflowMode / _Surface（#112 で URP として読むことにした条件と同じ）
        return mat.has("_WorkflowMode", "_Surface")

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        n = self._base(mat, info)
        # _BaseMap / _BaseColor を持たない（古い）マテリアルだけ、互換用の _MainTex / _Color を読む
        n.base_color_tex = mat.tex("_BaseMap" if mat.has("_BaseMap") else "_MainTex")
        n.base_color = mat.color("_BaseColor" if mat.has("_BaseColor") else "_Color")
        n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)

        n.normal_tex = mat.tex("_BumpMap")
        if n.normal_tex is not None:
            n.normal_strength = mat.f("_BumpScale", 1.0)

        read_emission(mat, n)
        n.occlusion_tex = mat.tex("_OcclusionMap")
        n.cull_backface = cull_backface(mat, default=True)
        _alpha(mat, n)
        _surface_inputs(mat, n)
        _extras(mat, n)
        return n


def _alpha(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """``_Surface`` / ``_AlphaClip`` / ``_Blend``。半透明でもアルファクリップが有効なら cutout として読む（#112）。"""
    transparent = int(mat.f("_Surface", SURFACE_OPAQUE)) == SURFACE_TRANSPARENT
    if mat.flag("_AlphaClip"):
        n.alpha_mode = "cutout"
    elif transparent:
        n.alpha_mode = "blend"
    else:
        n.alpha_mode = "opaque"
    n.alpha_cutoff = mat.f("_Cutoff", 0.5)
    n.alpha_from_texture = n.alpha_mode != "opaque"
    if transparent:
        n.blend_mode = _BLEND_MODES.get(int(mat.f("_Blend", 0)), "alpha")
        if n.blend_mode in ("additive", "multiply"):
            n.warnings.append(f"URP blend mode {n.blend_mode!r} is approximated as alpha blending")


def _surface_inputs(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """Metallic と Smoothness（``LitInput.hlsl`` の ``SampleMetallicSpecGloss``）。

    Metallic はマップ（``_MetallicGlossMap``）があればその R（``_Metallic`` は掛けない）、無ければ ``_Metallic``。
    Smoothness はマップの A か、``_SmoothnessTextureChannel`` = 1 ならアルベドの A に ``_Smoothness`` を掛けたもので、
    どちらも無ければ ``_Smoothness``。アルベドの A を使うのは不透明のときだけ（LitGUI がキーワードを立てる条件）。
    """
    smoothness = mat.f("_Smoothness", 0.5)
    n.roughness = max(0.0, min(1.0, 1.0 - smoothness))
    n.smoothness_scale = max(0.0, min(1.0, smoothness))
    n.smoothness_from_albedo = (
        int(mat.f("_SmoothnessTextureChannel", 0.0)) == 1 and n.alpha_mode == "opaque" and n.base_color_tex is not None
    )
    if int(mat.f("_WorkflowMode", WORKFLOW_METALLIC)) == WORKFLOW_SPECULAR:
        # Specular ワークフロー（_SpecColor / _SpecGlossMap）は #117 で扱う。いまは非金属として読む
        n.metallic = 0.0
        n.warnings.append("URP specular workflow is not supported yet; read as non-metallic")
        return
    n.metallic = mat.f("_Metallic", 0.0)
    n.metallic_tex = mat.tex("_MetallicGlossMap")


def _extras(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """組み立てには使わない URP の設定（``unity_props`` に残す）。"""
    urp: dict[str, object] = {"blend": n.blend_mode} if n.alpha_mode == "blend" else {}
    if n.occlusion_tex is not None:
        urp["occlusion_strength"] = mat.f("_OcclusionStrength", 1.0)
    if mat.flag("_AlphaToMask"):
        urp["alpha_to_mask"] = True
    if mat.has("_ReceiveShadows") and not mat.flag("_ReceiveShadows", True):
        urp["receive_shadows"] = False
    if int(mat.f("_WorkflowMode", WORKFLOW_METALLIC)) == WORKFLOW_SPECULAR:
        spec_tex = mat.tex("_SpecGlossMap")
        urp["specular"] = {"color": mat.color("_SpecColor"), "tex": spec_tex.guid if spec_tex else None}
    for keyword, name in (("_DETAIL_MULX2", "detail"), ("_DETAIL_SCALED", "detail"), ("_PARALLAXMAP", "parallax"),
                          ("_CLEARCOAT", "clear_coat"), ("_CLEARCOATMAP", "clear_coat")):
        if keyword in mat.keywords:
            urp[name] = True
    if urp:
        n.extras["urp"] = urp
