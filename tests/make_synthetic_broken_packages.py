"""読み込みに失敗するモデルを含む合成パッケージを作る（#70 の統合テスト用。Blender は不要）。

実行例::

    python3 tests/make_synthetic_broken_packages.py [synthetic_multi.unitypackage] [出力フォルダ]

引数を省略すると ``_local/synthetic_multi.unitypackage`` を元にして ``_local/`` に書き出す。

- ``synthetic_broken_model.unitypackage``: ``synthetic_multi`` の ``Sphere.fbx`` の中身を壊したもの。
  そのモデルだけをエラーとして外し、残りを読み込むことを確かめる
- ``synthetic_broken_all.unitypackage``: 壊れた FBX だけのパッケージ。何も残さずに失敗することを確かめる
"""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# FBX のバイナリのヘッダーだけを持ち、中身の無いファイル（新旧どちらの FBX インポーターも RuntimeError になる）
BROKEN_FBX = b"Kaydara FBX Binary  \x00\x1a\x00" + b"broken on purpose"
BROKEN_GUID = "b0" * 16


def _add(tar: tarfile.TarFile, info: tarfile.TarInfo, data: bytes | None) -> None:
    tar.addfile(info, io.BytesIO(data) if data is not None else None)


def make_broken_model(source: Path, out: Path, target: str = "Assets/Synthetic/Models/Sphere.fbx") -> None:
    with tarfile.open(source, "r:*") as tar:
        members = [(m, tar.extractfile(m).read() if m.isfile() else None) for m in tar]
    pathnames = {m.name.split("/")[0]: data.decode("utf-8").strip() for m, data in members if m.name.endswith("/pathname")}
    guid = next((g for g, p in pathnames.items() if p == target), None)
    if guid is None:
        raise SystemExit(f"{target} not found in {source}")
    with tarfile.open(out, "w:gz") as tar:
        for info, data in members:
            if data is not None and info.name == f"{guid}/asset":
                data = BROKEN_FBX
                info.size = len(data)
            _add(tar, info, data)


def make_broken_all(out: Path) -> None:
    meta = f"fileFormatVersion: 2\nguid: {BROKEN_GUID}\nModelImporter:\n  serializedVersion: 22200\n".encode()
    files = {
        f"{BROKEN_GUID}/asset": BROKEN_FBX,
        f"{BROKEN_GUID}/asset.meta": meta,
        f"{BROKEN_GUID}/pathname": b"Assets/Broken/Broken.fbx",
    }
    with tarfile.open(out, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            _add(tar, info, data)


def main() -> None:
    argv = sys.argv[1:]
    source = Path(argv[0]) if argv else REPO_ROOT / "_local" / "synthetic_multi.unitypackage"
    out_dir = Path(argv[1]) if len(argv) > 1 else REPO_ROOT / "_local"
    out_dir.mkdir(parents=True, exist_ok=True)
    make_broken_model(source, out_dir / "synthetic_broken_model.unitypackage")
    make_broken_all(out_dir / "synthetic_broken_all.unitypackage")
    print(f"wrote {out_dir / 'synthetic_broken_model.unitypackage'} and {out_dir / 'synthetic_broken_all.unitypackage'}")


if __name__ == "__main__":
    main()
