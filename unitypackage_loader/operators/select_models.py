"""パッケージ内に複数モデルがあるときのモデル選択ダイアログ。

``IMPORT_SCENE_OT_unitypackage`` が ``prepare_package`` の結果を ``set_pending`` で渡し、
``INVOKE_DEFAULT`` でこのオペレーターを呼ぶ。ダイアログの OK で ``run_import`` を実行する。
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from ..core.package import PackageError


@dataclass
class _Pending:
    filepath: str
    opts: object  # ImportOptions
    prepared: object  # PreparedPackage


_pending: _Pending | None = None


def set_pending(filepath: str, opts, prepared) -> None:
    global _pending
    _pending = _Pending(filepath, opts, prepared)


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class UNITYPKG_ModelItem(bpy.types.PropertyGroup):
    guid: StringProperty()
    pathname: StringProperty()
    size_text: StringProperty()
    materials_text: StringProperty()
    selected: BoolProperty(default=True)
    supported: BoolProperty(default=True)


class UNITYPKG_UL_models(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        sub = row.row(align=True)
        sub.enabled = item.supported
        sub.prop(item, "selected", text="")
        sub.label(text=item.pathname, icon="MESH_DATA" if item.supported else "ERROR")
        right = row.row(align=True)
        right.alignment = "RIGHT"
        right.label(text=item.size_text)
        right.label(text=item.materials_text)


def _prefab_items(self, context):
    items = [("", "All (first wins)", "Merge every prefab; the first one by path takes precedence")]
    if _pending is not None:
        for pathname in sorted(_pending.prepared.prefab_tables):
            items.append((pathname, pathname.rsplit("/", 1)[-1], pathname))
    return items


class IMPORT_SCENE_OT_unitypackage_select(bpy.types.Operator):
    bl_idname = "import_scene.unitypackage_select"
    bl_label = "Select Models to Import"
    bl_description = "Choose which models in the package to import"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    items: CollectionProperty(type=UNITYPKG_ModelItem)
    active_index: IntProperty(default=0)
    package_name: StringProperty()
    summary_materials: StringProperty()
    summary_textures: StringProperty()
    prefab: EnumProperty(
        name="Prefab",
        items=_prefab_items,
        description="Prefab whose renderer material assignments are used when the model's own mapping is missing or shared",
    )

    def invoke(self, context, event):
        if _pending is None:
            self.report({"ERROR"}, "No pending package to import")
            return {"CANCELLED"}
        prepared = _pending.prepared
        self.items.clear()
        for m in prepared.models:
            item = self.items.add()
            item.guid = m.guid
            item.pathname = m.entry.pathname
            item.size_text = _human_size(m.entry.size)
            item.materials_text = (
                f"{m.resolved_count}/{m.material_count} mat" if m.material_count else "no mat info"
            ) if m.supported else "unsupported"
            item.supported = m.supported
            item.selected = m.supported
        self.package_name = prepared.path.name
        total = len(prepared.unity_mats)
        self.summary_materials = f"Materials: {total} found in package"
        if len(prepared.prefab_tables) > 1 and not _pending.opts.prefab:
            # 複数 prefab があるときは最初のものを既定にし、ユーザーが変えられるようにする
            self.prefab = sorted(prepared.prefab_tables)[0]
        elif _pending.opts.prefab in prepared.prefab_tables:
            self.prefab = _pending.opts.prefab
        ref, missing = len(prepared.referenced_textures), len(prepared.missing_textures)
        self.summary_textures = f"Textures: {ref} referenced" + (f", {missing} missing from package" if missing else "")
        return context.window_manager.invoke_props_dialog(self, width=620)

    def draw(self, context):
        layout = self.layout
        layout.label(text=self.package_name, icon="PACKAGE")
        layout.label(text="Models")
        layout.template_list("UNITYPKG_UL_models", "", self, "items", self, "active_index", rows=min(max(len(self.items), 3), 10))
        row = layout.row(align=True)
        row.operator("unitypkg.select_models_all", text="All").select = True
        row.operator("unitypkg.select_models_all", text="None").select = False
        col = layout.column(align=True)
        col.label(text=self.summary_materials, icon="MATERIAL")
        col.label(text=self.summary_textures, icon="TEXTURE")
        if _pending is not None and len(_pending.prepared.prefab_tables) > 1:
            layout.prop(self, "prefab")

    def execute(self, context):
        from ..blender.importer import run_import

        if _pending is None:
            self.report({"ERROR"}, "No pending package to import")
            return {"CANCELLED"}
        selected = [it.guid for it in self.items if it.selected and it.supported]
        if not selected:
            self.report({"WARNING"}, "No models selected")
            return {"CANCELLED"}
        opts = _pending.opts
        opts.model_guids = selected
        opts.prefab = self.prefab
        wm = context.window_manager
        wm.progress_begin(0, 100)
        try:
            report = run_import(
                context,
                _pending.filepath,
                opts,
                progress=lambda f, msg: wm.progress_update(int(f * 100)),
                prepared=_pending.prepared,
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
        from ..ui.preferences import get_prefs

        prefs = get_prefs(context)
        if prefs is None or prefs.verbose_log:
            print(report.as_text())
        self.report({"WARNING" if report.warnings else "INFO"}, report.summary())
        return {"FINISHED"}


class UNITYPKG_OT_select_models_all(bpy.types.Operator):
    """ダイアログ内の All / None ボタン。ダイアログのオペレーターインスタンスを直接触れないため、
    最後に invoke されたインスタンスの items を書き換える。"""

    bl_idname = "unitypkg.select_models_all"
    bl_label = "Select All Models"
    bl_options = {"INTERNAL"}

    select: BoolProperty(default=True)

    def execute(self, context):
        op = getattr(context, "active_operator", None)
        items = getattr(op, "items", None) if op is not None else None
        if items is None:
            return {"CANCELLED"}
        for it in items:
            if it.supported:
                it.selected = self.select
        return {"FINISHED"}


_classes = (
    UNITYPKG_ModelItem,
    UNITYPKG_UL_models,
    UNITYPKG_OT_select_models_all,
    IMPORT_SCENE_OT_unitypackage_select,
)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
