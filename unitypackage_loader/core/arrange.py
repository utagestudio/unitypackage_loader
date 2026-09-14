"""複数の読み込み単位（prefab など）を、重ならないように並べる位置の計算。bpy 非依存。

外形は Blender のワールド座標の軸平行バウンディングボックス（最小点, 最大点）で受け取る。
1 つ目の単位は動かさず、2 つ目以降を +X 方向に並べる。数が多いときは格子状に折り返し、
次の行は +Y 方向（正面が -Y のモデルから見て後ろ）に置く。行の中では手前側（Y の最小）を揃える。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Vec3 = tuple[float, float, float]
Bounds = tuple[Vec3, Vec3]

SINGLE_ROW_MAX = 4  # この数までは折り返さず 1 列に並べる
MIN_GAP = 0.1  # 単位どうしの最小の間隔（m）
GAP_RATIO = 0.25  # 間隔は、いちばん大きい単位の幅・奥行きに対するこの割合（MIN_GAP 未満にはしない）

_EMPTY: Bounds = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))


def grid_columns(count: int) -> int:
    """1 行に並べる数。少なければ 1 行、多ければほぼ正方形の格子にする。"""
    if count <= SINGLE_ROW_MAX:
        return max(count, 1)
    return math.ceil(math.sqrt(count))


def arrange_offsets(bounds: Sequence[Bounds | None]) -> list[tuple[float, float]]:
    """各単位に加える (X, Y) の移動量。外形が無い単位（メッシュが無いなど）は ``None`` で、原点の点として扱う。"""
    count = len(bounds)
    if count == 0:
        return []
    boxes = [b if b is not None else _EMPTY for b in bounds]
    widths = [max(mx[0] - mn[0], 0.0) for mn, mx in boxes]
    depths = [max(mx[1] - mn[1], 0.0) for mn, mx in boxes]
    gap = max(MIN_GAP, GAP_RATIO * max(max(widths), max(depths)))
    columns = grid_columns(count)

    offsets: list[tuple[float, float]] = []
    origin_x = boxes[0][0][0]
    row_y = boxes[0][0][1]
    for row_start in range(0, count, columns):
        row = range(row_start, min(row_start + columns, count))
        cursor_x = origin_x
        for i in row:
            mn = boxes[i][0]
            offsets.append((cursor_x - mn[0], row_y - mn[1]))
            cursor_x += widths[i] + gap
        row_y += max(depths[i] for i in row) + gap
    return offsets
