"""テストから core パッケージを import するためのパス設定。"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = REPO_ROOT / "_local"  # gitignore 済みの検証用データ置き場
PACKAGES_DIR = LOCAL_DIR / "unitypackages"  # 検証用 .unitypackage の置き場

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def local_sample() -> Path | None:
    """実パッケージでの検証に使うサンプル 1 つ（無ければ ``None``）。

    ``_local/expectations.json`` の ``"package"`` で指定されたものを使う。全パッケージを
    なめると重いうえ、アセットごとに成り立つ前提が違う（単色マテリアルだけ、tar が非圧縮、
    externalObjects が解決しない等）ので、対象はこの 1 つに絞る。
    """
    expectations = LOCAL_DIR / "expectations.json"
    if not expectations.is_file():
        return None
    named = json.loads(expectations.read_text("utf-8")).get("package")
    return find_package(named) if named else None


def find_package(name: str) -> Path | None:
    """ファイル名から検証用パッケージを探す。``_local/unitypackages/`` →
    ``_local/`` 直下（昔の置き場）の順に見て、見つからなければ ``None``。"""
    for candidate in (PACKAGES_DIR / name, LOCAL_DIR / name):
        if candidate.is_file():
            return candidate
    return None
