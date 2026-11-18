"""``asset.meta`` から必要なインポーター設定だけを取り出す。

Unity 4 以前に書き出したパッケージは ``asset.meta`` の代わりに ``metaData``（取り込み結果とインポーターを入れた
バイナリの SerializedFile）を持つ。モデルの ``m_FileIDToRecycleName`` と ``m_GlobalScale`` だけを ``legacy_meta`` で読む（#233）。
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from .unity_binary import parse_serialized_file
from .unity_yaml import UnityRef, parse_text

__all__ = [
    "LEGACY_GLOBAL_SCALE",
    "MetaInfo",
    "ModelImporterInfo",
    "TextureImporterInfo",
    "legacy_meta",
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


# legacy_meta が Unity 4 の m_GlobalScale を入れるキー（テキストの .meta には無い名前にして、区別する）
LEGACY_GLOBAL_SCALE = "legacyGlobalScale"


def legacy_meta(data: bytes) -> MetaInfo | None:
    """Unity 4 以前の ``metaData`` を読む。モデル（ModelImporter）でなければ None。

    ヘッダーのファイルサイズまでが SerializedFile で、その後ろにはプレビューの PNG が続く。モデルの中のオブジェクトの
    fileID → 名前の表（``m_FileIDToRecycleName``）を、テキストの .meta と同じ ``fileIDToRecycleName`` の形にする。
    ``m_GlobalScale`` は ``LEGACY_GLOBAL_SCALE`` に入れる（Unity 4 は FBX の単位を見ず、頂点にこの値だけを掛ける。
    ``ModelImporterInfo.legacy_global_scale``）。壊れたデータには ``ValueError`` 系を送出する。
    """
    if len(data) < 20:
        raise ValueError("metaData too small")
    size = struct.unpack_from(">I", data, 4)[0]
    if not 20 <= size <= len(data):
        raise ValueError(f"metaData header size {size} out of range")
    for doc in parse_serialized_file(data[:size], compact=True):
        if doc.type_name != "ModelImporter":
            continue
        table: dict[int, str] = {}
        items = doc.body.get("m_FileIDToRecycleName")
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("first"), int) and isinstance(item.get("second"), str):
                table[item["first"]] = item["second"]
        data: dict[str, Any] = {"fileIDToRecycleName": table}
        scale = doc.body.get("m_GlobalScale")
        if isinstance(scale, (int, float)) and math.isfinite(scale) and scale > 0:
            data[LEGACY_GLOBAL_SCALE] = float(scale)
        return MetaInfo(guid="", importer="ModelImporter", data=data)
    return None


_NUMERIC_SUFFIX_RE = re.compile(r"\.[0-9]{3,}$")


def strip_numeric_suffix(name: str) -> str:
    """``Body.001`` / ``Pole.1003`` → ``Body`` / ``Pole``。Unity / Blender が付ける連番サフィックスを外す。

    Blender は同名が 1000 を超えると ``.1000`` 以上の連番を付ける（#67）。元の名前が数字で終わる場合と区別できないので、
    照合する側は「完全一致 → 外した形」の順で引く。
    """
    match = _NUMERIC_SUFFIX_RE.search(name)
    return name[: match.start()] if match else name


def _unit(unit_scale: float | None) -> float:
    return unit_scale if unit_scale and math.isfinite(unit_scale) and unit_scale > 0 else 1.0


@dataclass
class ModelImporterInfo:
    external_materials: dict[str, str] = field(default_factory=dict)  # FBX 内マテリアル名 → .mat GUID
    material_import_mode: int | None = None
    global_scale: float = 1.0
    use_file_scale: bool = True
    # Bake Axis Conversion（meshes.bakeAxisConversion）。有効なら Unity は Y が上でない FBX の軸の変換を頂点に焼き込み、
    # 無効（既定）ならルートの回転として持つ（#240）
    bake_axis_conversion: bool = False
    import_blend_shapes: bool = True
    # モデルの中のオブジェクトの fileID → 名前（//RootNode はルート）。Unity 2018.2 以前の形式の fileIDToRecycleName と、
    # 古い番号を引き継いだ internalIDToNameTable から読む。新しい形式（ハッシュの fileID）では空
    recycle_names: dict[int, str] = field(default_factory=dict)
    # Unity 4 以前（metaData）のモデルの m_GlobalScale。Unity 4 は FBX の単位（UnitScaleFactor）を見ず、頂点の値に
    # これだけを掛ける（Japanese School Cafeteria: cm の FBX も m の FBX も 0.01 で、頂点はどちらも cm の値）。
    # Unity 5 以降のモデルでは None
    legacy_global_scale: float | None = None

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
        table = data.get("fileIDToRecycleName")
        if isinstance(table, dict):
            for key, name in table.items():
                try:
                    info.recycle_names[int(key)] = str(name)
                except (TypeError, ValueError):
                    continue
        # 古い番号のまま新しい Unity で開き直したモデルは、同じ表を internalIDToNameTable（first: {クラス ID: fileID}）に持つ
        internal = data.get("internalIDToNameTable")
        for item in internal if isinstance(internal, list) else []:
            first = item.get("first") if isinstance(item, dict) else None
            if not isinstance(first, dict) or item.get("second") is None:
                continue
            for file_id in first.values():
                try:
                    info.recycle_names.setdefault(int(file_id), str(item["second"]))
                except (TypeError, ValueError):
                    continue
        legacy = data.get(LEGACY_GLOBAL_SCALE)
        if isinstance(legacy, float):
            info.legacy_global_scale = legacy
        materials = data.get("materials") or {}
        if isinstance(materials, dict) and isinstance(materials.get("materialImportMode"), int):
            info.material_import_mode = materials["materialImportMode"]
        meshes = data.get("meshes") or {}
        if isinstance(meshes, dict):
            scale = meshes.get("globalScale")
            if isinstance(scale, (int, float)):
                info.global_scale = float(scale)
            info.use_file_scale = bool(meshes.get("useFileScale", 1))
            info.bake_axis_conversion = bool(meshes.get("bakeAxisConversion", 0))
            info.import_blend_shapes = bool(meshes.get("importBlendShapes", 1))
        return info

    def import_scale(self, unit_scale: float | None) -> float:
        """FBX を Blender で読むときに掛ける倍率（Unity 4 のモデルを Unity と同じ大きさにする）。それ以外は 1。

        Blender の FBX インポーターは UnitScaleFactor / 100 を掛けるが、Unity 4 は頂点に m_GlobalScale だけを掛ける。
        ``unit_scale`` は FBX の UnitScaleFactor（読めなければ None。Blender と同じく 1 とみなす）。
        """
        if self.legacy_global_scale is None:
            return 1.0
        return self.legacy_global_scale * 100.0 / _unit(unit_scale)

    def unity_unit_scale(self, unit_scale: float | None) -> float | None:
        """配置の計算で、Unity の fileScale を求めるのに使う UnitScaleFactor。

        ``import_scale`` で読み込んだ Unity 4 のモデルは、fileScale が m_GlobalScale と同じになる値にする。
        """
        if self.legacy_global_scale is None:
            return unit_scale
        return self.legacy_global_scale * 100.0

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
