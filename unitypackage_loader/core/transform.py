"""Unity の Transform（位置・回転・スケール）の行列計算と、Unity → Blender の座標変換。bpy 非依存。

行列は 4x4 の行優先タプル（列ベクトルに左から掛ける ``p' = M p``）。Unity の ``localToWorldMatrix`` と同じ並び。

Unity（左手系・Y が上）のモデル空間の点 (x, y, z) は、Blender（右手系・Z が上）では (-x, -z, y) になる。
これは Unity 6 で Blender 由来の合成 FBX を置いたシーンと、同じ FBX を Blender の FBX インポーターで読んだ結果を
突き合わせて確かめた（Issue #48）。Unity 上でモデルのルートに掛かっている行列 M は、Blender では
``C · M · C⁻¹``（C は上の基底変換。行列式 -1 どうしで打ち消し合うので鏡映にはならない）になり、
原点に読み込んだモデルのオブジェクトの行列に左から掛ければ Unity 上と同じ位置・向き・大きさになる。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]  # Unity の並び (x, y, z, w)
Mat4 = tuple[tuple[float, float, float, float], ...]

IDENTITY: Mat4 = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))

# Unity のモデル空間 → Blender のワールド。(x, y, z) → (-x, -z, y)
UNITY_TO_BLENDER: Mat4 = ((-1.0, 0.0, 0.0, 0.0), (0.0, 0.0, -1.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
BLENDER_TO_UNITY: Mat4 = ((-1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def normalize_quat(q: Quat) -> Quat:
    """長さ 1 にする。長さ 0（壊れた値）なら単位回転。"""
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if not math.isfinite(n) or n < 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return (x / n, y / n, z / n, w / n)


def trs(position: Vec3, rotation: Quat, scale: Vec3) -> Mat4:
    """Unity の ``Matrix4x4.TRS`` と同じ行列（平行移動 · 回転 · スケール）。"""
    x, y, z, w = normalize_quat(rotation)
    sx, sy, sz = scale
    r = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    return (
        (r[0][0] * sx, r[0][1] * sy, r[0][2] * sz, position[0]),
        (r[1][0] * sx, r[1][1] * sy, r[1][2] * sz, position[1]),
        (r[2][0] * sx, r[2][1] * sy, r[2][2] * sz, position[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def multiply(a: Mat4, b: Mat4) -> Mat4:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)) for i in range(4))  # type: ignore[return-value]


def chain(matrices: Sequence[Mat4]) -> Mat4:
    """親から順に並べた行列の積。"""
    result = IDENTITY
    for m in matrices:
        result = multiply(result, m)
    return result


def transform_point(m: Mat4, p: Vec3) -> Vec3:
    return (
        m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
        m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
        m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3],
    )


def inverse_affine(m: Mat4) -> Mat4 | None:
    """最下行が (0, 0, 0, 1) の行列の逆行列。特異（スケール 0 など）なら None。"""
    a, b, c = m[0][:3], m[1][:3], m[2][:3]
    det = a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    if not math.isfinite(det) or abs(det) < 1e-12:
        return None
    inv = (
        ((b[1] * c[2] - b[2] * c[1]) / det, (a[2] * c[1] - a[1] * c[2]) / det, (a[1] * b[2] - a[2] * b[1]) / det),
        ((b[2] * c[0] - b[0] * c[2]) / det, (a[0] * c[2] - a[2] * c[0]) / det, (a[2] * b[0] - a[0] * b[2]) / det),
        ((b[0] * c[1] - b[1] * c[0]) / det, (a[1] * c[0] - a[0] * c[1]) / det, (a[0] * b[1] - a[1] * b[0]) / det),
    )
    t = (m[0][3], m[1][3], m[2][3])
    return tuple(  # type: ignore[return-value]
        (*inv[i], -(inv[i][0] * t[0] + inv[i][1] * t[1] + inv[i][2] * t[2])) for i in range(3)
    ) + ((0.0, 0.0, 0.0, 1.0),)


def unity_to_blender(m: Mat4) -> Mat4:
    """Unity のモデル空間で掛かる行列を、Blender のワールドで同じ働きをする行列にする（C · M · C⁻¹）。"""
    return multiply(multiply(UNITY_TO_BLENDER, m), BLENDER_TO_UNITY)


def is_finite(m: Mat4) -> bool:
    return all(math.isfinite(v) for row in m for v in row)
