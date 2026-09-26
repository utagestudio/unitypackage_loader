"""NormalizedMaterial から Blender のノードツリーを組み立てる。"""

from __future__ import annotations

import json
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
    backface_culling: bool = True
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
    def __init__(self, mat: bpy.types.Material):
        self.mat = mat
        if mat.node_tree is None:  # Blender 4.x の materials.new() はノードを持たない（5.0 以降は常に持つ）
            mat.use_nodes = True
        self.tree = mat.node_tree
        self.nodes = self.tree.nodes
        self.links = self.tree.links
        self.nodes.clear()
        self._rows: dict[int, int] = {}

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

    b = _Builder(mat)
    out = b.add("ShaderNodeOutputMaterial", 4)

    # --- UV / ベースカラー ---
    mapping_out = None
    if norm.base_color_tex is not None and norm.base_color_tex.has_transform:
        coord = b.add("ShaderNodeTexCoord", -1)
        mapping = b.add("ShaderNodeMapping", 0, label="Unity Tiling/Offset")
        mapping.inputs["Scale"].default_value = (norm.uv_scale[0], norm.uv_scale[1], 1.0)
        mapping.inputs["Location"].default_value = (norm.uv_offset[0], norm.uv_offset[1], 0.0)
        b.link(coord.outputs["UV"], mapping.inputs["Vector"])
        mapping_out = mapping.outputs["Vector"]

    base_img = _image_for(norm.base_color_tex, images, warnings, "base color")
    base_node = None
    if norm.base_color_tex is not None:
        base_node = b.image(base_img, tex_infos.get(norm.base_color_tex.guid), 1, "Base Color")
        if mapping_out is not None:
            b.link(mapping_out, base_node.inputs["Vector"])

    color_out = base_node.outputs["Color"] if base_node is not None else None
    rgb = norm.base_color[:3]
    if color_out is not None and any(abs(c - 1.0) > 1e-4 for c in rgb):
        mix = b.add("ShaderNodeMix", 2, label="Tint (_Color)")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        socket(mix.inputs, "Factor_Float").default_value = 1.0
        socket(mix.inputs, "B_Color").default_value = (*rgb, 1.0)
        b.link(color_out, socket(mix.inputs, "A_Color"))
        color_out = socket(mix.outputs, "Result_Color")

    # --- アルファ ---
    alpha_out = None
    if alpha_mode != "opaque":
        if base_node is not None and norm.alpha_from_texture:
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
        _build_unlit(b, norm, out, color_out, alpha_out, images, tex_infos, warnings, opts)
    elif mode == MODE_TOON:
        _build_toon(b, norm, out, color_out, alpha_out, mapping_out, images, tex_infos, warnings, opts)
    else:
        base_alpha = base_node.outputs["Alpha"] if base_node is not None else None
        _build_principled(b, norm, out, color_out, alpha_out, mapping_out, images, tex_infos, warnings, opts, base_alpha)
    return mode, warnings


def _image_for(ref: TexRef | None, images, warnings: list[str], role: str):
    if ref is None:
        return None
    image = images.get(ref.guid)
    if image is None:
        warnings.append(f"{role} texture {ref.guid} could not be loaded (missing from package or unsupported format)")
    return image


def _build_principled(b, norm, out, color_out, alpha_out, mapping_out, images, tex_infos, warnings, opts, base_alpha=None):
    bsdf = b.add("ShaderNodeBsdfPrincipled", 3)
    b.link(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if color_out is not None:
        b.link(color_out, bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = (*norm.base_color[:3], 1.0)
    if alpha_out is not None:
        b.link(alpha_out, bsdf.inputs["Alpha"])
    elif norm.alpha_mode != "opaque" and not opts.force_opaque:
        bsdf.inputs["Alpha"].default_value = norm.base_color[3]

    bsdf.inputs["Metallic"].default_value = max(0.0, min(1.0, norm.metallic))
    bsdf.inputs["Roughness"].default_value = max(0.0, min(1.0, norm.roughness))

    smoothness_out = base_alpha if norm.smoothness_from_albedo else None
    if norm.metallic_tex is not None:
        img = _image_for(norm.metallic_tex, images, warnings, "metallic")
        label = "Metallic (R)" if norm.smoothness_from_albedo else "Metallic (R) / Smoothness (A)"
        node = b.image(img, tex_infos.get(norm.metallic_tex.guid), 1, label)
        if mapping_out is not None:
            b.link(mapping_out, node.inputs["Vector"])
        sep = b.add("ShaderNodeSeparateColor", 2, label="Metallic R")
        b.link(node.outputs["Color"], sep.inputs["Color"])
        b.link(sep.outputs["Red"], bsdf.inputs["Metallic"])
        if not norm.smoothness_from_albedo:
            smoothness_out = node.outputs["Alpha"]
    if smoothness_out is not None:
        # Unity の Smoothness は A × 倍率（URP の _Smoothness / Standard の _GlossMapScale。#112）
        if abs(norm.smoothness_scale - 1.0) > 1e-4:
            mul = b.add("ShaderNodeMath", 2, label=f"Smoothness × {norm.smoothness_scale:g}")
            mul.operation = "MULTIPLY"
            mul.inputs[1].default_value = norm.smoothness_scale
            b.link(smoothness_out, mul.inputs[0])
            smoothness_out = mul.outputs[0]
        inv = b.add("ShaderNodeMath", 2, label="Roughness = 1 - Smoothness")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        b.link(smoothness_out, inv.inputs[1])
        b.link(inv.outputs[0], bsdf.inputs["Roughness"])

    if opts.use_normal_maps and norm.normal_tex is not None:
        img = _image_for(norm.normal_tex, images, warnings, "normal")
        node = b.image(img, tex_infos.get(norm.normal_tex.guid), 1, "Normal")
        if mapping_out is not None:
            b.link(mapping_out, node.inputs["Vector"])
        nm = b.add("ShaderNodeNormalMap", 2)
        nm.inputs["Strength"].default_value = max(0.0, norm.normal_strength)
        b.link(node.outputs["Color"], nm.inputs["Color"])
        b.link(nm.outputs["Normal"], bsdf.inputs["Normal"])

    if opts.use_emission and norm.has_emission:
        bsdf.inputs["Emission Strength"].default_value = norm.emission_strength
        if norm.emission_tex is not None:
            img = _image_for(norm.emission_tex, images, warnings, "emission")
            node = b.image(img, tex_infos.get(norm.emission_tex.guid), 1, "Emission")
            if mapping_out is not None:
                b.link(mapping_out, node.inputs["Vector"])
            e_out = node.outputs["Color"]
            if any(abs(c - 1.0) > 1e-4 for c in norm.emission_color[:3]):
                mix = b.add("ShaderNodeMix", 2, label="Emission × _EmissionColor")
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                socket(mix.inputs, "Factor_Float").default_value = 1.0
                socket(mix.inputs, "B_Color").default_value = (*norm.emission_color[:3], 1.0)
                b.link(e_out, socket(mix.inputs, "A_Color"))
                e_out = socket(mix.outputs, "Result_Color")
            b.link(e_out, bsdf.inputs["Emission Color"])
        else:
            bsdf.inputs["Emission Color"].default_value = (*norm.emission_color[:3], 1.0)
    else:
        bsdf.inputs["Emission Strength"].default_value = 0.0

    if norm.occlusion_tex is not None:
        img = _image_for(norm.occlusion_tex, images, warnings, "occlusion")
        b.image(img, tex_infos.get(norm.occlusion_tex.guid), 1, "Occlusion (unused)")


def _build_unlit(b, norm, out, color_out, alpha_out, images, tex_infos, warnings, opts):
    emit = b.add("ShaderNodeEmission", 3, label="Unlit")
    emit.inputs["Strength"].default_value = 1.0
    if color_out is not None:
        b.link(color_out, emit.inputs["Color"])
    else:
        emit.inputs["Color"].default_value = (*norm.base_color[:3], 1.0)

    if alpha_out is not None or (norm.alpha_mode != "opaque" and not opts.force_opaque and norm.base_color[3] < 1.0):
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
        if ref is not None and opts.use_normal_maps:
            img = images.get(ref.guid)
            if img is not None:
                b.image(img, tex_infos.get(ref.guid), 1, label)


def _normal_output(b, norm, mapping_out, images, tex_infos, warnings, opts, col: int):
    """ノーマルマップがあれば Normal Map ノード、無ければ Geometry の Normal を返す。"""
    if opts.use_normal_maps and norm.normal_tex is not None:
        img = _image_for(norm.normal_tex, images, warnings, "normal")
        node = b.image(img, tex_infos.get(norm.normal_tex.guid), col, "Normal")
        if mapping_out is not None:
            b.link(mapping_out, node.inputs["Vector"])
        nm = b.add("ShaderNodeNormalMap", col + 1)
        nm.inputs["Strength"].default_value = max(0.0, norm.normal_strength)
        b.link(node.outputs["Color"], nm.inputs["Color"])
        return nm.outputs["Normal"]
    geo = b.add("ShaderNodeNewGeometry", col + 1, label="Geometry normal")
    return geo.outputs["Normal"]


def _build_toon(b, norm, out, color_out, alpha_out, mapping_out, images, tex_infos, warnings, opts):
    from .toon_group import get_toon_group

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
    elif norm.alpha_mode != "opaque" and not opts.force_opaque:
        group.inputs["Alpha"].default_value = norm.base_color[3]

    normal_out = _normal_output(b, norm, mapping_out, images, tex_infos, warnings, opts, 1)
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

    matcap = norm.matcap
    matcap_img = images.get(matcap.tex) if matcap is not None else None
    if matcap_img is not None:
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
        tex = b.image(matcap_img, tex_infos.get(matcap.tex), 1, "MatCap")
        tex.extension = "EXTEND"
        b.link(uv.outputs["Vector"], tex.inputs["Vector"])
        mc_out = tex.outputs["Color"]
        mc_color = toon_color(matcap.color, WHITE)
        if any(abs(c - 1.0) > 1e-4 for c in mc_color[:3]):
            tint = b.add("ShaderNodeMix", 2, label="MatCap Color")
            tint.data_type = "RGBA"
            tint.blend_type = "MULTIPLY"
            socket(tint.inputs, "Factor_Float").default_value = 1.0
            socket(tint.inputs, "B_Color").default_value = mc_color
            b.link(mc_out, socket(tint.inputs, "A_Color"))
            mc_out = socket(tint.outputs, "Result_Color")
        b.link(mc_out, group.inputs["MatCap"])
        group.inputs["MatCap Strength"].default_value = max(0.0, min(1.0, float(matcap.strength)))
        group.inputs["MatCap Mode"].default_value = float(matcap.mode)
    elif matcap is not None:
        warnings.append("matcap texture could not be loaded")

    rim = norm.rim
    if rim is not None:
        rim_color = toon_color(rim.color, WHITE)
        group.inputs["Rim Color"].default_value = (*rim_color[:3], 1.0)
        group.inputs["Rim Strength"].default_value = float(rim.strength)
        group.inputs["Rim Border"].default_value = float(rim.border)
        group.inputs["Rim Blur"].default_value = float(rim.blur)

    if opts.use_emission and norm.has_emission:
        group.inputs["Emission Strength"].default_value = norm.emission_strength
        if norm.emission_tex is not None:
            img = _image_for(norm.emission_tex, images, warnings, "emission")
            node = b.image(img, tex_infos.get(norm.emission_tex.guid), 1, "Emission")
            if mapping_out is not None:
                b.link(mapping_out, node.inputs["Vector"])
            e_out = node.outputs["Color"]
            if any(abs(c - 1.0) > 1e-4 for c in norm.emission_color[:3]):
                mix = b.add("ShaderNodeMix", 2, label="Emission × _EmissionColor")
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                socket(mix.inputs, "Factor_Float").default_value = 1.0
                socket(mix.inputs, "B_Color").default_value = (*norm.emission_color[:3], 1.0)
                b.link(e_out, socket(mix.inputs, "A_Color"))
                e_out = socket(mix.outputs, "Result_Color")
            b.link(e_out, group.inputs["Emission"])
        else:
            group.inputs["Emission"].default_value = (*norm.emission_color[:3], 1.0)


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
