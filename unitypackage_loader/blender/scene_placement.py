"""シーン 1 つの配置（``_ImportSession.import_scene``）。モデルの配置、組み込みメッシュ、デカール、ライト・カメラを置く。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import bpy
from mathutils import Matrix

from ..core.fbx_units import read_axes, read_unit_scale
from ..core.hierarchy import CLASS_CAMERA, CLASS_LIGHT, CLASS_TERRAIN, SOLE_NODE
from ..core.placement import built_from_renderers
from ..core.hierarchy import components as scene_components
from ..core.lights import LIGHT_CAMERA_BASIS, convert_camera, convert_light
from ..core.model_ids import PartMatch, PartMatcher
from ..core.prefab import RendererMaterials
from ..core.scene_import import PlacementStats, parts_to_hide, scene_warnings, unused_in_placement
from ..core.terrain import terrain_data_ref, terrain_local
from ..core.transform import inverse_affine, multiply, unity_to_blender
from ..core.unity_yaml import to_float
from ..core.units import SceneSummary
from .log import record_failure
from .model_import import node_name
from .unity_meshes import make_mesh_object
from .prepare import ModelSummary
from .scene_objects import (
    ModelUnits,
    SceneTemplate,
    apply_collapsed_root,
    apply_node_transforms,
    apply_offsets,
    apply_renderer_worlds,
    attach_to_empty,
    decal_mesh,
    defer_hide,
    duplicate_objects,
    json_text,
    make_camera,
    make_light,
    make_primitive,
    make_shadows_only,
    primitive_mesh,
    scene_empty,
)

if TYPE_CHECKING:
    from .importer import _ImportSession


class SceneImport:
    """シーン 1 つの読み込み（``_ImportSession.import_scene``）。

    以前は import_scene の 1 つのメソッド（約 210 行）が、配置ごとに使う表（Empty・テンプレート・FBX の単位の倍率など）と
    集計をローカル変数で持ち、組み込みメッシュ・デカール・ライト／カメラのそれぞれに「親の Empty とローカル行列を決める」
    同じ処理をコピーしていた。シーン 1 つ分の状態をこのクラスのフィールドにし、手順をメソッドに分けた（#198）。
    """

    def __init__(self, session: _ImportSession, scene_summary: SceneSummary):
        self.session = session
        self.opts = session.opts
        self.report = session.report
        self.summary = scene_summary
        self.pathname = scene_summary.pathname
        self.hierarchy = session.prepared.scene_hierarchies[scene_summary.guid]
        self.worlds, self.active = session.scene_frames(scene_summary.guid)
        self.target: bpy.types.Collection | None = None  # シーンのコレクション（run で作る）
        self.empties: dict[int, bpy.types.Object] = {}  # Node の key → その GameObject の Empty（か組み込みメッシュ）
        self.templates: dict[tuple, SceneTemplate] = {}  # 配置の key → 最初に読み込んだオブジェクト（以後は複製する）
        self.matchers: dict[tuple, PartMatcher | None] = {}  # 配置の key → 中のオブジェクトの fileID の計算（#172）
        self.removed_by_key: dict[tuple, int] = {}  # テンプレートを作るときに消した部品の数（以後の配置でも同じ数を数える）
        self.unit_scales: dict[str, ModelUnits | None] = {}  # モデルの GUID → FBX の単位と軸（FBX 以外は None）
        self.stats = PlacementStats()

    def run(self, progress_start: float, progress_span: float) -> None:
        self.target = bpy.data.collections.new(self.summary.name)
        self.session.collection.children.link(self.target)
        self.target["unity_scene"] = self.pathname
        self.target["unity_scene_guid"] = self.summary.guid
        self.report.scenes.append(self.pathname)

        count = len(self.summary.placements)
        for index, placement in enumerate(self.summary.placements):
            if index % 25 == 0:
                self.session.step(progress_start + progress_span * index / max(count, 1), f"Placing {index}/{count} in {self.summary.name}")
            self.place_model(placement)
        self.place_primitives()
        if self.opts.terrain_points is not None:
            self.stats.terrains_imported = True
            self.place_terrains()
        if self.opts.scene_decals:
            self.place_decals()
        self.place_lights_and_cameras()
        for message in scene_warnings(self.pathname, self.summary.contents, self.stats):
            self.report.warn(message)

    # --- 共通 ---

    def parent_and_local(self, key: int) -> tuple[bpy.types.Object | None, Matrix]:
        """Node の GameObject に置くオブジェクトの親と、親からの行列。

        同じ GameObject にモデルの配置の Empty（か組み込みメッシュ）があればその子にし、無ければ親の GameObject の Empty の
        子にして Node の行列で置く。
        """
        if key in self.empties:
            return self.empties[key], Matrix.Identity(4)
        node = self.hierarchy.nodes[key]
        parent = self.empty(node.parent) if node.parent is not None else None
        return parent, Matrix(unity_to_blender(node.local))

    def empty(self, key: int) -> bpy.types.Object:
        """Node の GameObject の Empty（祖先の Empty も必要なら作る）。"""
        return scene_empty(self.hierarchy, key, self.target, self.empties, self.active, self.session.hidden_objects)

    def link(self, obj: bpy.types.Object, parent, local: Matrix, visible: bool) -> None:
        self.target.objects.link(obj)
        obj.parent = parent
        obj.matrix_basis = local
        self.report.objects.append(obj.name)
        if not visible:
            defer_hide(obj, self.session.hidden_objects)

    def unit_scale(self, summary: ModelSummary) -> ModelUnits | None:
        """モデルの FBX の単位と軸（配置の計算の fileScale と軸の変換に使う。#53 / #240）。FBX 以外は None。"""
        if summary.guid not in self.unit_scales:
            path = self.session.paths[summary.guid]
            if summary.entry.ext == ".fbx":
                # Unity 4 のモデルは読み込みで Unity と同じ大きさにしてあるので、fileScale を m_GlobalScale にする（#233）
                unit = summary.info.unity_unit_scale(read_unit_scale(path))
                self.unit_scales[summary.guid] = ModelUnits(unit, read_axes(path), summary.info.global_scale)
            else:
                self.unit_scales[summary.guid] = None
        return self.unit_scales[summary.guid]

    # --- モデルの配置 ---

    def place_model(self, placement) -> None:
        """モデルの配置 1 つ。最初の配置は読み込み、同じモデル・同じ割り当ての以後の配置は複製する。"""
        session = self.session
        summary = session.supported_models.get(placement.model_guid)
        if summary is None:
            skipped = session.model_by_guid.get(placement.model_guid)
            if skipped is not None:
                self.report.warn(f"scene {self.pathname}: skipped {skipped.entry.pathname}: {skipped.skip_reason}")
            return
        if summary.guid in session.failed_models:
            return  # 読み込みに失敗したモデルの配置は飛ばす（エラーは最初の 1 回だけ記録する）
        root = self.empty(placement.root)
        key = self.template_key(placement)
        template = self.templates.get(key)
        if template is None:
            try:
                objects = self.import_template(summary, placement, key)
            except Exception as exc:  # noqa: BLE001 - そのモデルの配置だけを外して続ける（#70）
                record_failure(self.report, f"scene {self.pathname}: could not import {summary.entry.pathname}: {exc}")
                return
            template = self.templates[key]
        else:
            objects = duplicate_objects(template, self.target)
            self.report.objects.extend(o.name for o in objects)
        attach_to_empty(objects, root, summary.info.global_scale)
        self.apply_transforms(summary, placement, template, objects, root)
        self.hide_parts(placement, key, template, objects)

    def template_key(self, placement) -> tuple:
        """読み込み結果を使い回せる配置どうしで同じになる値。"""
        # 消した中のオブジェクトは signature に入っている（テンプレートから消すため）
        key = (placement.signature(), bool(placement.parts))
        if self.opts.skip_unused_parts:
            # 使われていない部品はテンプレートから消すので、それを決める値もテンプレートを使い回す条件に入れる
            mesh_ids = tuple(sorted(r.mesh_file_id for r in placement.renderers.values()))
            key = (key, built_from_renderers(self.hierarchy, placement), mesh_ids)
        return key

    def import_template(self, summary: ModelSummary, placement, key: tuple) -> list[bpy.types.Object]:
        """配置のモデルを読み込み、以後の配置で複製するテンプレートにする。"""
        session = self.session
        parts = placement.parts
        # メッシュ参照の fileID も渡す。GameObject の名前を変えてあると名前では引けないので、表を引くとき（``find_renderer``）に
        # オブジェクト名のハッシュと照合する（#213）
        table = {
            name: RendererMaterials(
                name, list(r.materials), r.renderer_class, placement.model_guid, r.mesh_file_id, r.lod_level
            )
            for name, r in placement.renderers.items()
        }
        objects = session.import_model(summary, table, self.target, with_parts=bool(parts))
        self.matchers[key] = session.last_matcher
        if self.opts.skip_unused_parts:
            # 最初に読み込んだものから消しておけば、以後の配置では必要な分だけを複製する（#152）。
            # シーン・prefab がモデルの中から消したオブジェクトも Unity に無いので、同じく消す（#172）
            unused = unused_in_placement(self.hierarchy, placement, [(node_name(o), o.type == "MESH") for o in objects])
            if parts and session.last_matcher is not None:
                unused |= session.last_matcher.match(parts).removed
            objects, self.removed_by_key[key] = session.remove_unused(objects, unused)
        self.templates[key] = SceneTemplate.capture(objects)
        return objects

    def apply_transforms(self, summary: ModelSummary, placement, template: SceneTemplate, objects, root) -> None:
        """モデルの中のノードへの位置の上書きと、Unity 上で動かした部品の置き直し。"""
        if placement.node_transforms:
            unit_scale = self.unit_scale(summary)
            overrides = dict(placement.node_transforms)
            root_node = placement.root_node
            if root_node == SOLE_NODE:
                # 表の無いモデル。オブジェクトが 1 つだけなら、Unity はそのノードをルートに畳んでいる（#111）。
                # そうでなければルートの Transform は単位行列が基準なので、展開した行列のままでよい
                overrides = {k: v for k, v in overrides.items() if k != SOLE_NODE}
                if len(objects) == 1 and not objects[0].children:
                    overrides[node_name(objects[0])] = placement.node_transforms[SOLE_NODE]
                    root_node = node_name(objects[0])
                else:
                    root_node = ""
            if root_node:  # 1 メッシュの FBX のモデルの PrefabInstance（#99）
                spec = overrides.pop(root_node, placement.node_transforms.get(root_node, {}))
                placed = apply_collapsed_root(template, objects, root, root_node, spec, unit_scale)
                self.stats.skipped_nodes += 0 if placed else 1
            if overrides:
                self.stats.skipped_nodes += apply_node_transforms(template, objects, overrides, unit_scale)
        if summary.entry.ext == ".fbx" and any(r.relative is not None for r in placement.renderers.values()):
            # Renderer ごとに Unity の位置と比べて置き直す（上書きで動いた部品も含む。#186）
            self.stats.skipped_offsets += apply_renderer_worlds(template, objects, root, placement.renderers, self.unit_scale(summary))
        elif placement.offsets:
            self.stats.skipped_offsets += apply_offsets(objects, placement.world, placement.offsets, summary.info.global_scale)

    def hide_parts(self, placement, key: tuple, template: SceneTemplate, objects) -> None:
        """配置に合わせて、使われていない部品・表示されていない部品・遠景用の LOD を隠し、数える。"""
        stats = self.stats
        # 照合はオブジェクトの FBX のノードとしての名前で行う。複製した配置のオブジェクトも、テンプレートと同じ名前を持つ（#237）
        hidden = parts_to_hide(self.hierarchy, placement, [(node_name(o), o.type == "MESH") for o in objects], self.opts.hide_lods)
        # モデルの中への表示の上書きと削除（#172）
        parts = placement.parts
        matcher = self.matchers.get(key)
        match = matcher.match(parts) if parts and matcher is not None else PartMatch()
        stats.unmatched_parts += match.unmatched
        present = {node_name(o) for o in objects}
        removed_here = match.removed & present  # 読み込まない設定でも、子孫を残すため残ったもの
        hide_here = (match.hidden & present) | removed_here
        shadows_here = (set(hidden.shadows_only) | match.shadows_only) - hide_here
        for obj in objects:
            name = node_name(obj)
            if name in hidden.lods:
                obj["unity_lod"] = hidden.lods[name]
            if name in hidden.names or name in hide_here:
                defer_hide(obj, self.session.hidden_objects)
            elif name in shadows_here and obj.type == "MESH":
                make_shadows_only(obj)  # Unity の Shadows Only（#232）
                stats.shadows_only += 1
        stats.hidden_unused += hidden.unused
        if placement.active:  # 消した部品は使われていない部品と同じく数える（非アクティブな配置は数えない）
            stats.hidden_unused += len(removed_here - hidden.names)
            stats.removed_unused += self.removed_by_key.get(key, 0)
        stats.hidden_lods += hidden.hidden_lods

    # --- モデル以外 ---

    def place_primitives(self) -> None:
        """Unity の組み込みメッシュ（Quad・Plane・Cube など）を使う Renderer（#190）。"""
        session = self.session
        primitives = self.summary.contents.primitives if self.summary.contents is not None else []
        session.meshes.prefetch({p.mesh_guid for p in primitives if p.mesh_guid})
        for primitive in primitives:
            node = self.hierarchy.nodes[primitive.key]
            parent, local = self.parent_and_local(primitive.key)
            if primitive.mesh_guid:  # パッケージの Unity の Mesh（#216）
                mesh = session.meshes.mesh(primitive.mesh_guid, primitive.mesh_file_id)
                if mesh is None:
                    continue
                obj = make_mesh_object(node.name, mesh, [session.materials.material_for([g]) for g in primitive.materials])
            else:
                obj = make_primitive(node.name, primitive_mesh(primitive.kind, session.primitive_meshes), session.materials.material_for(primitive.materials))
            obj["unity_game_object"] = node.name
            self.link(obj, parent, local, primitive.visible)
            if primitive.visible and primitive.shadows_only:
                make_shadows_only(obj)  # Unity の Shadows Only（Japanese Hotel Room の Quad_shadow。#232）
                self.stats.shadows_only += 1
            self.empties.setdefault(primitive.key, obj)  # 子の GameObject（ライトなど）はこの下に置く

    def place_terrains(self) -> None:
        """Terrain をメッシュにする（#205）。Terrain は回転・拡大縮小を受けないので、ワールドの位置だけで置く。"""
        session = self.session
        for component in scene_components(self.hierarchy, [CLASS_TERRAIN], self.worlds, self.active):
            guid = terrain_data_ref(component.body)
            if not guid:
                continue
            built = session.terrains.build(guid, self.pathname)
            if built is None:
                continue
            key = component.node
            node = self.hierarchy.nodes[key]
            # 親の空間で、ワールドでは位置だけの行列（world · terrain_local(world)）になるように置く
            world = self.worlds[key]
            placed = multiply(world, terrain_local(world))
            if key in self.empties:
                parent, frame = self.empties[key], world
            elif node.parent is not None:
                parent, frame = self.empty(node.parent), self.worlds[node.parent]
            else:
                parent, frame = None, None
            inverse = inverse_affine(frame) if frame is not None else None
            local = multiply(inverse, placed) if inverse is not None else placed
            obj = bpy.data.objects.new(node.name, built.mesh)
            obj["unity_game_object"] = node.name
            obj["unity_terrain"] = json_text(component.body)
            visible = component.active and to_float(component.body.get("m_DrawHeightmap"), 1.0) != 0
            self.link(obj, parent, Matrix(unity_to_blender(local)), visible)
            self.report.terrains += 1

    def place_decals(self) -> None:
        """DecalProjector を板にする（#165）。"""
        session = self.session
        for component, decal in session.scene_decals(self.summary):
            material = session.materials.decal_material_for(decal.material)
            if material is None:
                self.stats.skipped_decals += 1
                continue
            parent, local = self.parent_and_local(component.node)
            world = component.world
            lossy = tuple(math.sqrt(sum(world[i][j] ** 2 for i in range(3))) for j in range(3))
            obj = bpy.data.objects.new(f"{component.name} (Decal)", decal_mesh(decal, lossy, session.decal_meshes))
            obj.material_slots[0].link = "OBJECT"
            obj.material_slots[0].material = material
            obj["unity_decal"] = json_text(component.body)
            obj.visible_shadow = False  # 投影したデカールは影を落とさない
            self.link(obj, parent, local, component.active)
            self.report.decals += 1

    def place_lights_and_cameras(self) -> None:
        session = self.session
        prepared = session.prepared
        light_scale = session.light_scale(self.summary, self.target) if self.opts.scene_lights else 1.0
        wanted = ([CLASS_LIGHT] if self.opts.scene_lights else []) + ([CLASS_CAMERA] if self.opts.scene_cameras else [])
        for component in scene_components(self.hierarchy, wanted, self.worlds, self.active) if wanted else []:
            parent, local = self.parent_and_local(component.node)
            if component.class_id == CLASS_LIGHT:
                values = convert_light(component.body, prepared.pipeline)
                values.energy *= light_scale  # HDRP の露出の分（#130）
                obj = make_light(component.name, values)
                obj["unity_light"] = json_text(component.body)
                obj["unity_render_pipeline"] = prepared.pipeline
                obj["unity_render_pipeline_source"] = prepared.pipeline_source
                if light_scale != 1.0:
                    obj["unity_light_scale"] = light_scale
                self.stats.baked_lights += values.baked_only
                self.stats.light_notes.update(values.notes)
                self.report.lights += 1
            else:
                obj = make_camera(component.name, convert_camera(component.body))
                obj["unity_camera"] = json_text(component.body)
                self.report.cameras += 1
                if session.scene.camera is None and component.active:
                    session.scene.camera = obj
            # ライト・カメラにはスケールを掛けない。レポートの objects には載せない（数は lights / cameras）
            location, rotation, _ = (local @ Matrix(LIGHT_CAMERA_BASIS)).decompose()
            self.target.objects.link(obj)
            obj.parent = parent
            obj.matrix_basis = Matrix.LocRotScale(location, rotation, None)
            if not component.active:
                defer_hide(obj, session.hidden_objects)
