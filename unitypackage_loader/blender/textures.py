"""展開済みテクスチャを bpy.data.images として読み込む。"""

from __future__ import annotations

from pathlib import Path

import bpy

from ..core.meta import TextureImporterInfo

# 読めるかを読み込んだ直後に確かめる形式。PSD は統合した画像（Photoshop の「互換性を優先」）があれば Blender で読めるが、
# 無ければ読めない（#238。以前は PSD を一律に読まず、展開だけしていた）
_CHECKED_IMAGE_EXTS = frozenset({".psd"})


def load_image(path: Path, info: TextureImporterInfo, *, pack: bool = False, refresh: bool = False) -> bpy.types.Image | None:
    """``refresh`` は、ファイルを展開し直したとき。同じパスで読み込み済みの画像を読み直す（古い内容のまま使わない。#72）。

    読めなければ None（呼ぶ側が警告にする）。
    """
    if not path.is_file():
        return None
    if refresh:
        for existing in bpy.data.images:
            if existing.filepath and not existing.packed_file and bpy.path.abspath(existing.filepath) == str(path):
                existing.reload()
    try:
        image = bpy.data.images.load(str(path), check_existing=True)
    except RuntimeError:  # 形式が分からないファイル
        return None
    if path.suffix.lower() in _CHECKED_IMAGE_EXTS and not image.has_data and tuple(image.size) == (0, 0):
        # 画像の読み込みは遅延される。size を見ると読み込まれ、読めなければ 0 × 0 のまま（ピンクの画像にしない）
        bpy.data.images.remove(image)
        return None
    apply_texture_info(image, info)
    if pack and not image.packed_file:
        try:
            image.pack()
        except RuntimeError:
            pass
    return image


def apply_texture_info(image: bpy.types.Image, info: TextureImporterInfo) -> None:
    try:
        image.colorspace_settings.name = "Non-Color" if info.is_linear else "sRGB"
    except TypeError:
        pass
    # alphaIsTransparency が立っていない画像は、アルファを別データ（マスク等）として扱う
    image.alpha_mode = "STRAIGHT" if info.alpha_is_transparency else "CHANNEL_PACKED"
