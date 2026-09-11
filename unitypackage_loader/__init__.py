"""Unity Package Importer — .unitypackage を Blender へ直接読み込む Extension。

bpy が無い環境（単体テスト）でも ``core`` パッケージを import できるように、
bpy 依存部分は遅延 import にしている。
"""

from __future__ import annotations

try:
    import bpy  # noqa: F401
except ImportError:  # pragma: no cover - テスト実行時
    bpy = None


def register() -> None:
    from .operators import import_package, outline_ops, rebuild_material, select_models
    from .ui import panel_report, preferences

    preferences.register()
    select_models.register()
    import_package.register()
    outline_ops.register()
    rebuild_material.register()
    panel_report.register()


def unregister() -> None:
    from .operators import import_package, outline_ops, rebuild_material, select_models
    from .ui import panel_report, preferences

    panel_report.unregister()
    rebuild_material.unregister()
    outline_ops.unregister()
    import_package.unregister()
    select_models.unregister()
    preferences.unregister()
