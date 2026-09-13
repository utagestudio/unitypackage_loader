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
from ..core.report import sanitize_display


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


PREFAB_ALL = "ALL"

# EnumProperty の動的 items は Python 側で文字列を保持しておかないと表示が壊れるので、モデル GUID ごとに持つ
_prefab_enum_cache: dict[str, list[tuple[str, str, str]]] = {}


def _prefab_candidates(model_guid: str) -> list[str]:
    return _pending.prepared.prefabs_for(model_guid) if _pending is not None else []


def _prefab_items(self, context):
    """モデル行の prefab ドロップダウン。識別子は候補の番号（pathname はパッケージ由来の文字列なので使わない）。"""
    items = [(PREFAB_ALL, "All (first wins)", "Merge every prefab using this model; the first one by path takes precedence")]
    for index, pathname in enumerate(_prefab_candidates(self.guid)):
        shown = sanitize_display(pathname)
        items.append((str(index), shown.rsplit("/", 1)[-1], shown))
    _prefab_enum_cache[self.guid] = items
    return items


class UNITYPKG_ModelItem(bpy.types.PropertyGroup):
    guid: StringProperty()
    pathname: StringProperty()
    size_text: StringProperty()
    materials_text: StringProperty()
    selected: BoolProperty(default=True)
    supported: BoolProperty(default=True)
    prefab_count: IntProperty(default=0)
    prefab: EnumProperty(
        name="Prefab",
        items=_prefab_items,
        description="Prefab whose renderer material assignments are used for this model",
    )


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
        if item.prefab_count > 1:
            # 色違いなど、同じモデルを使う prefab が複数あるモデルだけ選べるようにする
            prefab = right.row(align=True)
            prefab.enabled = item.supported
            prefab.prop(item, "prefab", text="", icon="PACKAGE")


_ITEMS_PROP = "unitypkg_select_models"
_INDEX_PROP = "unitypkg_select_models_index"


def _model_items(context):
    """ダイアログのモデル一覧（WindowManager 側。ダイアログ内のボタンからも触れるようにするため）。"""
    return getattr(context.window_manager, _ITEMS_PROP)


class IMPORT_SCENE_OT_unitypackage_select(bpy.types.Operator):
    bl_idname = "import_scene.unitypackage_select"
    bl_label = "Select Models to Import"
    bl_description = "Choose which models in the package to import"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    package_name: StringProperty()
    summary_materials: StringProperty()
    summary_textures: StringProperty()

    def invoke(self, context, event):
        if _pending is None:
            self.report({"ERROR"}, "No pending package to import")
            return {"CANCELLED"}
        prepared = _pending.prepared
        items = _model_items(context)
        items.clear()
        for m in prepared.models:
            item = items.add()
            item.guid = m.guid
            item.pathname = sanitize_display(m.entry.pathname)
            item.size_text = _human_size(m.entry.size)
            if m.supported:
                item.materials_text = f"{m.resolved_count}/{m.material_count} mat" if m.material_count else "no mat info"
            elif m.entry.ext == ".blend":
                item.materials_text = ".blend import disabled"
            else:
                item.materials_text = "unsupported"
            item.supported = m.supported
            item.selected = m.supported
            candidates = prepared.prefabs_for(m.guid)
            item.prefab_count = len(candidates)
            # 既定は「そのモデルを使う prefab を統合」。前回の指定があれば引き継ぐ
            chosen = _pending.opts.prefabs.get(m.guid, "")
            item.prefab = str(candidates.index(chosen)) if chosen in candidates else PREFAB_ALL
        self.package_name = sanitize_display(prepared.path.name)
        total = len(prepared.unity_mats)
        self.summary_materials = f"Materials: {total} found in package"
        ref, missing = len(prepared.referenced_textures), len(prepared.missing_textures)
        self.summary_textures = f"Textures: {ref} referenced" + (f", {missing} missing from package" if missing else "")
        return context.window_manager.invoke_props_dialog(self, width=620)

    def draw(self, context):
        layout = self.layout
        layout.label(text=self.package_name, icon="PACKAGE")
        layout.label(text="Models")
        wm = context.window_manager
        items = _model_items(context)
        layout.template_list("UNITYPKG_UL_models", "", wm, _ITEMS_PROP, wm, _INDEX_PROP, rows=min(max(len(items), 3), 10))
        row = layout.row(align=True)
        row.operator("unitypkg.select_models_all", text="All").select = True
        row.operator("unitypkg.select_models_all", text="None").select = False
        col = layout.column(align=True)
        col.label(text=self.summary_materials, icon="MATERIAL")
        col.label(text=self.summary_textures, icon="TEXTURE")
        if any(it.prefab_count > 1 for it in items):
            col.label(text="Prefab: choose per model which prefab's material assignments to use", icon="PACKAGE")

    def execute(self, context):
        from ..blender.importer import run_import

        if _pending is None:
            self.report({"ERROR"}, "No pending package to import")
            return {"CANCELLED"}
        items = _model_items(context)
        selected = [it.guid for it in items if it.selected and it.supported]
        if not selected:
            self.report({"WARNING"}, "No models selected")
            return {"CANCELLED"}
        opts = _pending.opts
        opts.model_guids = selected
        opts.prefabs = {}
        for it in items:
            candidates = _prefab_candidates(it.guid)
            if it.prefab != PREFAB_ALL and it.prefab.isdigit() and int(it.prefab) < len(candidates):
                opts.prefabs[it.guid] = candidates[int(it.prefab)]
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
            self.report({"ERROR"}, sanitize_display(str(exc)))
            return {"CANCELLED"}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, sanitize_display(f"Import failed: {exc!r}"))
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
    """ダイアログ内の All / None ボタン。

    ダイアログが開いている間は ``context.active_operator`` がダイアログを指さない（None になる）ため、
    モデル一覧は WindowManager 側に置き、ここから直接書き換える。"""

    bl_idname = "unitypkg.select_models_all"
    bl_label = "Select All Models"
    bl_options = {"INTERNAL"}

    select: BoolProperty(default=True)

    def execute(self, context):
        items = _model_items(context)
        if not items:
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
    setattr(bpy.types.WindowManager, _ITEMS_PROP, CollectionProperty(type=UNITYPKG_ModelItem))
    setattr(bpy.types.WindowManager, _INDEX_PROP, IntProperty(default=0))


def unregister() -> None:
    for prop in (_INDEX_PROP, _ITEMS_PROP):
        if hasattr(bpy.types.WindowManager, prop):
            delattr(bpy.types.WindowManager, prop)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
