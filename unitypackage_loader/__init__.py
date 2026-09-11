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
    from .operators import import_package

    import_package.register()


def unregister() -> None:
    from .operators import import_package

    import_package.unregister()
