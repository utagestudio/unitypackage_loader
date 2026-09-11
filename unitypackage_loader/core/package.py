"""``.unitypackage``（tar.gz）の走査・索引・オンデマンド展開。

tar.gz はランダムアクセスできないため、

1. ``scan()`` で 1 度ストリーム走査して GUID ごとの索引を作る
   （``pathname`` と ``asset.meta`` は小さいので常駐、``.mat`` の実体もキャッシュ）
2. ``extract()`` で必要な GUID だけを 2 度目の走査で書き出す

という 2 パス構成にしている。
"""

from __future__ import annotations

import tarfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = [
    "AssetEntry",
    "PackageError",
    "UnityPackage",
    "MODEL_EXTS",
    "TEXTURE_EXTS",
    "MATERIAL_EXTS",
]

MODEL_EXTS = frozenset({".fbx", ".obj", ".dae", ".blend", ".gltf", ".glb"})
TEXTURE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".bmp", ".exr", ".hdr", ".psd", ".webp"}
)
MATERIAL_EXTS = frozenset({".mat"})

# scan 時に実体をメモリへ載せておく拡張子と上限サイズ
_CACHE_EXTS = frozenset({".mat"})
_CACHE_MAX_SIZE = 1 << 20  # 1 MiB

ProgressFn = Callable[[float, str], None]


class PackageError(RuntimeError):
    """unitypackage として読めない場合に送出される。"""


@dataclass
class AssetEntry:
    guid: str
    pathname: str = ""
    has_asset: bool = False
    size: int = 0
    meta_text: str | None = None
    _cache: bytes | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return PurePosixPath(self.pathname).name

    @property
    def ext(self) -> str:
        return PurePosixPath(self.pathname).suffix.lower()

    @property
    def kind(self) -> str:
        """``model`` / ``material`` / ``texture`` / ``folder`` / ``other``"""
        if not self.has_asset:
            return "folder"
        ext = self.ext
        if ext in MODEL_EXTS:
            return "model"
        if ext in MATERIAL_EXTS:
            return "material"
        if ext in TEXTURE_EXTS:
            return "texture"
        return "other"


def _split_member(name: str) -> tuple[str, str] | None:
    """tar メンバー名を (guid, part) に分ける。想定外の形なら None。"""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if len(parts) != 2:
        return None
    guid, part = parts
    if len(guid) != 32 or any(c not in "0123456789abcdefABCDEF" for c in guid):
        return None
    return guid.lower(), part


def safe_relative_path(pathname: str) -> PurePosixPath:
    """展開先に使える相対パスへ正規化する。``..`` や絶対パスは拒否。"""
    p = PurePosixPath(pathname.replace("\\", "/"))
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        raise PackageError(f"unsafe pathname in package: {pathname!r}")
    if not p.parts:
        raise PackageError("empty pathname in package")
    return p


class UnityPackage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, AssetEntry] = {}
        self._scanned = False

    # ------------------------------------------------------------------ scan
    def scan(self, progress: ProgressFn | None = None) -> dict[str, AssetEntry]:
        if self._scanned:
            return self.entries
        entries: dict[str, AssetEntry] = {}
        try:
            with tarfile.open(self.path, "r:*") as tar:
                for member in tar:
                    split = _split_member(member.name)
                    if split is None or not member.isfile():
                        continue
                    guid, part = split
                    entry = entries.get(guid)
                    if entry is None:
                        entry = entries[guid] = AssetEntry(guid)
                    if part == "pathname":
                        text = tar.extractfile(member).read().decode("utf-8", "replace")
                        entry.pathname = text.splitlines()[0].strip() if text.strip() else ""
                    elif part == "asset.meta":
                        entry.meta_text = tar.extractfile(member).read().decode("utf-8", "replace")
                    elif part == "asset":
                        entry.has_asset = True
                        entry.size = member.size
                        if member.size <= _CACHE_MAX_SIZE:
                            entry._cache = tar.extractfile(member).read()
                    if progress is not None:
                        progress(0.0, entry.pathname or guid)
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise PackageError(f"cannot read {self.path.name}: {exc}") from exc

        if not entries:
            raise PackageError(f"{self.path.name} does not look like a unitypackage (no GUID entries)")

        # pathname が判明した後で、キャッシュを残す拡張子以外は解放する
        for entry in entries.values():
            if entry._cache is not None and entry.ext not in _CACHE_EXTS:
                entry._cache = None

        self.entries = entries
        self._scanned = True
        return entries

    # ---------------------------------------------------------------- lookup
    def _require_scan(self) -> None:
        if not self._scanned:
            self.scan()

    def by_kind(self, kind: str) -> list[AssetEntry]:
        self._require_scan()
        return sorted((e for e in self.entries.values() if e.kind == kind), key=lambda e: e.pathname)

    def models(self) -> list[AssetEntry]:
        return self.by_kind("model")

    def materials(self) -> list[AssetEntry]:
        return self.by_kind("material")

    def textures(self) -> list[AssetEntry]:
        return self.by_kind("texture")

    def get(self, guid: str) -> AssetEntry | None:
        self._require_scan()
        return self.entries.get(guid.lower())

    def find_by_path(self, pathname: str) -> AssetEntry | None:
        self._require_scan()
        for entry in self.entries.values():
            if entry.pathname == pathname:
                return entry
        return None

    # ------------------------------------------------------------- read/extract
    def read_asset(self, guid: str) -> bytes:
        """小さなアセット（.mat など）の実体を返す。キャッシュに無ければ再走査する。"""
        entry = self.get(guid)
        if entry is None or not entry.has_asset:
            raise KeyError(guid)
        if entry._cache is not None:
            return entry._cache
        target = f"{entry.guid}/asset"
        with tarfile.open(self.path, "r:*") as tar:
            for member in tar:
                split = _split_member(member.name)
                if split and split[0] == entry.guid and split[1] == "asset":
                    return tar.extractfile(member).read()
        raise KeyError(f"{target} not found in {self.path.name}")

    def read_text(self, guid: str) -> str:
        return self.read_asset(guid).decode("utf-8", "replace")

    def extract(
        self,
        guids: Iterable[str],
        dest_root: str | Path,
        *,
        overwrite: bool = False,
        progress: ProgressFn | None = None,
    ) -> dict[str, Path]:
        """指定 GUID の ``asset`` を ``dest_root/<pathname>`` に書き出し、GUID → パスを返す。

        既に同じサイズのファイルがあれば ``overwrite=False`` のとき書き出しをスキップする。
        """
        self._require_scan()
        dest_root = Path(dest_root)
        wanted: dict[str, AssetEntry] = {}
        for guid in guids:
            entry = self.get(guid)
            if entry is not None and entry.has_asset and entry.pathname:
                wanted[entry.guid] = entry

        result: dict[str, Path] = {}
        pending: dict[str, Path] = {}
        for guid, entry in wanted.items():
            target = dest_root / safe_relative_path(entry.pathname)
            result[guid] = target
            if not overwrite and target.is_file() and target.stat().st_size == entry.size:
                continue
            pending[guid] = target

        if not pending:
            return result

        done = 0
        with tarfile.open(self.path, "r:*") as tar:
            for member in tar:
                split = _split_member(member.name)
                if not split or split[1] != "asset" or split[0] not in pending:
                    continue
                target = pending.pop(split[0])
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                with open(target, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                done += 1
                if progress is not None:
                    progress(done / (done + len(pending)), target.name)
                if not pending:
                    break
        return result
