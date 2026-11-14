"""捕まえて続けた例外のログ。

読み込みは 1 つの壊れた補助データでパッケージ全体を止めないので、例外を捕まえて警告に要約だけを出し、traceback は
verbose ログが有効なときだけコンソールに出す。有効かどうかは Preferences の Verbose Console Log で、オペレーターが
読み込みの前に ``set_verbose`` で渡す（以前は importer が ui.preferences を直接読んでいた。#198）。
"""

from __future__ import annotations

import traceback
from collections.abc import Callable

_verbose = True  # オペレーターを通さずに読み込むとき（スクリプトなど）は、今までどおり出す


def set_verbose(enabled: bool) -> None:
    global _verbose
    _verbose = enabled


def verbose() -> bool:
    return _verbose


def log_exception(message: str) -> None:
    """except の中で呼ぶ。verbose ログが有効なら、メッセージと traceback をコンソールに出す。"""
    if _verbose:
        print(f"[Unitypackage Importer] {message}")
        traceback.print_exc()


def warn_skipped(warn: Callable[[str], None], what: str, exc: Exception) -> None:
    """補助データを読めずに外したり既定値で続けたりするときに、except の中で呼ぶ（#69）。

    警告には ``what: 例外の文`` だけを出し、traceback は verbose ログが有効ならコンソールに出す。
    """
    warn(f"{what}: {exc}")
    log_exception(what)
