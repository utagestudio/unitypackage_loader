"""アドオン Preferences。インポートオプションの既定値と追加のシェーダー GUID 表を持つ。"""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

_ROOT_PACKAGE = __package__.rsplit(".", 1)[0]

MATERIAL_MODE_ITEMS = (
    ("AUTO", "Auto", "Toon node group for toon shaders (lilToon etc.), Principled BSDF for PBR shaders"),
    ("PRINCIPLED", "Principled BSDF", "Always build a Principled BSDF material"),
    ("TOON", "Toon (Node Group)", "UnityToon node group: shadow color, MatCap, rim light and emission (EEVEE)"),
    ("UNLIT", "Unlit (Emission)", "Texture straight into an Emission shader, no lighting"),
    ("NAMES_ONLY", "Names Only", "Keep the FBX importer's materials, only attach Unity metadata"),
)
EXTRACT_MODE_ITEMS = (
    ("BESIDE_BLEND", "Beside .blend", "<blend dir>/textures/<package>/ (falls back to cache if unsaved)"),
    ("CACHE", "Add-on Cache", "The extension's user cache directory"),
    ("CUSTOM", "Custom Path", "The directory given below"),
)
MODELS_ITEMS = (
    ("ASK", "Ask", "Show a selection dialog when the package contains more than one model"),
    ("ALL", "All", "Import every model in the package"),
    ("FIRST", "First Only", "Import only the first model found"),
)


class UNITYPKG_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = _ROOT_PACKAGE

    default_models: EnumProperty(name="Models", items=MODELS_ITEMS, default="ASK")
    default_material_mode: EnumProperty(name="Material Mode", items=MATERIAL_MODE_ITEMS, default="AUTO")
    default_extract_mode: EnumProperty(name="Extract To", items=EXTRACT_MODE_ITEMS, default="BESIDE_BLEND")
    default_extract_path: StringProperty(name="Custom Path", subtype="DIR_PATH", default="")
    default_import_unreferenced: BoolProperty(name="Import Unreferenced Images", default=False)
    shader_table_path: StringProperty(
        name="Extra Shader Table",
        subtype="FILE_PATH",
        default="",
        description="JSON file with the same layout as shader_guids.json; entries are added to the built-in table",
    )
    max_extract_mb: IntProperty(
        name="Max Extract Size (MB)",
        default=8192,
        min=0,
        soft_max=65536,
        description=(
            "Refuse to import when the files to extract from one package would exceed this total size "
            "(protects against packages that hide huge files behind gzip). 0 = no limit"
        ),
    )
    verbose_log: BoolProperty(
        name="Verbose Console Log",
        default=True,
        description="Print the full import report to the system console",
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        col = layout.column(heading="Import Defaults")
        col.prop(self, "default_models")
        col.prop(self, "default_material_mode")
        col.prop(self, "default_extract_mode")
        if self.default_extract_mode == "CUSTOM":
            col.prop(self, "default_extract_path")
        col.prop(self, "default_import_unreferenced")

        col = layout.column(heading="Shaders")
        col.prop(self, "shader_table_path")

        col = layout.column(heading="Safety")
        col.prop(self, "max_extract_mb")

        col = layout.column(heading="Logging")
        col.prop(self, "verbose_log")

        row = layout.row()
        row.operator("unitypkg.open_cache_folder", icon="FILE_FOLDER")


class UNITYPKG_OT_open_cache_folder(bpy.types.Operator):
    bl_idname = "unitypkg.open_cache_folder"
    bl_label = "Open Cache Folder"
    bl_description = "Open the extension's texture cache directory in the system file browser"

    def execute(self, context):
        from ..blender.importer import cache_root

        bpy.ops.wm.path_open(filepath=str(cache_root()))
        return {"FINISHED"}


def get_prefs(context) -> UNITYPKG_AddonPreferences | None:
    addon = context.preferences.addons.get(_ROOT_PACKAGE)
    return addon.preferences if addon else None


_classes = (UNITYPKG_AddonPreferences, UNITYPKG_OT_open_cache_folder)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
