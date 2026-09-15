"""保存済みの Unity 情報からマテリアルを組み直す（モードの切り替え）。"""

from __future__ import annotations

import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty

from ..blender import materials as mat_builder
from ..ui.preferences import MATERIAL_MODE_ITEMS

_MODE_ITEMS = tuple(item for item in MATERIAL_MODE_ITEMS if item[0] != "NAMES_ONLY")


def _target_materials(context, scope: str) -> list[bpy.types.Material]:
    if scope == "ACTIVE":
        obj = context.active_object
        mat = obj.active_material if obj else None
        return [mat] if mat is not None else []
    seen: dict[str, bpy.types.Material] = {}
    for obj in context.selected_objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material is not None:
                seen[slot.material.name] = slot.material
    return list(seen.values())


class UNITYPKG_OT_rebuild_material(bpy.types.Operator):
    bl_idname = "unitypkg.rebuild_material"
    bl_label = "Rebuild Unity Material"
    bl_description = "Rebuild the material nodes from the stored Unity data in another mode (no package needed)"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(name="Material Mode", items=_MODE_ITEMS, default="AUTO")
    scope: EnumProperty(
        name="Apply To",
        items=(
            ("SELECTED", "Selected Objects", "All materials on the selected meshes"),
            ("ACTIVE", "Active Material", "Only the active object's active material"),
        ),
        default="SELECTED",
    )
    force_opaque: BoolProperty(name="Force Opaque", default=False)
    use_normal_maps: BoolProperty(name="Normal Maps", default=True)
    use_emission: BoolProperty(name="Emission", default=True)

    @classmethod
    def poll(cls, context):
        return any(m.get("unity_normalized") for m in _target_materials(context, "SELECTED")) or (
            context.active_object is not None
            and context.active_object.active_material is not None
            and context.active_object.active_material.get("unity_normalized")
        )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "mode")
        layout.prop(self, "scope")
        layout.prop(self, "force_opaque")
        layout.prop(self, "use_normal_maps")
        layout.prop(self, "use_emission")

    def execute(self, context):
        opts = mat_builder.MaterialBuildOptions(
            mode=self.mode,
            force_opaque=self.force_opaque,
            use_normal_maps=self.use_normal_maps,
            use_emission=self.use_emission,
        )
        done, skipped, failed = 0, 0, []
        for mat in _target_materials(context, self.scope):
            if not mat.get("unity_normalized"):
                skipped += 1
                continue
            try:
                mat_builder.rebuild_from_props(mat, self.mode, opts)
                done += 1
            except Exception as exc:  # noqa: BLE001 - 1 つの失敗で残りの再構築を止めない
                traceback.print_exc()
                failed.append(f"{mat.name}: {exc!r}")
        for f in failed:
            print("[Unitypackage Importer] rebuild failed:", f)
        msg = f"Rebuilt {done} material(s)"
        if skipped:
            msg += f", skipped {skipped} without Unity data"
        if failed:
            msg += f", {len(failed)} failed (see console)"
        self.report({"WARNING" if failed else "INFO"}, msg)
        return {"FINISHED"} if done else {"CANCELLED"}


_classes = (UNITYPKG_OT_rebuild_material,)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
