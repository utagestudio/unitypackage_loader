"""入れ子になった .unitypackage（パイプライン別の中身をパッケージのまま同梱したもの。#217）の選び方と、外側のアセットとの結合。"""

from __future__ import annotations

import tarfile
from pathlib import Path, PurePosixPath

from .package import AssetEntry, PackageError, UnityPackage, split_member


# 入れ子のパッケージを選ばなかったときに優先する中身（Blender のマテリアルにもっとも近い Built-in のもの。#217）
_NESTED_PREFERRED = ("builtin", "built-in", "birp", "standard", "3d")


def default_nested_package(entries: list[AssetEntry]) -> AssetEntry:
    """入れ子のパッケージのうち、選ばなかったときに読むもの（Built-in 向けらしい名前のもの、無ければ pathname 順の最初）。"""
    for entry in entries:
        if any(key in PurePosixPath(entry.pathname).stem.lower() for key in _NESTED_PREFERRED):
            return entry
    return entries[0]


# 外側のパッケージにあっても読み込みに使わないもの（説明書など）
_DOCUMENT_EXTS = frozenset({".txt", ".pdf", ".md", ".rtf", ".html", ".htm", ".url"})


def merge_nested_package(outer: UnityPackage, inner_path: Path, dest: Path) -> Path | None:
    """入れ子のパッケージ ``inner_path`` に、外側のパッケージのアセット（入れ子のパッケージと説明書を除く）を足した
    パッケージを ``dest`` に書く。外側に足すものが無ければ書かずに None。

    中身の prefab が外側のメッシュやテクスチャを使うことがある（Conifers BOTD の BIRP / URP / HDRP の中身は prefab とマテリアルだけで、
    メッシュは外側の Sources にある。#217）。中身にある GUID は中身のものを使う。``dest`` が両方より新しければ書き直さない。
    """
    outer.scan()
    extra = {
        e.guid for e in outer.entries.values()
        if e.has_asset and e.ext != ".unitypackage" and e.ext not in _DOCUMENT_EXTS
    }
    if not extra:
        return None
    if dest.is_file() and dest.stat().st_mtime >= max(inner_path.stat().st_mtime, outer.path.stat().st_mtime):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    inner_guids: set[str] = set()
    try:
        with tarfile.open(partial, "w:gz", compresslevel=1) as out:
            for source, wanted in ((inner_path, None), (outer.path, extra)):
                with tarfile.open(source, "r:*") as tar:
                    for member in tar:
                        split = split_member(member.name)
                        if split is None or not member.isfile():
                            continue
                        guid = split[0]
                        if wanted is None:
                            inner_guids.add(guid)
                        elif guid not in wanted or guid in inner_guids:
                            continue
                        out.addfile(member, tar.extractfile(member))
        partial.replace(dest)
    except (tarfile.TarError, EOFError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise PackageError(f"cannot combine {inner_path.name} with {outer.path.name}: {exc}") from exc
    return dest
