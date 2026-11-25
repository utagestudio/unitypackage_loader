"""File > Import > Unitypackage オペレーター。"""

from __future__ import annotations

import traceback
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from ..core.package import PackageError
from ..core.report import describe_exception, sanitize_display
from ..blender import log
from ..ui.preferences import (
    ARRANGE_ITEMS, EXTRACT_MODE_ITEMS, MATERIAL_MODE_ITEMS, MODELS_ITEMS, TERRAIN_ITEMS, UNIT_ITEMS, get_prefs,
)
from . import select_models
from .common import options_from_operator, report_level


class IMPORT_SCENE_OT_unitypackage(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.unitypackage"
    bl_label = "Import Unitypackage"
    bl_description = "Import meshes, materials and textures from a .unitypackage"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".unitypackage"
    filter_glob: StringProperty(default="*.unitypackage", options={"HIDDEN"})
    # 複数選択（一括インポート）
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})

    # --- Model ---
    models: EnumProperty(name="Models", items=MODELS_ITEMS, default="ASK")
    # 読み込む単位と並べ方。通常はモデル選択ダイアログで選ぶ。スクリプトやダイアログを出さないときに使う
    unit: EnumProperty(name="Import Unit", items=UNIT_ITEMS, default="MODELS", options={"HIDDEN", "SKIP_SAVE"})
    arrange: EnumProperty(name="Arrange", items=ARRANGE_ITEMS, default="SIDE_BY_SIDE", options={"HIDDEN", "SKIP_SAVE"})
    scene_lights: BoolProperty(name="Scene Lights", default=True, options={"HIDDEN", "SKIP_SAVE"})
    scene_cameras: BoolProperty(name="Scene Cameras", default=True, options={"HIDDEN", "SKIP_SAVE"})
    scene_exposure: BoolProperty(name="Scene Exposure", default=True, options={"HIDDEN", "SKIP_SAVE"})
    scene_decals: BoolProperty(name="Scene Decals", default=True, options={"HIDDEN", "SKIP_SAVE"})
    scene_terrain: EnumProperty(name="Scene Terrain", items=TERRAIN_ITEMS, default="513", options={"HIDDEN", "SKIP_SAVE"})
    # 入れ子のパッケージのうち読むものの pathname（#217）。空なら、選ぶダイアログを出すか Built-in らしいものを読む
    nested_package: StringProperty(name="Nested Package", default="", options={"HIDDEN", "SKIP_SAVE"})
    hide_lods: BoolProperty(
        name="Hide Lower LODs",
        default=True,
        description="Hide the distant LOD levels of Unity LODGroups (scenes and prefabs)",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    skip_unused_parts: BoolProperty(
        name="Skip Unused Parts",
        default=True,
        description="Do not import the parts of a model file that the Unity prefabs or scene do not use (scenes and prefabs)",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    fbx_importer: EnumProperty(
        name="FBX Importer",
        items=(
            ("AUTO", "Auto", "Use the new FBX importer"),
            ("NEW", "New (C++)", "bpy.ops.wm.fbx_import"),
            ("LEGACY", "Legacy (Python)", "bpy.ops.import_scene.fbx"),
        ),
        default="AUTO",
    )
    global_scale: FloatProperty(name="Scale", default=1.0, min=0.001, max=1000.0)
    use_vrm_addon: BoolProperty(
        name="VRM via VRM Add-on",
        default=True,
        description=(
            "Import .vrm models with the 'VRM format' add-on when it is installed "
            "(MToon, humanoid and spring bones are reproduced by the add-on). "
            "Otherwise the glTF importer is used and materials are rebuilt from .mat files"
        ),
    )
    use_anim: BoolProperty(name="Import Animation", default=False)
    import_clips: BoolProperty(
        name="Animation Clips",
        default=True,
        description=(
            "Turn the package's .anim files (blend shapes, object visibility and transforms) into actions, "
            "added as muted NLA tracks on the objects they animate. Humanoid clips and bone curves are skipped"
        ),
    )
    ignore_leaf_bones: BoolProperty(name="Ignore Leaf Bones", default=True)
    import_blend: BoolProperty(
        name="Import Bundled .blend Files",
        default=False,
        description=(
            "Append objects from .blend files inside the package. A .blend can contain Python scripts "
            "(drivers, registered text blocks) that run when 'Auto Run Python Scripts' is enabled; "
            "turn this on only for packages you trust"
        ),
    )

    # --- Materials ---
    material_mode: EnumProperty(name="Material Mode", items=MATERIAL_MODE_ITEMS, default="AUTO")
    force_opaque: BoolProperty(
        name="Force Opaque", default=False, description="Ignore Unity cutout/transparent settings"
    )
    # 既定は OFF（Unity は軽さのために背面を描かないが、Blender では裏側が欠けて見えるだけのことが多い。#169）
    backface_culling: BoolProperty(
        name="Backface Culling",
        default=False,
        description="Hide back faces where Unity's _Cull setting does (off: draw both sides of every material)",
    )
    use_normal_maps: BoolProperty(name="Normal Maps", default=True)
    use_emission: BoolProperty(name="Emission", default=True)
    reuse_existing: BoolProperty(
        name="Reuse Existing Materials",
        default=False,
        description="If a material with the same name already exists in the file, use it instead of building a new one",
    )
    outlines: BoolProperty(
        name="Outlines (Solidify)",
        default=False,
        description="Add a Solidify outline to meshes whose Unity material has outline settings",
    )
    outline_width_scale: FloatProperty(name="Outline Width Scale", default=0.01, min=0.0, soft_max=0.1, precision=4)
    blend_materials: EnumProperty(
        name="Bundled .blend Materials",
        items=(
            ("KEEP", "Keep", "Leave materials from bundled .blend files untouched (only attach Unity metadata)"),
            ("REBUILD", "Rebuild", "Rebuild them from the Unity .mat like other models"),
        ),
        default="KEEP",
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
        description="Also load the package's other images (masks, textures of materials the imported items don't use)",
    )
    overwrite_extracted: BoolProperty(name="Overwrite Extracted Files", default=False)

    # Preferences の既定値を反映するプロパティ（ユーザーが明示的に変えたものは上書きしない）
    _PREF_DEFAULTS = {
        "models": "default_models",
        "material_mode": "default_material_mode",
        "extract_mode": "default_extract_mode",
        "extract_path": "default_extract_path",
        "import_unreferenced": "default_import_unreferenced",
        "backface_culling": "default_backface_culling",
    }

    def invoke(self, context, event):
        prefs = get_prefs(context)
        if prefs is not None:
            for prop, pref in self._PREF_DEFAULTS.items():
                if not self.properties.is_property_set(prop):
                    setattr(self, prop, getattr(prefs, pref))
        # ドラッグ＆ドロップ（FileHandler 経由）で filepath が既に入っていればオプションのポップアップ、
        # メニューからならファイルブラウザを開く。ImportHelper.invoke_popup と同じだが、タイトルはファイル名だけにする
        # （フルパスをタイトルにすると、深いフォルダのパッケージでダイアログが横に伸びる。#211）
        if self.properties.is_property_set("filepath"):
            return context.window_manager.invoke_props_dialog(
                self, confirm_text="Import", title=Path(self.filepath).name, translate=False
            )
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        # よく変える項目だけを常に出し、残りは折りたたむ。何を読み込むか（prefab / モデル）は次のダイアログで選ぶ。
        # 「Models」（選択ダイアログを出すか）は Preferences の既定値に任せ、ここには出さない
        col = layout.column()
        col.prop(self, "material_mode")
        col.prop(self, "global_scale")

        header, body = layout.panel("UNITYPKG_import_model", default_closed=True)
        header.label(text="Model", icon="MESH_DATA")
        if body:
            body.prop(self, "fbx_importer")
            body.prop(self, "use_anim")
            body.prop(self, "import_clips")
            body.prop(self, "ignore_leaf_bones")
            body.prop(self, "use_vrm_addon")
            body.prop(self, "import_blend")
        if self.import_blend:
            # 閉じたパネルの中で ON になっていても（プリセットなど）見落とさないよう、開閉によらず出す
            warn = (body or layout).column(align=True)
            warn.label(text="Bundled .blend files may contain Python scripts.", icon="ERROR")
            warn.label(text="Enable only for packages you trust.")

        header, body = layout.panel("UNITYPKG_import_materials", default_closed=True)
        header.label(text="Materials", icon="MATERIAL")
        if body:
            sub = body.column()
            sub.active = self.material_mode != "NAMES_ONLY"
            sub.prop(self, "force_opaque")
            sub.prop(self, "backface_culling")
            sub.prop(self, "use_normal_maps")
            sub.prop(self, "use_emission")
            sub.prop(self, "outlines")
            if self.outlines:
                sub.prop(self, "outline_width_scale")
            body.prop(self, "reuse_existing")
            sub = body.column()
            sub.active = self.import_blend
            sub.prop(self, "blend_materials")
            body.prop(self, "store_props")

        header, body = layout.panel("UNITYPKG_import_textures", default_closed=True)
        header.label(text="Textures", icon="TEXTURE")
        if body:
            body.prop(self, "extract_mode")
            if self.extract_mode == "CUSTOM":
                body.prop(self, "extract_path")
            body.prop(self, "pack_images")
            body.prop(self, "import_unreferenced")
            body.prop(self, "overwrite_extracted")

    def _selected_paths(self) -> list[str]:
        if self.files and self.directory:
            paths = [str(Path(self.directory) / f.name) for f in self.files if f.name]
            if paths:
                return paths
        return [self.filepath] if self.filepath else []

    def execute(self, context):
        from ..blender.importer import (
            ImportOptions, build_shader_table, default_nested_package, extract_nested_package, prepare_package, run_import,
        )
        from ..core.package import UnityPackage
        from ..core.report import ImportReport

        prefs = get_prefs(context)
        log.set_verbose(prefs is None or prefs.verbose_log)
        paths = self._selected_paths()
        if not paths:
            self.report({"ERROR"}, "No package selected")
            return {"CANCELLED"}
        batch = len(paths) > 1

        def make_opts() -> ImportOptions:
            # 一括インポート時はダイアログを出さず全モデルを読む
            return options_from_operator(self, prefs, models="ALL" if (batch and self.models == "ASK") else self.models)

        wm = context.window_manager
        wm.progress_begin(0, 100)
        reports: list[ImportReport] = []
        failures: list[str] = []
        try:
            for index, path in enumerate(paths):
                opts = make_opts()
                base = index / len(paths)
                span = 1.0 / len(paths)
                try:
                    pkg = UnityPackage(path)
                    pkg.scan()
                    nested = pkg.nested_packages()
                    if nested:
                        # 外側に読み込めるものが無く、中に .unitypackage がある（パイプライン別の中身。#217）
                        chosen = next((e for e in nested if e.pathname == self.nested_package), None)
                        if chosen is None and len(nested) > 1 and not batch and not bpy.app.background:
                            set_nested_pending(path, opts, pkg, nested)
                            return bpy.ops.import_scene.unitypackage_nested("INVOKE_DEFAULT")
                        chosen = chosen or default_nested_package(nested)
                        path, pkg = str(extract_nested_package(pkg, chosen, opts, Path(path))), None
                    prepared = prepare_package(
                        path, build_shader_table(opts.shader_table_path), import_blend=opts.import_blend, pkg=pkg
                    )
                    ask = opts.models == "ASK" and not bpy.app.background
                    if ask and prepared.has_choices:
                        select_models.set_pending(path, opts, prepared)
                        return bpy.ops.import_scene.unitypackage_select("INVOKE_DEFAULT")
                    report = run_import(
                        context,
                        path,
                        opts,
                        progress=lambda f, msg, base=base, span=span: wm.progress_update(int((base + span * f) * 100)),
                        prepared=prepared,
                    )
                    reports.append(report)
                except PackageError as exc:
                    # 例外文にはパッケージ由来のパスが入るので表示前に無害化する
                    failures.append(sanitize_display(f"{Path(path).name}: {exc}"))
                    if not batch:
                        self.report({"ERROR"}, sanitize_display(str(exc)))
                        return {"CANCELLED"}
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    failures.append(sanitize_display(f"{Path(path).name}: {describe_exception(exc)}"))
                    if not batch:
                        self.report({"ERROR"}, sanitize_display(f"Import failed: {describe_exception(exc)}"))
                        return {"CANCELLED"}
        finally:
            wm.progress_end()

        if log.verbose():
            for report in reports:
                print(report.as_text())
            for f in failures:
                print("[Unitypackage Importer] failed:", f)

        if not reports:
            self.report({"ERROR"}, "; ".join(failures) or "Nothing imported")
            return {"CANCELLED"}
        if batch:
            objects = sum(len(r.objects) for r in reports)
            materials = sum(len(r.materials) for r in reports)
            mapped = sum(r.mapped_count for r in reports)
            warnings = sum(len(r.warnings) + len(r.errors) for r in reports) + len(failures)
            summary = f"Imported {len(reports)} packages: {objects} objects, {materials} materials ({mapped} mapped)"
            if failures:
                summary += f", {len(failures)} package(s) failed"
            if warnings:
                summary += f". {warnings} warning(s) — see the system console"
            self.report({"WARNING" if warnings else "INFO"}, summary)
        else:
            self.report({report_level(reports[0])}, reports[0].summary())
        return {"FINISHED"}


# 入れ子のパッケージを選ぶダイアログに渡すもの（#217）。EnumProperty の動的 items は文字列を保持しておく必要があるので、モジュールで持つ
_nested_pending: tuple | None = None  # (外側のパス, ImportOptions, 走査済みの UnityPackage, 入れ子の AssetEntry の並び)
_nested_items: list[tuple[str, str, str]] = []


def set_nested_pending(path: str, opts, pkg, nested) -> None:
    global _nested_pending
    _nested_pending = (path, opts, pkg, nested)
    from ..blender.importer import default_nested_package

    default = default_nested_package(nested)
    _nested_items[:] = [
        (e.pathname, sanitize_display(Path(e.pathname).name), sanitize_display(e.pathname))
        for e in [default] + [e for e in nested if e is not default]
    ]


def _nested_enum(self, context):
    return _nested_items or [("", "(none)", "")]


class IMPORT_SCENE_OT_unitypackage_nested(bpy.types.Operator):
    """入れ子になった .unitypackage のうち、読み込むものを選ぶ（#217）。"""

    bl_idname = "import_scene.unitypackage_nested"
    bl_label = "Choose a Package Inside"
    bl_description = "This package only contains other .unitypackage files (for example one per render pipeline). Choose one to import"
    bl_options = {"INTERNAL"}

    choice: EnumProperty(name="Package", items=_nested_enum)

    def invoke(self, context, event):
        if _nested_pending is None:
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=420, confirm_text="Continue")

    def draw(self, context):
        layout = self.layout
        path = _nested_pending[0] if _nested_pending else ""
        layout.label(text=sanitize_display(Path(path).name), icon="PACKAGE")
        layout.label(text="This package contains other packages. Choose one to import:")
        layout.prop(self, "choice", expand=True)

    def execute(self, context):
        global _nested_pending
        from ..blender.importer import build_shader_table, extract_nested_package, prepare_package, run_import

        if _nested_pending is None:
            return {"CANCELLED"}
        path, opts, pkg, nested = _nested_pending
        _nested_pending = None
        entry = next((e for e in nested if e.pathname == self.choice), None)
        if entry is None:
            return {"CANCELLED"}
        try:
            inner = str(extract_nested_package(pkg, entry, opts, Path(path)))
            prepared = prepare_package(inner, build_shader_table(opts.shader_table_path), import_blend=opts.import_blend)
            if opts.models == "ASK" and prepared.has_choices:
                select_models.set_pending(inner, opts, prepared)
                return bpy.ops.import_scene.unitypackage_select("INVOKE_DEFAULT")
            report = run_import(context, inner, opts, prepared=prepared)
        except PackageError as exc:
            self.report({"ERROR"}, sanitize_display(str(exc)))
            return {"CANCELLED"}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, sanitize_display(f"Import failed: {describe_exception(exc)}"))
            return {"CANCELLED"}
        self.report({report_level(report)}, report.summary())
        return {"FINISHED"}


class IO_FH_unitypackage(bpy.types.FileHandler):
    """3D ビューポートなどへの .unitypackage のドラッグ＆ドロップを受け付ける。"""

    bl_idname = "IO_FH_unitypackage"
    bl_label = "Unitypackage"
    bl_import_operator = IMPORT_SCENE_OT_unitypackage.bl_idname
    bl_file_extensions = ".unitypackage"

    @classmethod
    def poll_drop(cls, context):
        from bpy_extras.io_utils import poll_file_object_drop

        return poll_file_object_drop(context)


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_unitypackage.bl_idname, text="Unitypackage (.unitypackage)")


_classes = (IMPORT_SCENE_OT_unitypackage, IMPORT_SCENE_OT_unitypackage_nested, IO_FH_unitypackage)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
