"""``asset.meta`` から必要なインポーター設定だけを取り出す。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .unity_yaml import UnityRef, parse_text

__all__ = [
    "MetaInfo",
    "ModelImporterInfo",
    "TextureImporterInfo",
    "parse_meta",
    "strip_numeric_suffix",
]

# Unity の TextureImporter.textureType
TEXTURE_TYPE_DEFAULT = 0
TEXTURE_TYPE_NORMAL_MAP = 1
TEXTURE_TYPE_SPRITE = 8
TEXTURE_TYPE_SINGLE_CHANNEL = 10


@dataclass
class MetaInfo:
    guid: str
    importer: str | None  # 例: "ModelImporter", "TextureImporter", "NativeFormatImporter"
    data: dict[str, Any] = field(default_factory=dict)


def parse_meta(text: str) -> MetaInfo:
    root = parse_text(text)
    if not isinstance(root, dict):
        raise ValueError("meta root is not a mapping")
    guid = str(root.get("guid", ""))
    importer = None
    data: dict[str, Any] = {}
    for key, value in root.items():
        if key.endswith("Importer") and isinstance(value, dict):
            importer, data = key, value
            break
    return MetaInfo(guid=guid, importer=importer, data=data)


def strip_numeric_suffix(name: str) -> str:
    """``Body.001`` → ``Body``。Unity / Blender が付ける連番サフィックスを外す。"""
    base, dot, tail = name.rpartition(".")
    if dot and tail.isdigit() and len(tail) == 3:
        return base
    return name


@dataclass
class ModelImporterInfo:
    external_materials: dict[str, str] = field(default_factory=dict)  # FBX 内マテリアル名 → .mat GUID
    material_import_mode: int | None = None
    global_scale: float = 1.0
    use_file_scale: bool = True
    import_blend_shapes: bool = True

    @classmethod
    def from_meta(cls, meta: MetaInfo | str) -> "ModelImporterInfo":
        if isinstance(meta, str):
            meta = parse_meta(meta)
        data = meta.data if meta.importer == "ModelImporter" else {}
        info = cls()
        for item in data.get("externalObjects") or []:
            if not isinstance(item, dict):
                continue
            first = item.get("first") or {}
            second = item.get("second")
            if not isinstance(first, dict) or not isinstance(second, UnityRef):
                continue
            if str(first.get("type", "")).endswith("Material") and second.guid:
                info.external_materials[str(first.get("name", ""))] = second.guid
        materials = data.get("materials") or {}
        if isinstance(materials, dict) and isinstance(materials.get("materialImportMode"), int):
            info.material_import_mode = materials["materialImportMode"]
        meshes = data.get("meshes") or {}
        if isinstance(meshes, dict):
            scale = meshes.get("globalScale")
            if isinstance(scale, (int, float)):
                info.global_scale = float(scale)
            info.use_file_scale = bool(meshes.get("useFileScale", 1))
            info.import_blend_shapes = bool(meshes.get("importBlendShapes", 1))
        return info

    def resolve_material(self, fbx_name: str) -> str | None:
        """完全一致 → 連番サフィックス除去 の順で .mat GUID を探す。"""
        guid = self.external_materials.get(fbx_name)
        if guid:
            return guid
        base = strip_numeric_suffix(fbx_name)
        if base != fbx_name:
            guid = self.external_materials.get(base)
            if guid:
                return guid
        # 逆に Unity 側が連番付きで登録している場合（"Body.001" のみ存在など）
        for name, guid in self.external_materials.items():
            if strip_numeric_suffix(name) == base:
                return guid
        return None


@dataclass
class TextureImporterInfo:
    texture_type: int = TEXTURE_TYPE_DEFAULT
    srgb: bool = True
    alpha_is_transparency: bool = False
    alpha_usage: int = 1
    wrap_u: int = 0  # 0 Repeat / 1 Clamp / 2 Mirror / 3 MirrorOnce
    wrap_v: int = 0

    @classmethod
    def from_meta(cls, meta: MetaInfo | str | None) -> "TextureImporterInfo":
        if meta is None:
            return cls()
        if isinstance(meta, str):
            meta = parse_meta(meta)
        data = meta.data if meta.importer == "TextureImporter" else {}
        info = cls()
        if isinstance(data.get("textureType"), int):
            info.texture_type = data["textureType"]
        mipmaps = data.get("mipmaps") or {}
        srgb = mipmaps.get("sRGBTexture") if isinstance(mipmaps, dict) else None
        if srgb is None:
            srgb = data.get("sRGBTexture")
        if isinstance(srgb, int):
            info.srgb = bool(srgb)
        if isinstance(data.get("alphaIsTransparency"), int):
            info.alpha_is_transparency = bool(data["alphaIsTransparency"])
        if isinstance(data.get("alphaUsage"), int):
            info.alpha_usage = data["alphaUsage"]
        settings = data.get("textureSettings") or {}
        if isinstance(settings, dict):
            if isinstance(settings.get("wrapU"), int):
                info.wrap_u = settings["wrapU"]
            if isinstance(settings.get("wrapV"), int):
                info.wrap_v = settings["wrapV"]
        return info

    @property
    def is_normal_map(self) -> bool:
        return self.texture_type == TEXTURE_TYPE_NORMAL_MAP

    @property
    def is_linear(self) -> bool:
        """Blender の Non-Color として読むべきか。"""
        return self.is_normal_map or not self.srgb

    @property
    def clamps(self) -> bool:
        return self.wrap_u == 1 and self.wrap_v == 1
