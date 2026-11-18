"""NormalizedMaterial から Blender のノードツリーを組み立てる。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import bpy

from ..core.material import GRAY, WHITE, NormalizedMaterial, TexRef, toon_color
from ..core.meta import TextureImporterInfo
from .nodes import socket

MODE_PRINCIPLED = "PRINCIPLED"
MODE_TOON = "TOON"
MODE_UNLIT = "UNLIT"
MODE_NAMES_ONLY = "NAMES_ONLY"
MODE_AUTO = "AUTO"


@dataclass
class MaterialBuildOptions:
    mode: str = MODE_AUTO
    force_opaque: bool = False
    backface_culling: bool = False  # Unity の _Cull に従って背面を隠すか（#169）
    use_normal_maps: bool = True
    use_emission: bool = True
    store_props: bool = True


def effective_mode(norm: NormalizedMaterial, mode: str) -> str:
    if mode != MODE_AUTO:
        return mode
    if norm.lighting == "toon":
        return MODE_TOON
    if norm.lighting == "unlit":
        return MODE_UNLIT
    return MODE_PRINCIPLED


# ---------------------------------------------------------------------------
# ノード配置のための小さなヘルパ
# ---------------------------------------------------------------------------


class _Builder:
    """1 つのマテリアルのノードを組む間の状態（ノードの並び、組むときの値、画像、警告）。

    以前はノードを組む関数が images / tex_infos / warnings / opts などを 10 個前後の引数で受け渡していた（#198）。
    """

    def __init__(
        self, mat: bpy.types.Material, norm: NormalizedMaterial, images, tex_infos: dict[str, TextureImporterInfo],
        opts: MaterialBuildOptions, warnings: list[str],
    ):
        self.mat = mat
        if mat.node_tree is None:  # Blender 4.x の materials.new() はノードを持たない（5.0 以降は常に持つ）
            mat.use_nodes = True
        self.tree = mat.node_tree
        self.nodes = self.tree.nodes
        self.links = self.tree.links
        self.nodes.clear()
        self._rows: dict[int, int] = {}
        self.norm = norm
        self.images = images
        self.tex_infos = tex_infos
        self.opts = opts
        self.warnings = warnings
        self.mapping_out = None  # Unity の Tiling / Offset を掛けた UV（ベースのテクスチャに Tiling があるときだけ）

    def add(self, bl_idname: str, col: int, *, label: str | None = None, name: str | None = None):
        node = self.nodes.new(bl_idname)
        row = self._rows.get(col, 0)
        node.location = (col * 300, -row * 320)
        self._rows[col] = row + 1
        if label:
            node.label = label
        if name:
            node.name = name
        return node

    def link(self, out_socket, in_socket) -> None:
        self.links.new(out_socket, in_socket)

    def image(self, image: bpy.types.Image | None, info: TextureImporterInfo | None, col: int, label: str):
        """画像テクスチャのノード。色空間は画像側の設定に従う。"""
        node = self.add("ShaderNodeTexImage", col, label=label)
        node.image = image
        if info is not None and info.clamps:
            node.extension = "EXTEND"
        return node

    def image_for(self, ref: TexRef | None, role: str):
        """テクスチャの参照から読み込んだ画像。読み込めなければ警告に足して None。"""
        if ref is None:
            return None
        image = self.images.get(ref.guid)
        if image is None:
            self.warnings.append(f"{role} texture {ref.guid} could not be loaded (missing from package or unsupported format)")
        return image

    def texture(self, ref: TexRef, role: str, label: str, col: int = 1):
        """テクスチャの画像ノードを置き、Unity の Tiling / Offset を掛けた UV があればつなぐ。"""
        node = self.image(self.image_for(ref, role), self.tex_infos.get(ref.guid), col, label)
        if self.mapping_out is not None:
            self.link(self.mapping_out, node.inputs["Vector"])
        return node

    def multiply(self, color_out, color: tuple, col: int, label: str):
        """色の出力に色を掛ける（Mix の Multiply）。掛けた結果の出力を返す。"""
        mix = self.add("ShaderNodeMix", col, label=label)
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        socket(mix.inputs, "Factor_Float").default_value = 1.0
        socket(mix.inputs, "B_Color").default_value = color
        self.link(color_out, socket(mix.inputs, "A_Color"))
        return socket(mix.outputs, "Result_Color")

    def emission_texture(self):
        """発光のテクスチャ（× _EmissionColor）の出力。テクスチャが無ければ None。"""
        norm = self.norm
        if norm.emission_tex is None:
            return None
        out = self.texture(norm.emission_tex, "emission", "Emission").outputs["Color"]
        if any(abs(c - 1.0) > 1e-4 for c in norm.emission_color[:3]):
            out = self.multiply(out, (*norm.emission_color[:3], 1.0), 2, "Emission × _EmissionColor")
        return out

    def transparent(self) -> bool:
        """透明の設定を使うか（Force Opaque でなく、.mat が不透明でない）。"""
        return self.norm.alpha_mode != "opaque" and not self.opts.force_opaque


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, TexRef):
        return value.guid
    return value


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def build_material(
    mat: bpy.types.Material,
    norm: NormalizedMaterial,
    images: dict[str, bpy.types.Image | None],
    tex_infos: dict[str, TextureImporterInfo],
    opts: MaterialBuildOptions,
) -> tuple[str, list[str]]:
    """マテリアルを組み立て、(実際に使ったモード, 警告一覧) を返す。"""
    warnings: list[str] = list(norm.warnings)
    mode = effective_mode(norm, opts.mode)

    alpha_mode = "opaque" if opts.force_opaque else norm.alpha_mode
    _apply_settings(mat, norm, alpha_mode, opts)
    if opts.store_props:
        store_props(mat, norm, opts)
    if mode == MODE_NAMES_ONLY:
        return mode, warnings

    b = _Builder(mat, norm, images, tex_infos, opts, warnings)
    out = b.add("ShaderNodeOutputMaterial", 4)

    # --- UV / ベースカラー ---
    if norm.base_color_tex is not None and norm.base_color_tex.has_transform:
        coord = b.add("ShaderNodeTexCoord", -1)
        mapping = b.add("ShaderNodeMapping", 0, label="Unity Tiling/Offset")
        mapping.inputs["Scale"].default_value = (norm.uv_scale[0], norm.uv_scale[1], 1.0)
        mapping.inputs["Location"].default_value = (norm.uv_offset[0], norm.uv_offset[1], 0.0)
        b.link(coord.outputs["UV"], mapping.inputs["Vector"])
        b.mapping_out = mapping.outputs["Vector"]

    base_node = b.texture(norm.base_color_tex, "base color", "Base Color") if norm.base_color_tex is not None else None
    color_out = base_node.outputs["Color"] if base_node is not None else None
    rgb = norm.base_color[:3]
    if color_out is not None and any(abs(c - 1.0) > 1e-4 for c in rgb):
        color_out = b.multiply(color_out, (*rgb, 1.0), 2, "Tint (_Color)")

    # --- アルファ ---
    alpha_out = None
    if alpha_mode != "opaque":
        if norm.alpha_tex is not None and norm.alpha_from_texture:
            # 色の画像とは別のアルファの画像（白黒のマスク）の R（#266）
            mask = b.texture(norm.alpha_tex, "alpha", "Alpha Mask (R)")
            split = b.add("ShaderNodeSeparateColor", 2, label="Alpha Mask R")
            b.link(mask.outputs["Color"], split.inputs["Color"])
            alpha_out = split.outputs["Red"]
        elif base_node is not None and norm.alpha_from_texture:
            alpha_out = base_node.outputs["Alpha"]
        if norm.base_color[3] < 1.0 - 1e-4:
            mul = b.add("ShaderNodeMath", 2, label="Alpha × _Color.a")
            mul.operation = "MULTIPLY"
            mul.inputs[1].default_value = norm.base_color[3]
            if alpha_out is not None:
                b.link(alpha_out, mul.inputs[0])
            else:
                mul.inputs[0].default_value = 1.0
            alpha_out = mul.outputs[0]
        if alpha_mode == "cutout" and alpha_out is not None:
            cut = b.add("ShaderNodeMath", 3, label=f"Cutoff {norm.alpha_cutoff:g}")
            cut.operation = "GREATER_THAN"
            cut.inputs[1].default_value = norm.alpha_cutoff
            b.link(alpha_out, cut.inputs[0])
            alpha_out = cut.outputs[0]

    if mode == MODE_UNLIT:
        _build_unlit(b, out, color_out, alpha_out)
    elif mode == MODE_TOON:
        _build_toon(b, out, color_out, alpha_out)
    else:
        base_alpha = base_node.outputs["Alpha"] if base_node is not None else None
        _build_principled(b, out, color_out, alpha_out, base_alpha)
    return mode, warnings


# Principled の IOR で表す F0 の上限。F0 → 1 で IOR が発散するので抑える
_SPECULAR_F0_MAX = 0.9


def _specular(b: _Builder, bsdf):
    """スペキュラーの色（F0）を IOR と Specular Tint で表す（#117）。組んだスペキュラーマップのノードを返す。

    Specular IOR Level は 0〜1（F0 を最大 2 倍）なので、Unity の値（Lit の Specular は既定で 0.2）には届かない。
    強さは IOR（F0 = ((IOR − 1) / (IOR + 1))²）で、色味は Specular Tint（F0 = IOR の F0 × Tint）で表す。
    マップがあれば F0 = マップの RGB × 色なので、IOR は色の最大成分から決め、Tint にマップ × 色 / その F0 をつなぐ。
    """
    norm = b.norm
    if norm.specular_color is None:
        return None
    rgb = [max(0.0, c) for c in norm.specular_color[:3]]
    peak = max(rgb)
    if peak <= 1e-4:
        bsdf.inputs["Specular IOR Level"].default_value = 0.0  # ハイライトも反射も無い
        return None
    f0 = min(peak, _SPECULAR_F0_MAX)
    root = math.sqrt(f0)
    bsdf.inputs["IOR"].default_value = (1.0 + root) / (1.0 - root)
    tint = tuple(c / f0 for c in rgb)
    node = None
    if norm.specular_tex is not None:
        node = b.texture(norm.specular_tex, "specular", "Specular (RGB) / Smoothness (A)")
        b.link(b.multiply(node.outputs["Color"], (*tint, 1.0), 2, "Specular × _SpecColor / F0"), bsdf.inputs["Specular Tint"])
    else:
        bsdf.inputs["Specular Tint"].default_value = (*tint, 1.0)
    return node


def _build_principled(b: _Builder, out, color_out, alpha_out, base_alpha=None):
    norm, opts = b.norm, b.opts
    bsdf = b.add("ShaderNodeBsdfPrincipled", 3)
    b.link(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if color_out is not None:
        b.link(color_out, bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = (*norm.base_color[:3], 1.0)
    if alpha_out is not None:
        b.link(alpha_out, bsdf.inputs["Alpha"])
    elif b.transparent():
        bsdf.inputs["Alpha"].default_value = norm.base_color[3]

    bsdf.inputs["Metallic"].default_value = max(0.0, min(1.0, norm.metallic))
    bsdf.inputs["Roughness"].default_value = max(0.0, min(1.0, norm.roughness))

    smoothness_out = base_alpha if norm.smoothness_from_albedo else None
    if norm.metallic_tex is not None:
        label = "Metallic (R)" if norm.smoothness_from_albedo else "Metallic (R) / Smoothness (A)"
        node = b.texture(norm.metallic_tex, "metallic", label)
        sep = b.add("ShaderNodeSeparateColor", 2, label="Metallic R")
        b.link(node.outputs["Color"], sep.inputs["Color"])
        metallic_out = sep.outputs["Red"]
        low, high = norm.metallic_remap
        if abs(low) > 1e-4 or abs(high - 1.0) > 1e-4:
            # HDRP のマスクマップの Remap（#119）
            remap = b.add("ShaderNodeMath", 2, label=f"Metallic Remap {low:g}–{high:g}")
            remap.operation = "MULTIPLY_ADD"
            remap.inputs[1].default_value = high - low
            remap.inputs[2].default_value = low
            b.link(metallic_out, remap.inputs[0])
            metallic_out = remap.outputs[0]
        b.link(metallic_out, bsdf.inputs["Metallic"])
        if not norm.smoothness_from_albedo:
            smoothness_out = node.outputs["Alpha"]
    spec_node = _specular(b, bsdf)
    if smoothness_out is None and spec_node is not None and norm.smoothness_from_specular:
        smoothness_out = spec_node.outputs["Alpha"]  # Specular ワークフロー / Simple Lit のマップの A（#117）
    if smoothness_out is not None:
        _link_roughness(b, smoothness_out, bsdf.inputs["Roughness"])

    if opts.use_normal_maps and norm.normal_tex is not None:
        node = b.texture(norm.normal_tex, "normal", "Normal")
        nm = b.add("ShaderNodeNormalMap", 2)
        nm.inputs["Strength"].default_value = max(0.0, norm.normal_strength)
        b.link(node.outputs["Color"], nm.inputs["Color"])
        b.link(nm.outputs["Normal"], bsdf.inputs["Normal"])

    if opts.use_emission and norm.has_emission:
        bsdf.inputs["Emission Strength"].default_value = norm.emission_strength
        emission_out = b.emission_texture()
        if emission_out is not None:
            b.link(emission_out, bsdf.inputs["Emission Color"])
        else:
            bsdf.inputs["Emission Color"].default_value = (*norm.emission_color[:3], 1.0)
    else:
        bsdf.inputs["Emission Strength"].default_value = 0.0

    if norm.occlusion_tex is not None:
        img = b.image_for(norm.occlusion_tex, "occlusion")
        b.image(img, b.tex_infos.get(norm.occlusion_tex.guid), 1, "Occlusion (unused)")


def _link_roughness(b: _Builder, smoothness_out, roughness_in) -> None:
    """Unity の Smoothness（テクスチャの A × 倍率。#112）から Roughness = 1 − Smoothness を組んでつなぐ。"""
    norm = b.norm
    if abs(norm.smoothness_offset) > 1e-4:
        # HDRP のマスクマップの Remap: offset + A × (max − min)（#119）
        mul = b.add("ShaderNodeMath", 2, label=f"Smoothness × {norm.smoothness_scale:g} + {norm.smoothness_offset:g}")
        mul.operation = "MULTIPLY_ADD"
        mul.inputs[1].default_value = norm.smoothness_scale
        mul.inputs[2].default_value = norm.smoothness_offset
        b.link(smoothness_out, mul.inputs[0])
        smoothness_out = mul.outputs[0]
    elif abs(norm.smoothness_scale - 1.0) > 1e-4:
        mul = b.add("ShaderNodeMath", 2, label=f"Smoothness × {norm.smoothness_scale:g}")
        mul.operation = "MULTIPLY"
        mul.inputs[1].default_value = norm.smoothness_scale
        b.link(smoothness_out, mul.inputs[0])
        smoothness_out = mul.outputs[0]
    inv = b.add("ShaderNodeMath", 2, label="Roughness = 1 - Smoothness")
    inv.operation = "SUBTRACT"
    inv.inputs[0].default_value = 1.0
    b.link(smoothness_out, inv.inputs[1])
    b.link(inv.outputs[0], roughness_in)


def _build_unlit(b: _Builder, out, color_out, alpha_out):
    norm = b.norm
    emit = b.add("ShaderNodeEmission", 3, label="Unlit")
    emit.inputs["Strength"].default_value = 1.0
    if color_out is not None:
        b.link(color_out, emit.inputs["Color"])
    else:
        emit.inputs["Color"].default_value = (*norm.base_color[:3], 1.0)

    if alpha_out is not None or (b.transparent() and norm.base_color[3] < 1.0):
        transparent = b.add("ShaderNodeBsdfTransparent", 3)
        mix = b.add("ShaderNodeMixShader", 4)
        if alpha_out is not None:
            b.link(alpha_out, mix.inputs[0])
        else:
            mix.inputs[0].default_value = norm.base_color[3]
        b.link(transparent.outputs[0], mix.inputs[1])
        b.link(emit.outputs[0], mix.inputs[2])
        b.link(mix.outputs[0], out.inputs["Surface"])
        out.location = (5 * 300, out.location[1])
    else:
        b.link(emit.outputs[0], out.inputs["Surface"])

    # Unlit では使わないが、後で使えるように未接続ノードとして置く
    for ref, label in ((norm.normal_tex, "Normal (unused)"), (norm.emission_tex, "Emission (unused)")):
        if ref is not None and b.opts.use_normal_maps:
            img = b.images.get(ref.guid)
            if img is not None:
                b.image(img, b.tex_infos.get(ref.guid), 1, label)


def _normal_output(b: _Builder, col: int):
    """ノーマルマップがあれば Normal Map ノード、無ければ Geometry の Normal を返す。"""
    norm = b.norm
    if b.opts.use_normal_maps and norm.normal_tex is not None:
        node = b.texture(norm.normal_tex, "normal", "Normal", col)
        nm = b.add("ShaderNodeNormalMap", col + 1)
        nm.inputs["Strength"].default_value = max(0.0, norm.normal_strength)
        b.link(node.outputs["Color"], nm.inputs["Color"])
        return nm.outputs["Normal"]
    geo = b.add("ShaderNodeNewGeometry", col + 1, label="Geometry normal")
    return geo.outputs["Normal"]


def _build_toon(b: _Builder, out, color_out, alpha_out):
    from .toon_group import get_toon_group

    norm = b.norm
    group = b.add("ShaderNodeGroup", 3, label="UnityToon")
    group.node_tree = get_toon_group()
    group.width = 220
    b.link(group.outputs["Shader"], out.inputs["Surface"])

    if color_out is not None:
        b.link(color_out, group.inputs["Base Color"])
    else:
        group.inputs["Base Color"].default_value = (*norm.base_color[:3], 1.0)
    if alpha_out is not None:
        b.link(alpha_out, group.inputs["Alpha"])
    elif b.transparent():
        group.inputs["Alpha"].default_value = norm.base_color[3]

    normal_out = _normal_output(b, 1)
    b.link(normal_out, group.inputs["Normal"])

    # 影・MatCap・リムは、プロファイルが換算した型付きの値だけを読む（extras のキーの名前には依らない。#73）
    shadow = norm.shadow
    if shadow is not None:
        group.inputs["Shadow Color"].default_value = toon_color(shadow.color, GRAY)
        group.inputs["Shadow Strength"].default_value = float(shadow.strength)
        group.inputs["Shadow Border"].default_value = float(shadow.border)
        group.inputs["Shadow Blur"].default_value = float(shadow.blur)
    else:
        group.inputs["Shadow Strength"].default_value = 0.0

    _toon_matcap(b, group, normal_out)

    rim = norm.rim
    if rim is not None:
        rim_color = toon_color(rim.color, WHITE)
        group.inputs["Rim Color"].default_value = (*rim_color[:3], 1.0)
        group.inputs["Rim Strength"].default_value = float(rim.strength)
        group.inputs["Rim Border"].default_value = float(rim.border)
        group.inputs["Rim Blur"].default_value = float(rim.blur)

    if b.opts.use_emission and norm.has_emission:
        group.inputs["Emission Strength"].default_value = norm.emission_strength
        emission_out = b.emission_texture()
        if emission_out is not None:
            b.link(emission_out, group.inputs["Emission"])
        else:
            group.inputs["Emission"].default_value = (*norm.emission_color[:3], 1.0)


def _toon_matcap(b: _Builder, group, normal_out) -> None:
    """MatCap（カメラから見た法線を UV にした画像）を UnityToon のノードグループにつなぐ。"""
    matcap = b.norm.matcap
    matcap_img = b.images.get(matcap.tex) if matcap is not None else None
    if matcap_img is None:
        if matcap is not None:
            b.warnings.append("matcap texture could not be loaded")
        return
    geo_n = b.add("ShaderNodeVectorTransform", 0, label="MatCap UV")
    geo_n.vector_type = "NORMAL"
    geo_n.convert_from = "WORLD"
    geo_n.convert_to = "CAMERA"
    b.link(normal_out, geo_n.inputs["Vector"])
    uv = b.add("ShaderNodeVectorMath", 1, label="×0.5 + 0.5")
    uv.operation = "MULTIPLY_ADD"
    uv.inputs[1].default_value = (0.5, 0.5, 0.0)
    uv.inputs[2].default_value = (0.5, 0.5, 0.0)
    b.link(geo_n.outputs["Vector"], uv.inputs[0])
    tex = b.image(matcap_img, b.tex_infos.get(matcap.tex), 1, "MatCap")
    tex.extension = "EXTEND"
    b.link(uv.outputs["Vector"], tex.inputs["Vector"])
    mc_out = tex.outputs["Color"]
    mc_color = toon_color(matcap.color, WHITE)
    if any(abs(c - 1.0) > 1e-4 for c in mc_color[:3]):
        mc_out = b.multiply(mc_out, mc_color, 2, "MatCap Color")
    b.link(mc_out, group.inputs["MatCap"])
    group.inputs["MatCap Strength"].default_value = max(0.0, min(1.0, float(matcap.strength)))
    group.inputs["MatCap Mode"].default_value = float(matcap.mode)


def _apply_settings(mat: bpy.types.Material, norm: NormalizedMaterial, alpha_mode: str, opts: MaterialBuildOptions) -> None:
    mat.surface_render_method = "BLENDED" if alpha_mode == "blend" else "DITHERED"
    mat.use_backface_culling = opts.backface_culling and norm.cull_backface
    mat.use_transparency_overlap = alpha_mode == "blend"
    mat.diffuse_color = (*norm.base_color[:3], 1.0)


BUILD_OPTIONS_PROP = "unity_build_options"
_REMEMBERED_OPTIONS = ("force_opaque", "backface_culling", "use_normal_maps", "use_emission")


def store_props(mat: bpy.types.Material, norm: NormalizedMaterial, opts: MaterialBuildOptions | None = None) -> None:
    """Unity 側の情報をカスタムプロパティに残す（別のモードでの再構築と、インポーターが作ったマテリアルを残す場合の記録に使う）。

    ``opts`` を渡すと、組み立てに使った設定も残す（再構築のダイアログの初期値にする。#78）。
    """
    if opts is not None:
        mat[BUILD_OPTIONS_PROP] = json.dumps({name: getattr(opts, name) for name in _REMEMBERED_OPTIONS})
    mat["unity_material_guid"] = norm.source_guid
    mat["unity_material_path"] = norm.source_path
    mat["unity_shader_guid"] = norm.shader_guid or ""
    mat["unity_shader_family"] = norm.family
    mat["unity_shader_name"] = norm.shader_name or ""
    mat["unity_alpha_mode"] = norm.alpha_mode
    mat["unity_props"] = json.dumps(_json_ready(norm.extras), ensure_ascii=False)
    mat["unity_normalized"] = json.dumps(_json_ready(norm.to_dict()), ensure_ascii=False)


def tag_images(images: dict[str, bpy.types.Image | None], tex_infos: dict[str, TextureImporterInfo]) -> None:
    """再構築時に GUID から画像を引けるように、画像側にも Unity 側の情報を残す。"""
    for guid, image in images.items():
        if image is None:
            continue
        image["unity_guid"] = guid
        info = tex_infos.get(guid)
        if info is not None:
            image["unity_texture_type"] = info.texture_type
            image["unity_clamps"] = info.clamps


def collect_tagged_images() -> tuple[dict[str, bpy.types.Image], dict[str, TextureImporterInfo]]:
    images: dict[str, bpy.types.Image] = {}
    infos: dict[str, TextureImporterInfo] = {}
    for image in bpy.data.images:
        guid = image.get("unity_guid")
        if not guid:
            continue
        images[guid] = image
        info = TextureImporterInfo()
        info.texture_type = int(image.get("unity_texture_type", 0))
        if image.get("unity_clamps"):
            info.wrap_u = info.wrap_v = 1
        infos[guid] = info
    return images, infos


def stored_build_options(mat: bpy.types.Material) -> dict[str, bool]:
    """``store_props`` が残した組み立ての設定。無いか壊れていれば空（呼び出し側の既定値を使う）。"""
    try:
        data = json.loads(mat.get(BUILD_OPTIONS_PROP) or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: bool(data[name]) for name in _REMEMBERED_OPTIONS if name in data}


def set_backface_culling(mat: bpy.types.Material, enabled: bool) -> bool:
    """Backface Culling を切り替える。ON は Unity の _Cull に従い、両面描画のマテリアルは ON にしない（#169）。

    組み立ての設定（``unity_build_options``）も書き換え、再構築のダイアログの初期値をそろえる。
    Unity の情報が無いマテリアル（アウトラインなど）は触らずに False を返す。
    """
    raw = mat.get("unity_normalized")
    if not raw:
        return False
    try:
        cull = bool(json.loads(raw).get("cull_backface", True))
    except (TypeError, ValueError, AttributeError):
        return False
    mat.use_backface_culling = enabled and cull
    options = stored_build_options(mat)
    options["backface_culling"] = enabled
    mat[BUILD_OPTIONS_PROP] = json.dumps(options)
    return True


def rebuild_from_props(mat: bpy.types.Material, mode: str, opts: MaterialBuildOptions | None = None) -> tuple[str, list[str]]:
    """カスタムプロパティ unity_normalized と読み込み済み画像だけでマテリアルを組み直す。"""
    raw = mat.get("unity_normalized")
    if not raw:
        raise ValueError(f"{mat.name}: no Unity data stored on this material")
    norm = NormalizedMaterial.from_dict(json.loads(raw))
    images, infos = collect_tagged_images()
    build_opts = opts or MaterialBuildOptions()
    build_opts.mode = mode
    return build_material(mat, norm, images, infos, build_opts)
