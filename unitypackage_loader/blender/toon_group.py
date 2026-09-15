"""トゥーン用ノードグループ "UnityToon" の生成。

Unity 側のトゥーンシェーダー（lilToon / MToon / Poiyomi 等）で共通して意味を持つ要素だけを
再現する: ベースカラー、影色（境界・ぼかし・強さ）、MatCap（Normal / Add / Screen / Multiply）、
リムライト、エミッション、アルファ。ライティングは Shader to RGB を使うため EEVEE 向け
（Cycles では影が落ちない）。
"""

from __future__ import annotations

import bpy

from .nodes import socket

GROUP_NAME = "UnityToon"
GROUP_VERSION = 1

_INPUTS = (
    # (name, socket_type, default, min, max)
    ("Base Color", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0), None, None),
    ("Alpha", "NodeSocketFloat", 1.0, 0.0, 1.0),
    ("Normal", "NodeSocketVector", None, None, None),
    ("Shadow Color", "NodeSocketColor", (0.8, 0.8, 0.8, 1.0), None, None),
    ("Shadow Strength", "NodeSocketFloat", 1.0, 0.0, 1.0),
    ("Shadow Border", "NodeSocketFloat", 0.5, 0.0, 1.0),
    ("Shadow Blur", "NodeSocketFloat", 0.1, 0.0, 1.0),
    ("MatCap", "NodeSocketColor", (0.0, 0.0, 0.0, 1.0), None, None),
    ("MatCap Strength", "NodeSocketFloat", 0.0, 0.0, 1.0),
    ("MatCap Mode", "NodeSocketFloat", 0.0, 0.0, 3.0),
    ("Rim Color", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0), None, None),
    ("Rim Strength", "NodeSocketFloat", 0.0, 0.0, 1.0),
    ("Rim Border", "NodeSocketFloat", 0.5, 0.0, 1.0),
    ("Rim Blur", "NodeSocketFloat", 0.1, 0.0, 1.0),
    ("Emission", "NodeSocketColor", (0.0, 0.0, 0.0, 1.0), None, None),
    ("Emission Strength", "NodeSocketFloat", 1.0, 0.0, None),
)


def get_toon_group() -> bpy.types.ShaderNodeTree:
    """既存の "UnityToon" があれば返し、無ければ（または古ければ）作る。"""
    tree = bpy.data.node_groups.get(GROUP_NAME)
    if tree is not None and tree.get("unitypkg_version") == GROUP_VERSION:
        return tree
    if tree is None:
        tree = bpy.data.node_groups.new(GROUP_NAME, "ShaderNodeTree")
    _build(tree)
    tree["unitypkg_version"] = GROUP_VERSION
    return tree


def _build(tree: bpy.types.ShaderNodeTree) -> None:
    tree.nodes.clear()
    for item in list(tree.interface.items_tree):
        tree.interface.remove(item)

    for name, socket_type, default, vmin, vmax in _INPUTS:
        sock = tree.interface.new_socket(name=name, in_out="INPUT", socket_type=socket_type)
        if default is not None:
            sock.default_value = default
        if vmin is not None:
            sock.min_value = vmin
        if vmax is not None:
            sock.max_value = vmax
        if name == "Normal":
            sock.hide_value = True
    tree.interface.new_socket(name="Shader", in_out="OUTPUT", socket_type="NodeSocketShader")

    nodes, links = tree.nodes, tree.links
    col = 0

    def add(bl_idname, x, y, label=None):
        node = nodes.new(bl_idname)
        node.location = (x * 220, y * -160)
        if label:
            node.label = label
        return node

    def mix_color(x, y, blend="MIX", label=None):
        node = add("ShaderNodeMix", x, y, label)
        node.data_type = "RGBA"
        node.blend_type = blend
        node.clamp_result = True
        return node

    def math(x, y, op, label=None, v1=None):
        node = add("ShaderNodeMath", x, y, label)
        node.operation = op
        if v1 is not None:
            node.inputs[1].default_value = v1
        return node

    inp = add("NodeGroupInput", -1, 3)
    out = add("NodeGroupOutput", 12, 3)

    # --- ライティング量（0..1） ---
    diffuse = add("ShaderNodeBsdfDiffuse", 0, 0, "Light probe")
    diffuse.inputs["Color"].default_value = (1, 1, 1, 1)
    diffuse.inputs["Roughness"].default_value = 0.0
    links.new(inp.outputs["Normal"], diffuse.inputs["Normal"])
    to_rgb = add("ShaderNodeShaderToRGB", 1, 0)
    links.new(diffuse.outputs[0], to_rgb.inputs[0])
    light = add("ShaderNodeRGBToBW", 2, 0)
    links.new(to_rgb.outputs["Color"], light.inputs[0])

    # 境界 ± ぼかし/2 で 0..1 に
    half_blur = math(1, 1, "MULTIPLY", "blur/2", 0.5)
    links.new(inp.outputs["Shadow Blur"], half_blur.inputs[0])
    lo = math(2, 1, "SUBTRACT", "border - blur/2")
    links.new(inp.outputs["Shadow Border"], lo.inputs[0])
    links.new(half_blur.outputs[0], lo.inputs[1])
    hi = math(2, 2, "ADD", "border + blur/2")
    links.new(inp.outputs["Shadow Border"], hi.inputs[0])
    links.new(half_blur.outputs[0], hi.inputs[1])
    shadow_fac = add("ShaderNodeMapRange", 3, 0, "Shadow factor")
    shadow_fac.clamp = True
    links.new(light.outputs[0], shadow_fac.inputs["Value"])
    links.new(lo.outputs[0], shadow_fac.inputs["From Min"])
    links.new(hi.outputs[0], shadow_fac.inputs["From Max"])

    # --- 影色: base * shadow color と base を factor で混ぜ、強さで元に戻す ---
    shadowed = mix_color(3, 2, "MULTIPLY", "Base × Shadow Color")
    socket(shadowed.inputs, "Factor_Float").default_value = 1.0
    links.new(inp.outputs["Base Color"], socket(shadowed.inputs, "A_Color"))
    links.new(inp.outputs["Shadow Color"], socket(shadowed.inputs, "B_Color"))
    lit_mix = mix_color(4, 1, "MIX", "Lit / Shadow")
    links.new(shadow_fac.outputs[0], socket(lit_mix.inputs, "Factor_Float"))
    links.new(socket(shadowed.outputs, "Result_Color"), socket(lit_mix.inputs, "A_Color"))
    links.new(inp.outputs["Base Color"], socket(lit_mix.inputs, "B_Color"))
    strength_mix = mix_color(5, 1, "MIX", "Shadow Strength")
    links.new(inp.outputs["Shadow Strength"], socket(strength_mix.inputs, "Factor_Float"))
    links.new(inp.outputs["Base Color"], socket(strength_mix.inputs, "A_Color"))
    links.new(socket(lit_mix.outputs, "Result_Color"), socket(strength_mix.inputs, "B_Color"))
    color = socket(strength_mix.outputs, "Result_Color")

    # --- MatCap: モード別に合成し、Compare で選ぶ ---
    branches = []
    for i, (blend, label) in enumerate((("MIX", "Normal"), ("ADD", "Add"), ("SCREEN", "Screen"), ("MULTIPLY", "Multiply"))):
        node = mix_color(6, 3 + i, blend, f"MatCap {label}")
        socket(node.inputs, "Factor_Float").default_value = 1.0
        links.new(color, socket(node.inputs, "A_Color"))
        links.new(inp.outputs["MatCap"], socket(node.inputs, "B_Color"))
        branches.append(socket(node.outputs, "Result_Color"))
    selected = branches[0]
    for i in (1, 2, 3):
        cmp = math(7, 3 + i, "COMPARE", f"mode == {i}", float(i))
        cmp.inputs[2].default_value = 0.5
        links.new(inp.outputs["MatCap Mode"], cmp.inputs[0])
        sel = mix_color(8, 3 + i, "MIX")
        links.new(cmp.outputs[0], socket(sel.inputs, "Factor_Float"))
        links.new(selected, socket(sel.inputs, "A_Color"))
        links.new(branches[i], socket(sel.inputs, "B_Color"))
        selected = socket(sel.outputs, "Result_Color")
    matcap_mix = mix_color(9, 3, "MIX", "MatCap Strength")
    links.new(inp.outputs["MatCap Strength"], socket(matcap_mix.inputs, "Factor_Float"))
    links.new(color, socket(matcap_mix.inputs, "A_Color"))
    links.new(selected, socket(matcap_mix.inputs, "B_Color"))
    color = socket(matcap_mix.outputs, "Result_Color")

    # --- リム: Facing を境界/ぼかしで 0..1 にし、色 × 強さを加算 ---
    layer = add("ShaderNodeLayerWeight", 4, 8, "Rim facing")
    layer.inputs["Blend"].default_value = 0.5
    links.new(inp.outputs["Normal"], layer.inputs["Normal"])
    rim_half = math(4, 9, "MULTIPLY", "rim blur/2", 0.5)
    links.new(inp.outputs["Rim Blur"], rim_half.inputs[0])
    rim_lo = math(5, 8, "SUBTRACT")
    links.new(inp.outputs["Rim Border"], rim_lo.inputs[0])
    links.new(rim_half.outputs[0], rim_lo.inputs[1])
    rim_hi = math(5, 9, "ADD")
    links.new(inp.outputs["Rim Border"], rim_hi.inputs[0])
    links.new(rim_half.outputs[0], rim_hi.inputs[1])
    rim_fac = add("ShaderNodeMapRange", 6, 8, "Rim factor")
    rim_fac.clamp = True
    links.new(layer.outputs["Facing"], rim_fac.inputs["Value"])
    links.new(rim_lo.outputs[0], rim_fac.inputs["From Min"])
    links.new(rim_hi.outputs[0], rim_fac.inputs["From Max"])
    rim_amount = math(7, 8, "MULTIPLY", "rim × strength")
    links.new(rim_fac.outputs[0], rim_amount.inputs[0])
    links.new(inp.outputs["Rim Strength"], rim_amount.inputs[1])
    rim_color = mix_color(8, 8, "MULTIPLY", "Rim Color × amount")
    socket(rim_color.inputs, "Factor_Float").default_value = 1.0
    links.new(inp.outputs["Rim Color"], socket(rim_color.inputs, "A_Color"))
    links.new(rim_amount.outputs[0], socket(rim_color.inputs, "B_Color"))
    rim_add = mix_color(10, 3, "ADD", "+ Rim")
    socket(rim_add.inputs, "Factor_Float").default_value = 1.0
    links.new(color, socket(rim_add.inputs, "A_Color"))
    links.new(socket(rim_color.outputs, "Result_Color"), socket(rim_add.inputs, "B_Color"))
    color = socket(rim_add.outputs, "Result_Color")

    # --- エミッション加算 ---
    emis_scaled = mix_color(9, 10, "MULTIPLY", "Emission × strength")
    socket(emis_scaled.inputs, "Factor_Float").default_value = 1.0
    links.new(inp.outputs["Emission"], socket(emis_scaled.inputs, "A_Color"))
    links.new(inp.outputs["Emission Strength"], socket(emis_scaled.inputs, "B_Color"))
    emis_add = mix_color(10, 5, "ADD", "+ Emission")
    emis_add.clamp_result = False
    socket(emis_add.inputs, "Factor_Float").default_value = 1.0
    links.new(color, socket(emis_add.inputs, "A_Color"))
    links.new(socket(emis_scaled.outputs, "Result_Color"), socket(emis_add.inputs, "B_Color"))
    color = socket(emis_add.outputs, "Result_Color")

    # --- 出力: Emission シェーダー + アルファで Transparent と Mix ---
    emit = add("ShaderNodeEmission", 11, 3, "Toon result")
    emit.inputs["Strength"].default_value = 1.0
    links.new(color, emit.inputs["Color"])
    transparent = add("ShaderNodeBsdfTransparent", 11, 4)
    mix_shader = add("ShaderNodeMixShader", 12, 4)
    links.new(inp.outputs["Alpha"], mix_shader.inputs[0])
    links.new(transparent.outputs[0], mix_shader.inputs[1])
    links.new(emit.outputs[0], mix_shader.inputs[2])
    out.location = (13 * 220, 4 * -160)
    links.new(mix_shader.outputs[0], out.inputs["Shader"])
