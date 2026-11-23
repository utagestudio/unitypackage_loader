"""シーン・prefab に置いたモデルの行列の計算（FBX の単位と軸、ノードへの上書き、Renderer の位置）。bpy 非依存。

行列は ``core/transform.py`` と同じ 4x4 の行優先タプル。Blender のオブジェクトへの当てはめ（親をたどった行列、
アーマチュアで変形するものの除外）は ``blender/scene_objects.py`` が行う（#263 で分けた）。

記号:

- C: Unity → Blender の基底（``transform.UNITY_TO_BLENDER``）
- A: Blender のオブジェクトのローカル座標（FBX のファイルの座標）→ Unity のノード空間（``ModelUnits.basis_a``）
- L: Unity のノード空間での、ノードの行列

原点に読み込んだ Blender のオブジェクトの行列 N と L の関係は N = C·L·A（Issue #53 で確かめた）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .fbx_units import blender_axis_matrix, unity_axis_matrix
from .transform import (
    IDENTITY,
    UNITY_TO_BLENDER,
    Mat4,
    Quat,
    Vec3,
    chain,
    inverse_affine,
    multiply,
    trs,
    unity_to_blender,
)


def _to_mat4(m3) -> Mat4:
    return tuple((*(float(v) for v in m3[i]), 0.0) for i in range(3)) + ((0.0, 0.0, 0.0, 1.0),)  # type: ignore[return-value]


def _diagonal(x: float, y: float, z: float) -> Mat4:
    return ((x, 0.0, 0.0, 0.0), (0.0, y, 0.0, 0.0), (0.0, 0.0, z, 0.0), (0.0, 0.0, 0.0, 1.0))


def _inverse(m: Mat4) -> Mat4:
    """逆行列。特異なら単位行列（呼ぶ側の行列は、スケールが正の値に限っているので特異にならない）。"""
    return inverse_affine(m) or IDENTITY


def file_scale(unit_scale: float | None) -> float:
    """Unity の fileScale（= FBX の UnitScaleFactor / 100）。読めなければ 1 として扱う。"""
    return (unit_scale if unit_scale and math.isfinite(unit_scale) and unit_scale > 0 else 1.0) / 100.0


def positive_scale(scale: float) -> float | None:
    """有効な倍率（有限で正）ならその値、そうでなければ None。"""
    return scale if math.isfinite(scale) and scale > 0 else None


@dataclass(frozen=True)
class ModelUnits:
    """配置の計算に使う、モデルの FBX の単位と軸（``read_unit_scale`` / ``read_axes``。FBX 以外は空）。"""

    unit_scale: float | None = None
    axes: tuple[int, ...] | None = None
    global_scale: float = 1.0  # .meta の globalScale（Unity は頂点と子の位置に焼き込む）

    def basis_a(self) -> Mat4:
        """Blender のオブジェクトのローカル座標（FBX のファイルの座標）→ Unity のノード空間（A = f·U）。

        U は Unity がこの FBX に掛ける軸の変換（``fbx_units.unity_axis_matrix``）。Y が上の FBX では diag(-1, 1, 1) で、
        A = diag(-f, f, f)（#53 で確かめた形）。3ds Max の Z が上の FBX では Y と Z の入れ替え（Cardboard Boxes Pack。#240）。
        """
        f = file_scale(self.unit_scale)
        return multiply(_diagonal(f, f, f), _to_mat4(unity_axis_matrix(self.axes)))

    def world_basis_a(self) -> Mat4:
        """``basis_a`` に globalScale も掛けたもの（ルートの Empty の下で、親の逆行列を通さずにワールドで置くとき）。"""
        g = positive_scale(self.global_scale) or 1.0
        return multiply(_diagonal(g, g, g), self.basis_a())


def axis_correction(axes: tuple[int, ...] | None) -> Mat4 | None:
    """Y が上でない FBX を読み込んだ直後に、最上位のオブジェクトへ左から掛けて Unity と同じ向きにする行列（K = C·U·B⁻¹）。

    Blender の FBX インポーターはファイルの上の軸を +Z にする（B。Z が上の 3ds Max の FBX では回転を掛けない）が、Unity は
    U で Y が上にする（#240）。Y が上の FBX では K が単位行列（C·diag(-1, 1, 1) = B）なので None。
    """
    if axes is None or axes[0] != 2:
        return None
    unity = _to_mat4(unity_axis_matrix(axes))
    blender = _to_mat4(blender_axis_matrix(axes))
    return chain((UNITY_TO_BLENDER, unity, _inverse(blender)))


def _quat_from_rotation(r) -> Quat:
    """直交行列（行優先 3x3）の回転を Unity の並びの四元数 (x, y, z, w) にする。"""
    trace = r[0][0] + r[1][1] + r[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        return ((r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s, 0.25 * s)
    if r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(max(1.0 + r[0][0] - r[1][1] - r[2][2], 0.0)) * 2 or 1.0
        return (0.25 * s, (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s, (r[2][1] - r[1][2]) / s)
    if r[1][1] > r[2][2]:
        s = math.sqrt(max(1.0 + r[1][1] - r[0][0] - r[2][2], 0.0)) * 2 or 1.0
        return ((r[0][1] + r[1][0]) / s, 0.25 * s, (r[1][2] + r[2][1]) / s, (r[0][2] - r[2][0]) / s)
    s = math.sqrt(max(1.0 + r[2][2] - r[0][0] - r[1][1], 0.0)) * 2 or 1.0
    return ((r[0][2] + r[2][0]) / s, (r[1][2] + r[2][1]) / s, 0.25 * s, (r[1][0] - r[0][1]) / s)


def decompose(m: Mat4) -> tuple[Vec3, Quat, Vec3]:
    """位置・回転（Unity の並びの四元数）・スケールに分ける（``mathutils.Matrix.decompose`` と同じ約束）。

    スケールは各列の長さ。行列式が負（鏡映）なら、回転とスケールの 3 成分すべての符号を反転する。
    """
    location = (m[0][3], m[1][3], m[2][3])
    columns = [[m[0][j], m[1][j], m[2][j]] for j in range(3)]
    scale = [math.sqrt(sum(v * v for v in col)) for col in columns]
    normalized = [[v / s for v in col] if s > 0 else [0.0, 0.0, 0.0] for col, s in zip(columns, scale)]
    a, b, c = columns
    det = a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    if det < 0:
        normalized = [[-v for v in col] for col in normalized]
        scale = [-s for s in scale]
    rotation = [[normalized[j][i] for j in range(3)] for i in range(3)]  # 列から行優先に戻す
    return location, _quat_from_rotation(rotation), (scale[0], scale[1], scale[2])


def node_basis(basis: Mat4, spec: dict, basis_a: Mat4) -> Mat4:
    """ノードへの上書き ``spec``（``position`` / ``rotation`` / ``scale`` の成分ごと。None は上書きなし）を当てた行列（C·M·A）。

    Unity のノード空間の行列 L と、原点に読み込んだ Blender のオブジェクトの行列 N の関係は N = C·L·A
    （C は Unity → Blender の基底、A は ``ModelUnits.basis_a``。Y が上の FBX では diag(-f, f, f)、f は Unity の fileScale
    = FBX の UnitScaleFactor / 100）。Japanese Apartment の FBX（UnitScaleFactor 100 と 1）と Blender 由来の FBX で、
    Unity 6 が読んだノードの値と突き合わせて確かめた（Issue #53）。元のノード値を L = C⁻¹·N·A⁻¹ で求め、上書きの無い成分は
    その値のまま使う。
    """
    return local_basis(basis, spec, UNITY_TO_BLENDER, basis_a)


def local_values(basis: Mat4, left: Mat4, right: Mat4) -> tuple[list[float], list[float], list[float]]:
    """行列 basis = left·L·right の、Unity のローカルの値 L（位置, 回転のクォータニオン, スケール）。"""
    location, rotation, scale = decompose(chain((_inverse(left), basis, _inverse(right))))
    return list(location), list(rotation), list(scale)


def local_basis(basis: Mat4, spec: dict, left: Mat4, right: Mat4) -> Mat4:
    """basis = left·L·right の L の成分を ``spec`` で置き換えた行列（``node_basis`` は left = C、right = A）。

    FBX の中で親が FBX のノードのオブジェクトは left = A⁻¹、right = A（親子とも C·W·A の形のため）。
    シーンの GameObject の Empty は left = C、right = C⁻¹（#209 のアニメーションで使う）。
    """
    location, rotation, scale = local_values(basis, left, right)
    values = {"position": location, "rotation": rotation, "scale": scale}
    for key, current in values.items():
        for index, value in enumerate(spec.get(key, [])):
            if value is not None and index < len(current):
                current[index] = value
    local = trs(tuple(values["position"]), tuple(values["rotation"]), tuple(values["scale"]))  # type: ignore[arg-type]
    return chain((left, local, right))


def collapsed_root(basis: Mat4, spec: dict, basis_a: Mat4) -> tuple[Mat4, Mat4]:
    """1 メッシュの FBX をモデルの PrefabInstance で置いた配置の、ルートの Empty とオブジェクトの行列（#99）。

    この形の FBX では Unity のモデルのルートの Transform の値が FBX のノードの変換 L で、シーンの上書きは
    その成分を置き換える（上書きしなかった成分は L のまま）。ルートの Empty は C·M·C⁻¹（M = L に上書きを当てた行列）、
    オブジェクトはノードを単位行列にした C·A にする。``basis`` は原点に読み込んだときのオブジェクトの行列。
    """
    neutral = multiply(UNITY_TO_BLENDER, basis_a)
    return multiply(node_basis(basis, spec, basis_a), _inverse(neutral)), neutral


def renderer_world(root_world: Mat4, relative: Mat4, world_basis_a: Mat4) -> Mat4:
    """FBX を展開した配置で、Renderer の GameObject に置くオブジェクトのワールド行列 E·C·R·A（#186）。

    ``root_world`` はルートの Empty のワールド行列 E、``relative`` は Unity でのルートから GameObject までの行列 R。
    ``world_basis_a`` は ``ModelUnits.world_basis_a``（ワールドで直接置くので globalScale も含める。#240）。
    """
    return chain((root_world, UNITY_TO_BLENDER, relative, world_basis_a))


def offset_world(offset: Mat4, root_world: Mat4, scale: float, current: Mat4) -> Mat4:
    """中のノードが上書きで動いたオブジェクトのワールド行列。

    置いた直後のオブジェクトの行列 ``current`` は「ルートの行列 · globalScale · 原点に読み込んだときの行列」なので、
    そこから原点での行列を求め、ノードから逆算したルートの行列 ``offset``（Unity の行列）を掛け直す。
    """
    s = positive_scale(scale)
    scale_matrix = _diagonal(s, s, s) if s is not None else IDENTITY
    to_origin = _inverse(multiply(unity_to_blender(root_world), scale_matrix))
    return chain((unity_to_blender(offset), scale_matrix, to_origin, current))


def same_placement(a: Mat4, b: Mat4) -> bool:
    """2 つの行列が、float の誤差を除いて同じ置き方か（列の長さに対する相対誤差と、位置の誤差）。

    位置は 0.1 mm に、座標の大きさに比例する float の誤差（100 万分の 1）を足した範囲で比べる。座標に比例させるだけだと、
    原点から離れた部品の数 cm のずれを見逃す（Farm の 216 m 先の門扉で 2 cm）。
    """
    size = max(1.0, *(math.sqrt(sum(a[r][i] ** 2 for r in range(3))) for i in range(3)))
    if any(abs(a[r][c] - b[r][c]) > 1e-4 * size for r in range(3) for c in range(3)):
        return False
    return all(abs(a[r][3] - b[r][3]) <= 1e-4 + 1e-6 * abs(a[r][3]) for r in range(3))
