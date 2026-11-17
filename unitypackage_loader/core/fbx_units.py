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


# ---------------------------------------------------------------------------
# 軸（GlobalSettings の UpAxis / FrontAxis / CoordAxis。#240）
# ---------------------------------------------------------------------------

AXIS_NAMES = ("UpAxis", "UpAxisSign", "FrontAxis", "FrontAxisSign", "CoordAxis", "CoordAxisSign")
Y_UP_AXES = (1, 1, 2, 1, 0, 1)  # Maya・Blender が書き出す FBX（Y が上、前が +Z、横が +X）


def _int_after(data: bytes, offset: int) -> int | None:
    try:
        for _ in range(_MAX_STRING_PROPERTIES + 1):
            kind = data[offset : offset + 1]
            if kind == b"S":
                (length,) = struct.unpack_from("<I", data, offset + 1)
                offset += 5 + length
                continue
            if kind == b"I":
                return struct.unpack_from("<i", data, offset + 1)[0]
            if kind == b"L":
                return struct.unpack_from("<q", data, offset + 1)[0]
            return None
    except struct.error:
        return None
    return None


def _int_property(data: bytes, name: str) -> int | None:
    raw = name.encode()
    if data.startswith(_BINARY_MAGIC):
        marker = b"S" + struct.pack("<I", len(raw)) + raw
        start = data.find(marker)
        while start >= 0:
            value = _int_after(data, start + len(marker))
            if value is not None:
                return value
            start = data.find(marker, start + 1)
        return None
    match = re.search(rb'(?:P|Property):\s*"' + re.escape(raw) + rb'"\s*,\s*"[^"]*"\s*,(?:\s*"[^"]*"\s*,)*\s*([-+0-9]+)', data)
    return int(match.group(1)) if match else None


def parse_axes(data: bytes) -> tuple[int, ...] | None:
    """FBX の軸の設定（``AXIS_NAMES`` の順）。どれかが読めないか、軸が重なっていれば None。"""
    values = tuple(_int_property(data, name) for name in AXIS_NAMES)
    if any(v is None for v in values):
        return None
    up, up_sign, front, front_sign, coord, coord_sign = values  # type: ignore[misc]
    if sorted((up, front, coord)) != [0, 1, 2] or {up_sign, front_sign, coord_sign} - {1, -1}:
        return None
    return values  # type: ignore[return-value]


def read_axes(path: str | Path) -> tuple[int, ...] | None:
    """FBX ファイルの軸の設定。読めなければ None（GlobalSettings は UnitScaleFactor と同じく先頭か末尾の近くにある）。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_READ_LIMIT)
            axes = parse_axes(head)
            if axes is not None:
                return axes
            handle.seek(0, 2)
            size = handle.tell()
            if size <= _READ_LIMIT:
                return None
            handle.seek(max(size - _READ_LIMIT, 0))
            tail = handle.read(_READ_LIMIT)
    except OSError:
        return None
    return parse_axes(head[:64] + tail) if head.startswith(_BINARY_MAGIC) else parse_axes(tail)


def unity_axis_matrix(axes: tuple[int, ...] | None) -> list[list[float]]:
    """Unity がこの FBX の頂点に掛ける軸の変換（ファイルの座標 → Unity のノード空間の 3 × 3。fileScale は含まない）。

    Unity は左手系なので、Y が上の FBX では X を反転し（diag(-1, 1, 1)。#53 で確かめた形）、Z が上の FBX（3ds Max）では
    Y と Z を入れ替える（入れ替えだけで左手系になる。Cardboard Boxes Pack を Unity 6000.6.9f1 で開いた結果と突き合わせた。#240）。
    それ以外（X が上など、手元に例が無い）と、軸が分からないものは Y が上とみなす。
    """
    up = (axes or Y_UP_AXES)[0]
    if up == 2:
        return [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]]
    return [[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def blender_axis_matrix(axes: tuple[int, ...] | None) -> list[list[float]]:
    """Blender の FBX インポーターがこの FBX に掛ける軸の変換（ファイルの座標 → Blender の座標の 3 × 3）。

    ファイルの上の軸を +Z、前の軸を -Y、横の軸を +X にする。Y が上の FBX では X 軸のまわりの 90° 回転で、
    Unity → Blender の基底 C と X の反転の積（C·diag(-1, 1, 1)）と同じになる。軸が分からなければ Y が上とみなす。
    """
    up, up_sign, front, front_sign, coord, coord_sign = axes or Y_UP_AXES
    columns = {up: (0.0, 0.0, float(up_sign)), front: (0.0, -float(front_sign), 0.0), coord: (float(coord_sign), 0.0, 0.0)}
    return [[columns[c][r] for c in range(3)] for r in range(3)]
