"""FBX ファイルの単位（GlobalSettings の UnitScaleFactor）を読む。bpy 非依存。

Unity は FBX の単位から fileScale（= UnitScaleFactor / 100。センチメートルの FBX は 0.01）を決め、ノードの値の
単位にする。古い形式の .meta には fileScale が無いので、モデルの中のノードへの位置の上書きを Blender の
オブジェクトに当てるとき（Issue #53）に、FBX から直接読む。

対応する書き方:

- バイナリ FBX: ``UnitScaleFactor`` の文字列属性（S）の後に、文字列属性がいくつか続き、数値属性（D / F / I / L）が値
- ASCII FBX 7: ``P: "UnitScaleFactor", "double", "Number", "",100``
- ASCII FBX 6: ``Property: "UnitScaleFactor", "double", "",1``
"""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path

_NAME = b"UnitScaleFactor"
_BINARY_MAGIC = b"Kaydara FBX Binary"
_ASCII_RE = re.compile(rb'(?:P|Property):\s*"UnitScaleFactor"\s*,\s*"[^"]*"\s*,(?:\s*"[^"]*"\s*,)*\s*([-+0-9.eE]+)')
_MAX_STRING_PROPERTIES = 4  # 名前の後、値までに挟まる文字列属性の数の上限
_READ_LIMIT = 64 << 20  # 巨大なファイルは先頭と末尾だけを見る（GlobalSettings はどちらかの近くにある）


def _valid(value: float) -> float | None:
    return value if math.isfinite(value) and value > 0 else None


def parse_unit_scale(data: bytes) -> float | None:
    """FBX の中身から UnitScaleFactor を返す。見つからない・壊れていれば None。"""
    if data.startswith(_BINARY_MAGIC):
        return _binary_unit_scale(data)
    match = _ASCII_RE.search(data)
    if match is None:
        return None
    try:
        return _valid(float(match.group(1)))
    except ValueError:
        return None


def _binary_unit_scale(data: bytes) -> float | None:
    marker = b"S" + struct.pack("<I", len(_NAME)) + _NAME
    start = data.find(marker)
    while start >= 0:
        value = _value_after(data, start + len(marker))
        if value is not None:
            return value
        start = data.find(marker, start + 1)
    return None


def _value_after(data: bytes, offset: int) -> float | None:
    try:
        for _ in range(_MAX_STRING_PROPERTIES + 1):
            kind = data[offset : offset + 1]
            if kind == b"S":
                (length,) = struct.unpack_from("<I", data, offset + 1)
                offset += 5 + length
                continue
            if kind == b"D":
                return _valid(struct.unpack_from("<d", data, offset + 1)[0])
            if kind == b"F":
                return _valid(struct.unpack_from("<f", data, offset + 1)[0])
            if kind == b"I":
                return _valid(float(struct.unpack_from("<i", data, offset + 1)[0]))
            if kind == b"L":
                return _valid(float(struct.unpack_from("<q", data, offset + 1)[0]))
            return None
    except struct.error:
        return None
    return None


def read_unit_scale(path: str | Path) -> float | None:
    """FBX ファイルの UnitScaleFactor。読めなければ None。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_READ_LIMIT)
            value = parse_unit_scale(head)
            if value is not None:
                return value
            handle.seek(0, 2)
            size = handle.tell()
            if size <= _READ_LIMIT:
                return None
            handle.seek(max(size - _READ_LIMIT, 0))
            tail = handle.read(_READ_LIMIT)
    except OSError:
        return None
    return _ascii_or_binary_tail(head, tail)


def _ascii_or_binary_tail(head: bytes, tail: bytes) -> float | None:
    if head.startswith(_BINARY_MAGIC):
        return _binary_unit_scale(tail)
    match = _ASCII_RE.search(tail)
    if match is None:
        return None
    try:
        return _valid(float(match.group(1)))
    except ValueError:
        return None
