"""テストから core パッケージを import するためのパス設定。"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = REPO_ROOT / "_local"  # gitignore 済みの検証用データ置き場

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def local_packages() -> list[Path]:
    """``_local/`` 直下にある .unitypackage（無ければ空リスト）。"""
    if not LOCAL_DIR.is_dir():
        return []
    return sorted(LOCAL_DIR.glob("*.unitypackage"))
