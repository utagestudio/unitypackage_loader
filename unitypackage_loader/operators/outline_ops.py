"""選択オブジェクトに対するアウトラインの追加・削除。"""

from __future__ import annotations

import bpy
from bpy.props import FloatProperty

from ..blender import outline


class UNITYPKG_OT_add_outlines(bpy.types.Operator):
    bl_idname = "unitypkg.add_outlines"
    bl_label = "Add Unity Outlines"
    bl_description = "Add a Solidify outline to selected meshes using the Unity material's outline color and width"
    bl_options = {"REGISTER", "UNDO"}

    width_scale: FloatProperty(
        name="Width Scale",
        default=outline.DEFAULT_WIDTH_SCALE,
        min=0.0,
        soft_max=0.1,
        precision=4,
        description="Multiplier from the Unity outline width to metres",
    )

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        count = outline.apply_outlines(context.selected_objects, self.width_scale)
        if count == 0:
            self.report({"WARNING"}, "No selected mesh has a Unity material with outline settings")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Added outlines to {count} object(s)")
        return {"FINISHED"}


class UNITYPKG_OT_remove_outlines(bpy.types.Operator):
    bl_idname = "unitypkg.remove_outlines"
    bl_label = "Remove Unity Outlines"
    bl_description = "Remove outlines added by this add-on from selected meshes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(o.modifiers.get(outline.MODIFIER_NAME) for o in context.selected_objects)

    def execute(self, context):
        count = sum(1 for o in context.selected_objects if outline.remove_outline(o))
        self.report({"INFO"}, f"Removed outlines from {count} object(s)")
        return {"FINISHED"}


_classes = (UNITYPKG_OT_add_outlines, UNITYPKG_OT_remove_outlines)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
