"""File > Import > Unity Package オペレーター（雛形）。"""

from __future__ import annotations

import bpy
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper


class IMPORT_SCENE_OT_unitypackage(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.unitypackage"
    bl_label = "Import Unity Package"
    bl_description = "Import meshes, materials and textures from a .unitypackage"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".unitypackage"
    filter_glob: StringProperty(default="*.unitypackage", options={"HIDDEN"})

    def execute(self, context):
        self.report({"INFO"}, f"Selected: {self.filepath}")
        return {"FINISHED"}


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_unitypackage.bl_idname, text="Unity Package (.unitypackage)")


_classes = (IMPORT_SCENE_OT_unitypackage,)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
