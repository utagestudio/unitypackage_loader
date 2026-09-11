"""3D View > Sidebar > Unity Package タブ。直近のインポート結果を表示する。"""

from __future__ import annotations

import bpy

from ..blender import importer

_MAX_WARNINGS = 12


class UNITYPKG_OT_copy_report(bpy.types.Operator):
    bl_idname = "unitypkg.copy_report"
    bl_label = "Copy Log"
    bl_description = "Copy the full import report to the clipboard"

    @classmethod
    def poll(cls, context):
        return importer.LAST_REPORT is not None

    def execute(self, context):
        context.window_manager.clipboard = importer.LAST_REPORT.as_text()
        self.report({"INFO"}, "Import report copied to clipboard")
        return {"FINISHED"}


class UNITYPKG_OT_open_extract_folder(bpy.types.Operator):
    bl_idname = "unitypkg.open_extract_folder"
    bl_label = "Open Extract Folder"
    bl_description = "Open the folder where textures were extracted"

    @classmethod
    def poll(cls, context):
        return importer.LAST_REPORT is not None and bool(importer.LAST_REPORT.extract_root)

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=importer.LAST_REPORT.extract_root)
        return {"FINISHED"}


class UNITYPKG_PT_report(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Unity Package"
    bl_label = "Unity Package Importer"

    def draw(self, context):
        layout = self.layout
        layout.operator("import_scene.unitypackage", text="Import Unity Package", icon="IMPORT")

        report = importer.LAST_REPORT
        if report is None:
            layout.label(text="No import in this session yet", icon="INFO")
            return

        box = layout.box()
        box.label(text=report.package, icon="PACKAGE")
        col = box.column(align=True)
        col.label(text=f"Objects: {len(report.objects)}")
        col.label(text=f"Materials: {len(report.materials)}  (mapped {report.mapped_count})")
        col.label(text=f"Textures: {len(report.images)}")
        if report.models:
            col.label(text=f"Models: {len(report.models)}")

        row = box.row(align=True)
        row.operator("unitypkg.open_extract_folder", icon="FILE_FOLDER")
        row.operator("unitypkg.copy_report", icon="COPYDOWN")


class UNITYPKG_PT_report_materials(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Unity Package"
    bl_label = "Materials"
    bl_parent_id = "UNITYPKG_PT_report"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return importer.LAST_REPORT is not None

    def draw(self, context):
        layout = self.layout
        report = importer.LAST_REPORT
        if not report.materials:
            layout.label(text="No materials were imported")
            return
        col = layout.column(align=True)
        for m in report.materials:
            row = col.row(align=True)
            if m.guid is None and m.method not in ("reused", "kept"):
                row.label(text=m.blender_name, icon="ERROR")
                row.label(text="unresolved")
            else:
                row.label(text=m.blender_name, icon="CHECKMARK" if not m.warnings else "INFO")
                detail = m.shader_name or m.family or m.method
                if m.alpha_mode and m.alpha_mode != "opaque":
                    detail += f" · {m.alpha_mode}"
                row.label(text=detail)


class UNITYPKG_PT_report_warnings(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Unity Package"
    bl_label = "Warnings"
    bl_parent_id = "UNITYPKG_PT_report"

    @classmethod
    def poll(cls, context):
        return importer.LAST_REPORT is not None and bool(importer.LAST_REPORT.warnings or importer.LAST_REPORT.errors)

    def draw_header(self, context):
        report = importer.LAST_REPORT
        self.layout.label(text=f"({len(report.warnings) + len(report.errors)})")

    def draw(self, context):
        layout = self.layout
        report = importer.LAST_REPORT
        col = layout.column(align=True)
        for e in report.errors:
            col.label(text=e, icon="CANCEL")
        for w in report.warnings[:_MAX_WARNINGS]:
            col.label(text=w, icon="ERROR")
        rest = len(report.warnings) - _MAX_WARNINGS
        if rest > 0:
            col.label(text=f"… and {rest} more (use Copy Log)")


class UNITYPKG_PT_tools(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Unity Package"
    bl_label = "Tools"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Outlines (selected meshes)")
        row = col.row(align=True)
        row.operator("unitypkg.add_outlines", text="Add", icon="MOD_SOLIDIFY")
        row.operator("unitypkg.remove_outlines", text="Remove", icon="X")


_classes = (
    UNITYPKG_OT_copy_report,
    UNITYPKG_OT_open_extract_folder,
    UNITYPKG_PT_report,
    UNITYPKG_PT_report_materials,
    UNITYPKG_PT_report_warnings,
    UNITYPKG_PT_tools,
)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
