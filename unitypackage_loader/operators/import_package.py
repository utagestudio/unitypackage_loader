"""File > Import > Unity Package オペレーター。"""

from __future__ import annotations

import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from ..core.package import PackageError
from ..ui.preferences import EXTRACT_MODE_ITEMS, MATERIAL_MODE_ITEMS, MODELS_ITEMS, get_prefs
from . import select_models


class IMPORT_SCENE_OT_unitypackage(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.unitypackage"
    bl_label = "Import Unity Package"
    bl_description = "Import meshes, materials and textures from a .unitypackage"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".unitypackage"
    filter_glob: StringProperty(default="*.unitypackage", options={"HIDDEN"})

    # --- Model ---
    models: EnumProperty(name="Models", items=MODELS_ITEMS, default="ASK")
    fbx_importer: EnumProperty(
        name="FBX Importer",
        items=(
            ("AUTO", "Auto", "Use the new FBX importer when available"),
            ("NEW", "New (C++)", "bpy.ops.wm.fbx_import"),
            ("LEGACY", "Legacy (Python)", "bpy.ops.import_scene.fbx"),
        ),
        default="AUTO",
    )
    global_scale: FloatProperty(name="Scale", default=1.0, min=0.001, max=1000.0)
    use_anim: BoolProperty(name="Import Animation", default=False)
    ignore_leaf_bones: BoolProperty(name="Ignore Leaf Bones", default=True)

    # --- Materials ---
    material_mode: EnumProperty(name="Material Mode", items=MATERIAL_MODE_ITEMS, default="AUTO")
    force_opaque: BoolProperty(
        name="Force Opaque", default=False, description="Ignore Unity cutout/transparent settings"
    )
    backface_culling: BoolProperty(name="Backface Culling", default=True, description="Follow Unity's _Cull setting")
    use_normal_maps: BoolProperty(name="Normal Maps", default=True)
    use_emission: BoolProperty(name="Emission", default=True)
    reuse_existing: BoolProperty(
        name="Reuse Existing Materials",
        default=False,
        description="If a material with the same name already exists in the file, use it instead of building a new one",
    )
    store_props: BoolProperty(
        name="Store Unity Properties",
        default=True,
        description="Save GUIDs and unmapped shader values as custom properties on the material",
    )

    # --- Textures ---
    extract_mode: EnumProperty(name="Extract To", items=EXTRACT_MODE_ITEMS, default="BESIDE_BLEND")
    extract_path: StringProperty(name="Path", subtype="DIR_PATH", default="")
    pack_images: BoolProperty(name="Pack Into .blend", default=False)
    import_unreferenced: BoolProperty(
        name="Import Unreferenced Images",
        default=False,
        description="Also load textures that no material references (masks etc.)",
    )
    overwrite_extracted: BoolProperty(name="Overwrite Extracted Files", default=False)

    # Preferences の既定値を反映するプロパティ（ユーザーが明示的に変えたものは上書きしない）
    _PREF_DEFAULTS = {
        "models": "default_models",
        "material_mode": "default_material_mode",
        "extract_mode": "default_extract_mode",
        "extract_path": "default_extract_path",
        "import_unreferenced": "default_import_unreferenced",
    }

    def invoke(self, context, event):
        prefs = get_prefs(context)
        if prefs is not None:
            for prop, pref in self._PREF_DEFAULTS.items():
                if not self.properties.is_property_set(prop):
                    setattr(self, prop, getattr(prefs, pref))
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Model", icon="MESH_DATA")
        box.prop(self, "models")
        box.prop(self, "fbx_importer")
        box.prop(self, "global_scale")
        box.prop(self, "use_anim")
        box.prop(self, "ignore_leaf_bones")

        box = layout.box()
        box.label(text="Materials", icon="MATERIAL")
        box.prop(self, "material_mode")
        sub = box.column()
        sub.active = self.material_mode != "NAMES_ONLY"
        sub.prop(self, "force_opaque")
        sub.prop(self, "backface_culling")
        sub.prop(self, "use_normal_maps")
        sub.prop(self, "use_emission")
        box.prop(self, "reuse_existing")
        box.prop(self, "store_props")

        box = layout.box()
        box.label(text="Textures", icon="TEXTURE")
        box.prop(self, "extract_mode")
        if self.extract_mode == "CUSTOM":
            box.prop(self, "extract_path")
        box.prop(self, "pack_images")
        box.prop(self, "import_unreferenced")
        box.prop(self, "overwrite_extracted")

    def execute(self, context):
        from ..blender.importer import ImportOptions, build_shader_table, prepare_package, run_import

        prefs = get_prefs(context)
        opts = ImportOptions(
            models=self.models,
            material_mode=self.material_mode,
            force_opaque=self.force_opaque,
            backface_culling=self.backface_culling,
            use_normal_maps=self.use_normal_maps,
            use_emission=self.use_emission,
            reuse_existing=self.reuse_existing,
            store_props=self.store_props,
            extract_mode=self.extract_mode,
            extract_path=self.extract_path,
            pack_images=self.pack_images,
            import_unreferenced=self.import_unreferenced,
            overwrite_extracted=self.overwrite_extracted,
            fbx_importer=self.fbx_importer,
            use_anim=self.use_anim,
            ignore_leaf_bones=self.ignore_leaf_bones,
            global_scale=self.global_scale,
            shader_table_path=prefs.shader_table_path if prefs else "",
        )
        wm = context.window_manager
        wm.progress_begin(0, 100)
        try:
            prepared = prepare_package(self.filepath, build_shader_table(opts.shader_table_path))
            if opts.models == "ASK" and len(prepared.models) > 1:
                select_models.set_pending(self.filepath, opts, prepared)
                return bpy.ops.import_scene.unitypackage_select("INVOKE_DEFAULT")
            report = run_import(
                context,
                self.filepath,
                opts,
                progress=lambda f, msg: wm.progress_update(int(f * 100)),
                prepared=prepared,
            )
        except PackageError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, f"Import failed: {exc!r}")
            return {"CANCELLED"}
        finally:
            wm.progress_end()

        if prefs is None or prefs.verbose_log:
            print(report.as_text())
        level = "WARNING" if report.warnings else "INFO"
        self.report({level}, report.summary())
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
