"""シーンの Unity Terrain をメッシュと塗り分けのマテリアルにする（Issue #205）。

TerrainData の読み取りと間引きは ``core/terrain.py``。ここでは、1 回の読み込みの中で TerrainData ごとにメッシュと
マテリアルを 1 度だけ作り（同じ TerrainData を使うシーンが複数あれば使い回す）、アルファマップを PNG にして画像として読む。

マテリアルは Unity の Terrain のシェーダーと同じ考え方で、レイヤーごとのテクスチャを Terrain 全体の UV にレイヤーの
倍率（大きさ / TileSize）を掛けた UV で読み、アルファマップのチャンネルを重みにして足し合わせる。

- 色: ディフューズ × ``m_DiffuseRemapMax``（色味）
- 金属感・つや: URP / HDRP でマスクマップがあれば、その R / A を ``m_MaskMapRemapMin``〜``Max`` に写した値。無ければ
  ``m_Metallic`` と「``m_Smoothness`` × ディフューズの A」（Built-in と URP の Terrain のシェーダーと同じ）
- 法線: 法線マップの色を重みで足し合わせて 1 つの Normal Map ノードに通す（強さは ``m_NormalScale`` の重み付きの和）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import bpy

from ..core.lights import PIPELINE_BUILTIN
from ..core.package import PackageError, extract_target, safe_relative_path
from ..core.terrain import (
    Alphamap,
    TerrainData,
    TerrainLayer,
    layer_uv_transform,
    png_rgba,
    read_terrain_data,
    read_terrain_layer,
    terrain_mesh,
)
from .materials import MODE_NAMES_ONLY, MODE_UNLIT
from .nodes import socket

if TYPE_CHECKING:
    from .importer import _ImportSession

TERRAIN_LAYER_EXT = ".terrainlayer"
_GRAY = (0.5, 0.5, 0.5, 1.0)
_FLAT_NORMAL = (0.5, 0.5, 1.0)


@dataclass
class BuiltTerrain:
    mesh: bpy.types.Mesh
    material: bpy.types.Material | None
    data: TerrainData


def layer_textures(pkg) -> set[str]:
    """パッケージの .terrainlayer が使うテクスチャの GUID（読み込む前に展開しておく分）。"""
    found: set[str] = set()
    for entry in pkg.entries.values():
        if entry.ext != TERRAIN_LAYER_EXT or not entry.has_asset:
            continue
        data = pkg.cached_asset(entry.guid)
        try:
            layer = read_terrain_layer(data, entry.guid) if data is not None else None
        except Exception:  # noqa: BLE001 - 読めない .terrainlayer は、組み立てのときに警告する
            layer = None
        if layer is not None:
            found.update(layer.textures())
    return found


class SessionTerrains:
    """1 回の読み込みで作った Terrain のメッシュとマテリアル（TerrainData の GUID ごと）。"""

    def __init__(self, session: _ImportSession):
        self.session = session
        self.built: dict[str, BuiltTerrain | None] = {}
        self._layers: dict[str, TerrainLayer | None] = {}

    def build(self, guid: str, pathname_for_warning: str) -> BuiltTerrain | None:
        if guid not in self.built:
            try:
                self.built[guid] = self._build(guid, pathname_for_warning)
            except (ValueError, PackageError, OSError) as exc:
                self.session.report.warn(f"scene {pathname_for_warning}: could not read Terrain data {self._name(guid)}: {exc}")
                self.built[guid] = None
        return self.built[guid]

    def _name(self, guid: str) -> str:
        entry = self.session.pkg.get(guid)
        return entry.pathname if entry is not None else guid

    def _build(self, guid: str, scene: str) -> BuiltTerrain | None:
        session = self.session
        report = session.report
        path = session.paths.get(guid)
        if path is None:
            report.warn(f"scene {scene}: Terrain data {guid} is not in the package; the Terrain was skipped")
            return None
        td = read_terrain_data(Path(path).read_bytes())
        prefix = f"Terrain data {self._name(guid)}: "
        for note in td.notes:
            report.warn(prefix + note)
        if td.trees:
            report.warn(prefix + f"{td.trees} tree(s) were not imported (Terrain trees are not supported)")
        if td.detail_prototypes:
            report.warn(prefix + f"{td.detail_prototypes} kind(s) of grass / detail were not imported (Terrain details are not supported)")

        points = session.opts.terrain_points or 0
        geometry = terrain_mesh(td, points)
        mesh = _make_mesh(td.name, geometry)
        mesh["unity_terrain_data_guid"] = guid
        mesh["unity_terrain_resolution"] = [td.columns, td.rows]
        if geometry.columns < td.columns or geometry.rows < td.rows:
            mesh["unity_terrain_thinned_to"] = [geometry.columns, geometry.rows]

        layers = self._layers_of(td, scene)
        material = self._material(guid, td, layers)
        if material is not None:
            mesh.materials.append(material)
        return BuiltTerrain(mesh, material, td)

    # --- レイヤー ---

    def _layer(self, guid: str) -> TerrainLayer | None:
        if guid not in self._layers:
            pkg = self.session.pkg
            data = pkg.cached_asset(guid)
            try:
                if data is None:
                    data = pkg.read_asset(guid)
                self._layers[guid] = read_terrain_layer(data, guid)
            except (KeyError, ValueError, PackageError):
                self._layers[guid] = None
        return self._layers[guid]

    def _layers_of(self, td: TerrainData, scene: str) -> list[TerrainLayer | None]:
        if td.legacy_layers and not td.layer_refs:
            return list(td.legacy_layers)
        layers = []
        for ref in td.layer_refs:
            layer = self._layer(ref) if ref else None
            if layer is None:
                self.session.report.warn(
                    f"scene {scene}: a Terrain layer of {td.name} is not in the package; that part is drawn in gray"
                )
            layers.append(layer)
        return layers

    # --- マテリアル ---

    def _material(self, guid: str, td: TerrainData, layers: list[TerrainLayer | None]) -> bpy.types.Material | None:
        session = self.session
        mat = bpy.data.materials.new(f"{td.name} (Terrain)")
        mat.use_nodes = True
        mat["unity_terrain_data_guid"] = guid
        mat["unity_terrain_layers"] = json.dumps([layer.as_dict() if layer else None for layer in layers])
        if session.opts.material_mode == MODE_NAMES_ONLY:
            return mat
        self._ensure_textures({g for layer in layers if layer for g in layer.textures()})
        alphamaps = self._alphamap_images(guid, td)
        _build_nodes(
            mat, td, layers, alphamaps, session.materials.images, session.prepared.pipeline,
            unlit=session.opts.material_mode == MODE_UNLIT, normal_maps=session.opts.use_normal_maps,
        )
        return mat

    def _ensure_textures(self, guids: set[str]) -> None:
        """レイヤーのテクスチャで、まだ読み込んでいないもの（読み込む前の予測から漏れたもの）を展開して読む。"""
        session = self.session
        images = session.materials.images
        missing = {g for g in guids if g not in images and session.pkg.get(g) is not None}
        if not missing:
            return
        written: set[str] = set()
        try:
            session.paths.update(
                session.pkg.extract(
                    sorted(missing), session.extract_root, overwrite=session.opts.overwrite_extracted,
                    max_total_size=session.opts.max_extract_size, written=written,
                )
            )
        except PackageError as exc:
            session.report.warn(f"could not extract Terrain textures: {exc}")
        session.materials.load_textures(missing, session.paths, written)

    def _alphamap_images(self, guid: str, td: TerrainData) -> list[bpy.types.Image | None]:
        session = self.session
        entry = session.pkg.get(guid)
        base = entry.pathname if entry is not None else f"Terrain/{guid}"
        refresh = guid in session.terrain_written
        result = []
        for alphamap in td.alphamaps:
            result.append(self._alphamap_image(base, alphamap, refresh))
        return result

    def _alphamap_image(self, base: str, alphamap: Alphamap, refresh: bool) -> bpy.types.Image | None:
        session = self.session
        try:
            target = extract_target(session.extract_root, safe_relative_path(f"{base}.{alphamap.name}.png"))
            if refresh or session.opts.overwrite_extracted or not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(png_rgba(alphamap.width, alphamap.height, alphamap.pixels))
                refresh = True
        except (ValueError, OSError, PackageError) as exc:
            session.report.warn(f"could not write the Terrain alphamap {alphamap.name}: {exc}")
            return None
        if refresh:
            for existing in bpy.data.images:
                if existing.filepath and not existing.packed_file and bpy.path.abspath(existing.filepath) == str(target):
                    existing.reload()
        image = bpy.data.images.load(str(target), check_existing=True)
        try:
            image.colorspace_settings.name = "Non-Color"
        except TypeError:
            pass
        image.alpha_mode = "CHANNEL_PACKED"  # A は 4 つ目のレイヤーの重み。RGB に掛けない
        image["unity_terrain_alphamap"] = alphamap.name
        if session.opts.pack_images and not image.packed_file:
            try:
                image.pack()
            except RuntimeError:
                pass
        session.report.images.append(image.name)
        return image


def _make_mesh(name: str, geometry) -> bpy.types.Mesh:
    import numpy as np

    mesh = bpy.data.meshes.new(name)
    vertices = np.frombuffer(geometry.vertices, dtype=np.float32)
    faces = np.frombuffer(geometry.faces, dtype=np.int32)
    count = len(vertices) // 3
    quads = len(faces) // 4
    mesh.vertices.add(count)
    mesh.vertices.foreach_set("co", vertices)
    mesh.loops.add(len(faces))
    mesh.loops.foreach_set("vertex_index", faces)
    mesh.polygons.add(quads)
    mesh.polygons.foreach_set("loop_start", np.arange(0, len(faces), 4, dtype=np.int32))
    mesh.polygons.foreach_set("loop_total", np.full(quads, 4, dtype=np.int32))
    uv_layer = mesh.uv_layers.new(name="UVMap")
    uvs = np.frombuffer(geometry.uvs, dtype=np.float32).reshape(-1, 2)
    uv_layer.data.foreach_set("uv", uvs[faces].ravel())
    mesh.update()
    mesh.validate(clean_customdata=False)
    mesh.shade_smooth()
    return mesh


class _Nodes:
    """ノードを列ごとに並べて置く小さな補助。"""

    def __init__(self, mat: bpy.types.Material):
        self.tree = mat.node_tree
        self.rows: dict[int, int] = {}

    def add(self, bl_idname: str, col: int, label: str = ""):
        node = self.tree.nodes.new(bl_idname)
        row = self.rows.get(col, 0)
        self.rows[col] = row + 1
        node.location = (col * 220 - 1400, 600 - row * 260)
        if label:
            node.label = label
        return node

    def link(self, a, b) -> None:
        self.tree.links.new(a, b)


class _WeightedSum:
    """Σ 重み × 値。値はソケットか定数。何も足さなければ ``output`` は None。"""

    def __init__(self, nodes: _Nodes, col: int, vector: bool, label: str):
        self.nodes, self.col, self.vector, self.label = nodes, col, vector, label
        self.output = None

    def add(self, weight, value=None, constant=None) -> None:
        n = self.nodes
        if self.vector:
            term = n.add("ShaderNodeVectorMath", self.col, f"{self.label} × weight")
            term.operation = "SCALE"
            if value is not None:
                n.link(value, term.inputs[0])
            else:
                term.inputs[0].default_value = constant
            n.link(weight, socket(term.inputs, "Scale"))
            out = term.outputs[0]
            if self.output is not None:
                total = n.add("ShaderNodeVectorMath", self.col + 1, f"{self.label} sum")
                total.operation = "ADD"
                n.link(self.output, total.inputs[0])
                n.link(out, total.inputs[1])
                out = total.outputs[0]
        else:
            if value is None and not constant:
                return
            term = n.add("ShaderNodeMath", self.col, f"{self.label} × weight + sum")
            term.operation = "MULTIPLY_ADD"
            n.link(weight, term.inputs[0])
            if value is not None:
                n.link(value, term.inputs[1])
            else:
                term.inputs[1].default_value = constant
            if self.output is not None:
                n.link(self.output, term.inputs[2])
            else:
                term.inputs[2].default_value = 0.0
            out = term.outputs[0]
        self.output = out


def _remap(n: _Nodes, value, low: float, high: float, col: int, label: str):
    """low + value × (high − low)。"""
    if abs(low) < 1e-6 and abs(high - 1.0) < 1e-6:
        return value
    node = n.add("ShaderNodeMath", col, label)
    node.operation = "MULTIPLY_ADD"
    n.link(value, node.inputs[0])
    node.inputs[1].default_value = high - low
    node.inputs[2].default_value = low
    return node.outputs[0]


def _build_nodes(mat, td: TerrainData, layers, alphamaps, images, pipeline: str, *, unlit: bool, normal_maps: bool) -> None:
    tree = mat.node_tree
    tree.nodes.clear()
    n = _Nodes(mat)
    out = n.add("ShaderNodeOutputMaterial", 12)
    uv = n.add("ShaderNodeUVMap", 0, "Terrain UV")
    uv.uv_map = "UVMap"

    # アルファマップごとに、RGB を分けて重みにする（A は画像ノードの Alpha）
    weights = []
    for index, image in enumerate(alphamaps):
        tex = n.add("ShaderNodeTexImage", 1, f"Alphamap {index}")
        tex.image = image
        tex.extension = "EXTEND"
        n.link(uv.outputs[0], tex.inputs[0])
        split = n.add("ShaderNodeSeparateColor", 2, f"Alphamap {index} RGB")
        n.link(tex.outputs["Color"], split.inputs[0])
        weights.extend([split.outputs[0], split.outputs[1], split.outputs[2], tex.outputs["Alpha"]])

    color = _WeightedSum(n, 7, True, "Color")
    metallic = _WeightedSum(n, 7, False, "Metallic")
    smoothness = _WeightedSum(n, 7, False, "Smoothness")
    normal = _WeightedSum(n, 9, True, "Normal")
    strength = _WeightedSum(n, 9, False, "Normal strength")
    uses_mask = pipeline != PIPELINE_BUILTIN
    any_normal = False
    for index, layer in enumerate(layers):
        if index >= len(weights):
            break
        weight = weights[index]
        if layer is None:
            color.add(weight, constant=_GRAY[:3])
            continue
        scale_u, scale_v, offset_u, offset_v = layer_uv_transform(layer, td.size)
        mapping = n.add("ShaderNodeMapping", 3, f"{layer.name} tiling")
        n.link(uv.outputs[0], mapping.inputs["Vector"])
        mapping.inputs["Scale"].default_value = (scale_u, scale_v, 1.0)
        mapping.inputs["Location"].default_value = (offset_u, offset_v, 0.0)

        diffuse = images.get(layer.diffuse) if layer.diffuse else None
        diffuse_node = None
        if diffuse is not None:
            diffuse_node = n.add("ShaderNodeTexImage", 4, f"{layer.name} diffuse")
            diffuse_node.image = diffuse
            n.link(mapping.outputs[0], diffuse_node.inputs[0])
            value = diffuse_node.outputs["Color"]
            tint = layer.diffuse_remap_max[:3]
            if any(abs(c - 1.0) > 1e-4 for c in tint):
                mul = n.add("ShaderNodeVectorMath", 5, f"{layer.name} tint")
                mul.operation = "MULTIPLY"
                n.link(value, mul.inputs[0])
                mul.inputs[1].default_value = tint
                value = mul.outputs[0]
            color.add(weight, value)
        else:
            color.add(weight, constant=_GRAY[:3])

        mask = images.get(layer.mask) if (layer.mask and uses_mask) else None
        if mask is not None:
            mask_node = n.add("ShaderNodeTexImage", 4, f"{layer.name} mask")
            mask_node.image = mask
            n.link(mapping.outputs[0], mask_node.inputs[0])
            split = n.add("ShaderNodeSeparateColor", 5, f"{layer.name} mask RGB")
            n.link(mask_node.outputs["Color"], split.inputs[0])
            low, high = layer.mask_remap_min, layer.mask_remap_max
            metallic.add(weight, _remap(n, split.outputs[0], low[0], high[0], 6, f"{layer.name} metallic"))
            smoothness.add(weight, _remap(n, mask_node.outputs["Alpha"], low[3], high[3], 6, f"{layer.name} smoothness"))
        else:
            metallic.add(weight, constant=layer.metallic)
            if diffuse_node is not None and layer.smoothness:
                mul = n.add("ShaderNodeMath", 6, f"{layer.name} smoothness × diffuse A")
                mul.operation = "MULTIPLY"
                n.link(diffuse_node.outputs["Alpha"], mul.inputs[0])
                mul.inputs[1].default_value = layer.smoothness
                smoothness.add(weight, mul.outputs[0])
            else:
                smoothness.add(weight, constant=layer.smoothness)

        normal_image = images.get(layer.normal) if (layer.normal and normal_maps) else None
        if normal_image is not None:
            normal_node = n.add("ShaderNodeTexImage", 4, f"{layer.name} normal")
            normal_node.image = normal_image
            n.link(mapping.outputs[0], normal_node.inputs[0])
            normal.add(weight, normal_node.outputs["Color"])
            any_normal = True
        else:
            normal.add(weight, constant=_FLAT_NORMAL)
        strength.add(weight, constant=layer.normal_scale)

    if unlit:
        shader = n.add("ShaderNodeEmission", 11, "Unlit")
        if color.output is not None:
            n.link(color.output, shader.inputs["Color"])
        else:
            shader.inputs["Color"].default_value = _GRAY
        n.link(shader.outputs[0], out.inputs["Surface"])
        return

    bsdf = n.add("ShaderNodeBsdfPrincipled", 11)
    if color.output is not None:
        n.link(color.output, bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = _GRAY
    if metallic.output is not None:
        n.link(metallic.output, bsdf.inputs["Metallic"])
    else:
        bsdf.inputs["Metallic"].default_value = 0.0
    if smoothness.output is not None:
        inv = n.add("ShaderNodeMath", 10, "Roughness = 1 - Smoothness")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        n.link(smoothness.output, inv.inputs[1])
        n.link(inv.outputs[0], bsdf.inputs["Roughness"])
    else:
        bsdf.inputs["Roughness"].default_value = 1.0
    if any_normal and normal.output is not None:
        normal_map = n.add("ShaderNodeNormalMap", 10, "Blended normal")
        normal_map.uv_map = "UVMap"
        n.link(normal.output, normal_map.inputs["Color"])
        if strength.output is not None:
            n.link(strength.output, normal_map.inputs["Strength"])
        n.link(normal_map.outputs[0], bsdf.inputs["Normal"])
    n.link(bsdf.outputs[0], out.inputs["Surface"])
