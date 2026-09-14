"""インポートの 2 段目: 読み込む単位（Prefabs / Models）を選び、その候補から読み込むものを選ぶダイアログ。

``IMPORT_SCENE_OT_unitypackage`` が ``prepare_package`` の結果を ``set_pending`` で渡し、
``INVOKE_DEFAULT`` でこのオペレーターを呼ぶ。ダイアログの OK で ``run_import`` を実行する。

単位は排他で、一覧にはいま選んでいる単位の候補だけを出す（同じモデルの二重読み込みを構造上起こさない）。
候補は推測で外さず、読み込めないものは灰色にして理由を表示する。
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from ..core.package import PackageError
from ..core.report import sanitize_display
from ..core.units import NO_MESH_REASON, UNIT_MODELS, UNIT_PREFABS, available_units, default_unit
from ..ui.preferences import ARRANGE_ITEMS, UNIT_ITEMS, get_prefs


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


def _short_reason(reason: str) -> str:
    """行の右端に出す、読み込めない理由の短い表記。"""
    from ..blender.importer import BLEND_DISABLED_REASON

    if reason == BLEND_DISABLED_REASON:
        return ".blend import disabled"
    if reason == NO_MESH_REASON:
        return reason
    return "unsupported format"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


_UNIT_ICONS = {UNIT_PREFABS: "PACKAGE", UNIT_MODELS: "MESH_DATA"}
_UNIT_NUMBERS = {UNIT_PREFABS: 0, UNIT_MODELS: 1}

# EnumProperty の動的 items は Python 側で文字列を保持しておかないと表示が壊れるので、モジュールで持つ
_unit_enum_cache: list[tuple[str, str, str, str, int]] = []


def _unit_items(self, context):
    """単位の切り替え。候補が 1 つも無い単位は出さない。ラベルに候補数を付ける。"""
    labels = {identifier: (name, description) for identifier, name, description in UNIT_ITEMS}
    counts = {UNIT_PREFABS: 0, UNIT_MODELS: 0}
    units: list[str] = []
    if _pending is not None:
        prepared = _pending.prepared
        counts = {UNIT_PREFABS: len(prepared.prefabs), UNIT_MODELS: len(prepared.models)}
        units = available_units(prepared.prefabs, len(prepared.models))
    _unit_enum_cache[:] = [
        (unit, f"{labels[unit][0]} ({counts[unit]})", labels[unit][1], _UNIT_ICONS[unit], _UNIT_NUMBERS[unit])
        for unit in units or [UNIT_MODELS]
    ]
    return _unit_enum_cache


class UNITYPKG_ImportItem(bpy.types.PropertyGroup):
    kind: StringProperty()  # UNIT_PREFABS / UNIT_MODELS
    guid: StringProperty()
    pathname: StringProperty()  # 表示用（sanitize_display 済み）
    size_text: StringProperty()
    detail_text: StringProperty()
    selected: BoolProperty(default=True)
    supported: BoolProperty(default=True)


class UNITYPKG_UL_import_items(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        sub = row.row(align=True)
        sub.enabled = item.supported
        sub.prop(item, "selected", text="")
        sub.label(text=item.pathname, icon=_UNIT_ICONS.get(item.kind, "DOT") if item.supported else "ERROR")
        right = row.row(align=True)
        right.alignment = "RIGHT"
        right.enabled = item.supported
        if item.size_text:
            right.label(text=item.size_text)
        right.label(text=item.detail_text)

    def filter_items(self, context, data, propname):
        """いま選んでいる単位の候補だけを表示する（名前での絞り込みも効かせる）。"""
        items = getattr(data, propname)
        unit = getattr(context.window_manager, _UNIT_PROP)
        if self.filter_name:
            flags = bpy.types.UI_UL_list.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, items, "pathname", reverse=self.use_filter_invert
            )
        else:
            flags = [self.bitflag_filter_item] * len(items)
        flags = [flag if item.kind == unit else 0 for flag, item in zip(flags, items)]
        return flags, []


_ITEMS_PROP = "unitypkg_select_models"
_INDEX_PROP = "unitypkg_select_models_index"
_UNIT_PROP = "unitypkg_select_unit"
_ARRANGE_PROP = "unitypkg_select_arrange"


def _items(context):
    """ダイアログの候補一覧（WindowManager 側。ダイアログ内のボタンからも触れるようにするため）。"""
    return getattr(context.window_manager, _ITEMS_PROP)


def _current_unit(context) -> str:
    return getattr(context.window_manager, _UNIT_PROP)


class IMPORT_SCENE_OT_unitypackage_select(bpy.types.Operator):
    bl_idname = "import_scene.unitypackage_select"
    bl_label = "Import from Unitypackage"
    bl_description = "Choose what to import from the package: prefabs or model files"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    package_name: StringProperty()
    summary_materials: StringProperty()
    summary_textures: StringProperty()

    def invoke(self, context, event):
        if _pending is None:
            self.report({"ERROR"}, "No pending package to import")
            return {"CANCELLED"}
        prepared = _pending.prepared
        items = _items(context)
        items.clear()
        for p in prepared.prefabs:
            item = items.add()
            item.kind = UNIT_PREFABS
            item.guid = p.guid
            item.pathname = sanitize_display(p.pathname)
            if p.supported:
                item.detail_text = f"{_plural(len(p.model_guids), 'model')} · {len(p.material_guids)} mat"
            else:
                item.detail_text = _short_reason(p.skip_reason)
            item.supported = p.supported
            item.selected = p.supported
        for m in prepared.models:
            item = items.add()
            item.kind = UNIT_MODELS
            item.guid = m.guid
            item.pathname = sanitize_display(m.entry.pathname)
            item.size_text = _human_size(m.entry.size)
            if m.supported:
                item.detail_text = f"{m.resolved_count}/{m.material_count} mat" if m.material_count else "no mat info"
            else:
                item.detail_text = _short_reason(m.skip_reason)
            item.supported = m.supported
            item.selected = m.supported

        wm = context.window_manager
        prefs = get_prefs(context)
        last_unit = prefs.last_import_unit if prefs is not None else ""
        setattr(wm, _UNIT_PROP, default_unit(prepared.prefabs, len(prepared.supported_models), last_unit))
        setattr(wm, _ARRANGE_PROP, prefs.default_arrange if prefs is not None else _pending.opts.arrange)

        self.package_name = sanitize_display(prepared.path.name)
        self.summary_materials = f"Materials: {len(prepared.unity_mats)} found in package"
        ref, missing = len(prepared.referenced_textures), len(prepared.missing_textures)
        self.summary_textures = f"Textures: {ref} referenced" + (f", {missing} missing from package" if missing else "")
        return wm.invoke_props_dialog(self, width=640, confirm_text="Import")

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        unit = _current_unit(context)
        items = _items(context)
        visible = [it for it in items if it.kind == unit]

        layout.label(text=self.package_name, icon="PACKAGE")
        layout.row().prop(wm, _UNIT_PROP, expand=True)
        layout.template_list(
            "UNITYPKG_UL_import_items", "", wm, _ITEMS_PROP, wm, _INDEX_PROP, rows=min(max(len(visible), 3), 10)
        )
        row = layout.row(align=True)
        row.operator("unitypkg.select_models_all", text="All").select = True
        row.operator("unitypkg.select_models_all", text="None").select = False

        if unit == UNIT_PREFABS:
            # 並べ方は 2 つ以上選んだときだけ効く。行を出し入れするとダイアログの高さと Import ボタンの位置が
            # 変わるので、常に出しておき、1 つ以下のときは淡色にする
            split = layout.split(factor=0.2)
            split.active = sum(1 for it in visible if it.selected and it.supported) > 1
            split.label(text="Arrange")
            split.row().prop(wm, _ARRANGE_PROP, expand=True)

        col = layout.column(align=True)
        col.label(text=self.summary_materials, icon="MATERIAL")
        col.label(text=self.summary_textures, icon="TEXTURE")

    def execute(self, context):
        from ..blender.importer import run_import

        if _pending is None:
            self.report({"ERROR"}, "No pending package to import")
            return {"CANCELLED"}
        wm = context.window_manager
        unit = _current_unit(context)
        arrange = getattr(wm, _ARRANGE_PROP)
        chosen = {it.guid for it in _items(context) if it.kind == unit and it.selected and it.supported}
        if not chosen:
            self.report({"WARNING"}, "Nothing selected to import")
            return {"CANCELLED"}
        prepared = _pending.prepared
        opts = _pending.opts
        opts.unit = unit
        opts.arrange = arrange
        if unit == UNIT_PREFABS:
            opts.prefab_paths = [p.pathname for p in prepared.prefabs if p.guid in chosen]
            opts.model_guids = None
        else:
            opts.model_guids = [m.guid for m in prepared.models if m.guid in chosen]
            opts.prefab_paths = None

        prefs = get_prefs(context)
        if prefs is not None:
            # 次に開いたときの既定にする（並べ方は Preferences の既定値そのものを更新する）
            prefs.last_import_unit = unit
            if unit == UNIT_PREFABS:
                prefs.default_arrange = arrange

        wm.progress_begin(0, 100)
        try:
            report = run_import(
                context,
                _pending.filepath,
                opts,
                progress=lambda f, msg: wm.progress_update(int(f * 100)),
                prepared=prepared,
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

        if prefs is None or prefs.verbose_log:
            print(report.as_text())
        self.report({"WARNING" if report.warnings else "INFO"}, report.summary())
        return {"FINISHED"}


class UNITYPKG_OT_select_models_all(bpy.types.Operator):
    """ダイアログ内の All / None ボタン。いま表示している単位の候補だけを切り替える。

    ダイアログが開いている間は ``context.active_operator`` がダイアログを指さない（None になる）ため、
    候補一覧は WindowManager 側に置き、ここから直接書き換える。"""

    bl_idname = "unitypkg.select_models_all"
    bl_label = "Select All"
    bl_options = {"INTERNAL"}

    select: BoolProperty(default=True)

    def execute(self, context):
        items = _items(context)
        if not items:
            return {"CANCELLED"}
        unit = _current_unit(context)
        for it in items:
            if it.supported and it.kind == unit:
                it.selected = self.select
        return {"FINISHED"}


_classes = (
    UNITYPKG_ImportItem,
    UNITYPKG_UL_import_items,
    UNITYPKG_OT_select_models_all,
    IMPORT_SCENE_OT_unitypackage_select,
)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    wm = bpy.types.WindowManager
    setattr(wm, _ITEMS_PROP, CollectionProperty(type=UNITYPKG_ImportItem))
    setattr(wm, _INDEX_PROP, IntProperty(default=0))
    setattr(wm, _UNIT_PROP, EnumProperty(name="Import Unit", items=_unit_items))
    setattr(wm, _ARRANGE_PROP, EnumProperty(name="Arrange", items=ARRANGE_ITEMS, default="SIDE_BY_SIDE"))


def unregister() -> None:
    for prop in (_ARRANGE_PROP, _UNIT_PROP, _INDEX_PROP, _ITEMS_PROP):
        if hasattr(bpy.types.WindowManager, prop):
            delattr(bpy.types.WindowManager, prop)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
