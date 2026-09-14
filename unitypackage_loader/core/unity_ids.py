"""Unity がモデルの中のアセットに付ける fileID の計算。bpy 非依存。

新しい形式（.meta に番号の表が無い）では、モデルの中のアセット（Mesh / Material）の fileID は
``xxHash64("Type:<型名>-><名前><オフセット>")`` を符号付き 64 bit にしたもの（同じ型・名前が重なると 2 つ目は
オフセット 1）。Unity 6 の ``AssetImporter.MakeLocalFileIDWithHash`` と 28 通りで一致し（Issue #31 の調査）、
FBX を展開した prefab の MeshFilter の ``m_Mesh`` とも一致した（Issue #58）。メッシュの名前は FBX のノード名で、
Blender の FBX インポーターが付けるオブジェクト名と同じ。
"""

from __future__ import annotations

_MASK = (1 << 64) - 1
_P1 = 11400714785074694791
_P2 = 14029467366897019727
_P3 = 1609587929392839161
_P4 = 9650029242287828579
_P5 = 2870177450012600261


def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & _MASK


def _round(acc: int, value: int) -> int:
    return (_rotl((acc + value * _P2) & _MASK, 31) * _P1) & _MASK


def _merge(acc: int, value: int) -> int:
    return ((acc ^ _round(0, value)) * _P1 + _P4) & _MASK


def xxh64(data: bytes, seed: int = 0) -> int:
    """XXH64（符号なし 64 bit）。"""
    length = len(data)
    i = 0
    if length >= 32:
        v1 = (seed + _P1 + _P2) & _MASK
        v2 = (seed + _P2) & _MASK
        v3 = seed & _MASK
        v4 = (seed - _P1) & _MASK
        while i + 32 <= length:
            v1 = _round(v1, int.from_bytes(data[i : i + 8], "little"))
            v2 = _round(v2, int.from_bytes(data[i + 8 : i + 16], "little"))
            v3 = _round(v3, int.from_bytes(data[i + 16 : i + 24], "little"))
            v4 = _round(v4, int.from_bytes(data[i + 24 : i + 32], "little"))
            i += 32
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _MASK
        for v in (v1, v2, v3, v4):
            h = _merge(h, v)
    else:
        h = (seed + _P5) & _MASK
    h = (h + length) & _MASK
    while i + 8 <= length:
        h = (_rotl(h ^ _round(0, int.from_bytes(data[i : i + 8], "little")), 27) * _P1 + _P4) & _MASK
        i += 8
    if i + 4 <= length:
        h = (_rotl(h ^ (int.from_bytes(data[i : i + 4], "little") * _P1 & _MASK), 23) * _P2 + _P3) & _MASK
        i += 4
    while i < length:
        h = (_rotl(h ^ (data[i] * _P5 & _MASK), 11) * _P1) & _MASK
        i += 1
    h ^= h >> 33
    h = (h * _P2) & _MASK
    h ^= h >> 29
    h = (h * _P3) & _MASK
    h ^= h >> 32
    return h


def local_file_id(type_name: str, name: str, offset: int = 0) -> int:
    """新しい形式のモデルの中のアセットの fileID（符号付き 64 bit）。"""
    value = xxh64(f"Type:{type_name}->{name}{offset}".encode("utf-8"))
    return value - (1 << 64) if value >= (1 << 63) else value


def mesh_file_id(name: str, offset: int = 0) -> int:
    return local_file_id("Mesh", name, offset)
