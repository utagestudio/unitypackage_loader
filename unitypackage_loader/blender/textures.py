"""展開済みテクスチャを bpy.data.images として読み込む。"""

from __future__ import annotations

from pathlib import Path

import bpy

from ..core.meta import TextureImporterInfo

# Blender が読めない形式（展開だけ行い、読み込みは警告にする）
UNSUPPORTED_IMAGE_EXTS = frozenset({".psd"})


def load_image(path: Path, info: TextureImporterInfo, *, pack: bool = False) -> bpy.types.Image | None:
    if path.suffix.lower() in UNSUPPORTED_IMAGE_EXTS or not path.is_file():
        return None
    image = bpy.data.images.load(str(path), check_existing=True)
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
