"""オペレーターが共通で使う関数（File > Import と、モデル選択ダイアログ）。"""

from __future__ import annotations

from dataclasses import fields


def options_from_operator(operator, prefs, **overrides):
    """オペレーターのプロパティと Preferences から ``ImportOptions`` を作る。

    ``ImportOptions`` のフィールドと同じ名前のプロパティは、そのまま写す（以前は約 30 個を 1 つずつ手で写していて、
    オプションを足すたびにオペレーター・ImportOptions・写す処理の 3 か所を直していた。#198）。
    Preferences にしか無いもの（追加のシェーダー表、展開の上限）はここで足す。
    """
    from ..blender.importer import ImportOptions

    names = {f.name for f in fields(ImportOptions)} & set(operator.properties.bl_rna.properties.keys())
    values = {name: getattr(operator, name) for name in names}
    values["shader_table_path"] = prefs.shader_table_path if prefs else ""
    values["max_extract_size"] = (prefs.max_extract_mb << 20) if prefs else 0
    values.update(overrides)
    return ImportOptions(**values)


def report_level(report) -> str:
    """読み込みの結果を Info バーに出すときの種類（警告かエラーがあれば WARNING）。"""
    return "WARNING" if report.warnings or report.errors else "INFO"
