"""Solidify モディファイアによるアウトライン（Unity のトゥーンシェーダーの Outline 相当）。

法線を反転した外側シェルに、裏面カリング付きの単色 Emission マテリアルを割り当てる古典的な手法。
幅は Unity 側の値（lilToon は cm 相当）を ``width_scale`` 倍してメートルにする。
"""

from __future__ import annotations

import json

import bpy

MODIFIER_NAME = "UnityOutline"
DEFAULT_WIDTH_SCALE = 0.01


def outline_params(mat: bpy.types.Material) -> tuple[tuple[float, float, float, float], float] | None:
    """マテリアルのカスタムプロパティから (color, width) を取り出す。無ければ None。"""
    raw = mat.get("unity_props")
    if not raw:
        return None
    try:
        props = json.loads(raw)
    except (TypeError, ValueError):
        return None
    outline = props.get("outline") if isinstance(props, dict) else None
    if not isinstance(outline, dict):
        return None
    width = float(outline.get("width", 0.0) or 0.0)
    color = outline.get("color")
    if width <= 0.0 or not isinstance(color, (list, tuple)) or len(color) < 3:
        return None
    rgba = tuple(float(c) for c in color[:3]) + (1.0,)
    return rgba, width


def outline_material(color: tuple[float, float, float, float]) -> bpy.types.Material:
    key = "UnityOutline_" + "".join(f"{int(max(0.0, min(1.0, c)) * 255):02x}" for c in color[:3])
    mat = bpy.data.materials.get(key)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(key)
    mat.use_backface_culling = True
    mat.diffuse_color = color
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "DITHERED"
    tree = mat.node_tree
    tree.nodes.clear()
    out = tree.nodes.new("ShaderNodeOutputMaterial")
    emit = tree.nodes.new("ShaderNodeEmission")
    emit.location = (-250, 0)
    emit.inputs["Color"].default_value = color
    emit.inputs["Strength"].default_value = 1.0
    tree.links.new(emit.outputs[0], out.inputs["Surface"])
    mat["unity_outline"] = True
    return mat


def add_outline(obj: bpy.types.Object, color, width: float, width_scale: float = DEFAULT_WIDTH_SCALE) -> bool:
    if obj.type != "MESH":
        return False
    mat = outline_material(color)
    mesh = obj.data
    index = next((i for i, m in enumerate(mesh.materials) if m == mat), None)
    if index is None:
        mesh.materials.append(mat)
        index = len(mesh.materials) - 1
    mod = obj.modifiers.get(MODIFIER_NAME)
    if mod is None or mod.type != "SOLIDIFY":
        mod = obj.modifiers.new(MODIFIER_NAME, "SOLIDIFY")
    mod.thickness = width * width_scale
    mod.offset = 1.0
    mod.use_flip_normals = True
    mod.use_rim = False
    mod.use_quality_normals = True
    mod.material_offset = index
    mod.material_offset_rim = index
    mod.show_in_editmode = False
    return True


def remove_outline(obj: bpy.types.Object) -> bool:
    mod = obj.modifiers.get(MODIFIER_NAME)
    if mod is None:
        return False
    obj.modifiers.remove(mod)
    if obj.type == "MESH":
        for i in reversed(range(len(obj.data.materials))):
            m = obj.data.materials[i]
            if m is not None and m.get("unity_outline"):
                obj.data.materials.pop(index=i)
    return True


def apply_outlines(objects, width_scale: float = DEFAULT_WIDTH_SCALE) -> int:
    """各オブジェクトの最初の「アウトライン設定を持つマテリアル」に従ってアウトラインを付ける。"""
    count = 0
    for obj in objects:
        if obj.type != "MESH":
            continue
        params = None
        for slot in obj.material_slots:
            if slot.material is not None and not slot.material.get("unity_outline"):
                params = outline_params(slot.material)
                if params:
                    break
        if params and add_outline(obj, params[0], params[1], width_scale):
            count += 1
    return count
