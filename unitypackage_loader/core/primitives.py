"""Unity の組み込みメッシュ（Cube / Sphere / Capsule / Cylinder / Plane / Quad）を Blender の座標で作る（Issue #190）。

シーンの Renderer が、パッケージの外にある Unity 組み込みのメッシュ（``Library/unity default resources``、
GUID ``0000000000000000e000000000000000``）を指すことがある。地面に敷いた Quad や Plane など。メッシュのデータは
パッケージに無いので、同じ形・寸法・UV をここで作る。

Unity 6000.6 の ``GameObject.CreatePrimitive`` が作るメッシュと突き合わせた値:

- Quad（fileID 10210）: XY 平面の 1 m 角。表は -Z 向き。UV は (-0.5, -0.5) が (0, 0)、(0.5, 0.5) が (1, 1)
- Plane（10209）: XZ 平面の 10 m 角を 10 × 10 に分割。表は +Y 向き。UV は (5, 0, 5) が (0, 0)、(-5, 0, -5) が (1, 1)
- Cube（10202）: 1 m 角。面ごとの UV は Unity と同じ並び
- Sphere（10207）: 半径 0.5。Cylinder（10206）: 半径 0.5・高さ 2。Capsule（10208）: 半径 0.5・高さ 2

Sphere / Cylinder / Capsule は分割数と UV の細部が Unity と違う（寸法と向き、UV の大まかな並びは合わせる）。

座標は Unity の (x, y, z) を Blender の (-x, -z, y) に直す（``transform.py`` の C）。C は鏡映を含むので、面の頂点の
並びはそのままだと裏返る。面ごとに、Unity での外向きの法線を直した向きと、Blender の並び（反時計回りが表）から
求めた向きを比べて、逆なら並びを反転する。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

Vec3 = tuple[float, float, float]
UV = tuple[float, float]

BUILTIN_MESH_GUID = "0000000000000000e000000000000000"
# 組み込みメッシュの fileID → 名前（Unity の Mesh.name と同じ）
BUILTIN_MESHES: dict[int, str] = {
    10202: "Cube",
    10206: "Cylinder",
    10207: "Sphere",
    10208: "Capsule",
    10209: "Plane",
    10210: "Quad",
}


def builtin_mesh_kind(guid: str | None, file_id: int) -> str | None:
    """メッシュ参照が Unity の組み込みメッシュなら、その名前（Cube など）。"""
    if guid is None or guid.lower() != BUILTIN_MESH_GUID:
        return None
    return BUILTIN_MESHES.get(file_id)


@dataclass
class PrimitiveMesh:
    """Blender の座標で作ったメッシュ。``uvs`` は面ごとの頂点の並びと同じ順の UV。"""

    name: str
    vertices: list[Vec3] = field(default_factory=list)
    faces: list[tuple[int, ...]] = field(default_factory=list)
    uvs: list[tuple[UV, ...]] = field(default_factory=list)
    smooth: list[bool] = field(default_factory=list)


class _Builder:
    def __init__(self, name: str) -> None:
        self.mesh = PrimitiveMesh(name)

    def vertex(self, p: Vec3) -> int:
        x, y, z = p
        self.mesh.vertices.append((-x, -z, y))  # Unity → Blender
        return len(self.mesh.vertices) - 1

    def face(self, corners: list[int], uvs: list[UV], normal: Vec3, smooth: bool = False) -> None:
        """``normal`` は Unity の座標での外向きの法線。Blender の並びの向きと合うように並べ直す。"""
        nx, ny, nz = normal
        want = (-nx, -nz, ny)
        if _dot(_polygon_normal([self.mesh.vertices[i] for i in corners]), want) < 0:
            corners = corners[::-1]
            uvs = uvs[::-1]
        self.mesh.faces.append(tuple(corners))
        self.mesh.uvs.append(tuple(uvs))
        self.mesh.smooth.append(smooth)


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _polygon_normal(points: list[Vec3]) -> Vec3:
    """反時計回りを表とする多角形の法線（Newell の方法。正規化しない）。"""
    nx = ny = nz = 0.0
    for i, (x1, y1, z1) in enumerate(points):
        x2, y2, z2 = points[(i + 1) % len(points)]
        nx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return (nx, ny, nz)


def _quad() -> PrimitiveMesh:
    b = _Builder("Quad")
    corners = [b.vertex(p) for p in ((-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0))]
    b.face(corners, [(0, 0), (1, 0), (1, 1), (0, 1)], (0, 0, -1))
    return b.mesh


def _plane() -> PrimitiveMesh:
    b = _Builder("Plane")
    grid = [[b.vertex((5.0 - i, 0.0, 5.0 - j)) for i in range(11)] for j in range(11)]
    for j in range(10):
        for i in range(10):
            corners = [grid[j][i], grid[j][i + 1], grid[j + 1][i + 1], grid[j + 1][i]]
            uvs = [(i / 10, j / 10), ((i + 1) / 10, j / 10), ((i + 1) / 10, (j + 1) / 10), (i / 10, (j + 1) / 10)]
            b.face(corners, uvs, (0, 1, 0))
    return b.mesh


# Cube の面: (法線, 4 頂点の座標と UV)。Unity の CreatePrimitive のメッシュと同じ値
_CUBE_FACES: list[tuple[Vec3, list[tuple[Vec3, UV]]]] = [
    ((0, 0, 1), [((0.5, -0.5, 0.5), (0, 0)), ((-0.5, -0.5, 0.5), (1, 0)), ((-0.5, 0.5, 0.5), (1, 1)), ((0.5, 0.5, 0.5), (0, 1))]),
    ((0, 1, 0), [((0.5, 0.5, 0.5), (0, 0)), ((-0.5, 0.5, 0.5), (1, 0)), ((-0.5, 0.5, -0.5), (1, 1)), ((0.5, 0.5, -0.5), (0, 1))]),
    ((0, 0, -1), [((0.5, 0.5, -0.5), (0, 0)), ((-0.5, 0.5, -0.5), (1, 0)), ((-0.5, -0.5, -0.5), (1, 1)), ((0.5, -0.5, -0.5), (0, 1))]),
    ((0, -1, 0), [((0.5, -0.5, -0.5), (0, 0)), ((-0.5, -0.5, -0.5), (1, 0)), ((-0.5, -0.5, 0.5), (1, 1)), ((0.5, -0.5, 0.5), (0, 1))]),
    ((-1, 0, 0), [((-0.5, -0.5, 0.5), (0, 0)), ((-0.5, -0.5, -0.5), (1, 0)), ((-0.5, 0.5, -0.5), (1, 1)), ((-0.5, 0.5, 0.5), (0, 1))]),
    ((1, 0, 0), [((0.5, -0.5, -0.5), (0, 0)), ((0.5, -0.5, 0.5), (1, 0)), ((0.5, 0.5, 0.5), (1, 1)), ((0.5, 0.5, -0.5), (0, 1))]),
]


def _cube() -> PrimitiveMesh:
    b = _Builder("Cube")
    for normal, corners in _CUBE_FACES:
        b.face([b.vertex(p) for p, _ in corners], [uv for _, uv in corners], normal)
    return b.mesh


def _ring(y: float, radius: float, segments: int) -> list[Vec3]:
    """Y 軸まわりの円周（最初と最後に同じ位置を置き、UV の継ぎ目を分ける）。"""
    return [
        (-radius * math.sin(2 * math.pi * s / segments), y, -radius * math.cos(2 * math.pi * s / segments))
        for s in range(segments + 1)
    ]


def _lathe(b: _Builder, profile: list[tuple[float, float, float]], segments: int, core: tuple[float, float]) -> None:
    """``profile``（y, 半径, v）を Y 軸まわりに回した面を張る。半径 0 の段は極（三角形）にする。

    外向きの法線は、面の中心から Y 軸上の芯（y が ``core`` の範囲の線分）の最も近い点を引いた向き。
    """
    rings = [_ring(y, r, segments) for y, r, _ in profile]
    rows = [[b.vertex(p) for p in ring] for ring in rings]
    for k in range(len(profile) - 1):
        (_, r0, v0), (_, r1, v1) = profile[k], profile[k + 1]
        for s in range(segments):
            u0, u1 = s / segments, (s + 1) / segments
            points = [rings[k][s], rings[k][s + 1], rings[k + 1][s + 1], rings[k + 1][s]]
            cx, cy, cz = (sum(p[i] for p in points) / 4 for i in range(3))
            normal = (cx, cy - min(max(cy, core[0]), core[1]), cz)
            if r0 == 0:
                corners, uvs = [rows[k][s], rows[k + 1][s + 1], rows[k + 1][s]], [((u0 + u1) / 2, v0), (u1, v1), (u0, v1)]
            elif r1 == 0:
                corners, uvs = [rows[k][s], rows[k][s + 1], rows[k + 1][s]], [(u0, v0), (u1, v0), ((u0 + u1) / 2, v1)]
            else:
                corners = [rows[k][s], rows[k][s + 1], rows[k + 1][s + 1], rows[k + 1][s]]
                uvs = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]
            b.face(corners, uvs, normal, True)


def _sphere(segments: int = 24, rings: int = 16) -> PrimitiveMesh:
    b = _Builder("Sphere")
    profile = []
    for k in range(rings + 1):
        theta = math.pi * k / rings  # 下の極から上の極へ
        profile.append((-0.5 * math.cos(theta), 0.5 * math.sin(theta) if 0 < k < rings else 0.0, k / rings))
    _lathe(b, profile, segments, (0.0, 0.0))
    return b.mesh


def _capsule(segments: int = 24, cap_rings: int = 8) -> PrimitiveMesh:
    b = _Builder("Capsule")
    profile = []
    for k in range(cap_rings + 1):  # 下の半球（y = -1 から -0.5）
        theta = 0.5 * math.pi * k / cap_rings
        profile.append((-0.5 - 0.5 * math.cos(theta), 0.5 * math.sin(theta) if k else 0.0, 0.25 * k / cap_rings))
    for k in range(cap_rings + 1):  # 上の半球（y = 0.5 から 1）
        theta = 0.5 * math.pi * k / cap_rings
        profile.append((0.5 + 0.5 * math.sin(theta), 0.5 * math.cos(theta) if k < cap_rings else 0.0, 0.75 + 0.25 * k / cap_rings))
    _lathe(b, profile, segments, (-0.5, 0.5))
    return b.mesh


def _cylinder(segments: int = 20) -> PrimitiveMesh:
    b = _Builder("Cylinder")
    _lathe(b, [(-1.0, 0.5, 0.0), (1.0, 0.5, 1.0)], segments, (-1.0, 1.0))
    for y, ny in ((1.0, 1.0), (-1.0, -1.0)):  # ふた（側面と頂点を分けて、平らに見せる）
        points = _ring(y, 0.5, segments)[:-1]
        corners = [b.vertex(p) for p in points]
        uvs = [(0.5 - p[0], 0.5 - p[2] * ny) for p in points]
        b.face(corners, uvs, (0, ny, 0))
    return b.mesh


_BUILDERS = {"Quad": _quad, "Plane": _plane, "Cube": _cube, "Sphere": _sphere, "Capsule": _capsule, "Cylinder": _cylinder}


def build_primitive(kind: str) -> PrimitiveMesh:
    """組み込みメッシュ ``kind``（``BUILTIN_MESHES`` の値）を Blender の座標で作る。"""
    return _BUILDERS[kind]()
