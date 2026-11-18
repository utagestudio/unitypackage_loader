"""Unity の Terrain（TerrainData と TerrainLayer）を読み、メッシュと塗り分けの材料にする（Issue #205）。bpy 非依存。

Terrain は、シーンの GameObject の Terrain コンポーネント（クラス 218）が ``.asset`` の TerrainData（クラス 156）を指す形で
置かれる。TerrainData はほぼ必ずバイナリで、高さマップ（``m_Heightmap``）、穴（``m_Holes``）、塗り分け（``m_SplatDatabase``:
TerrainLayer の参照と、同じファイルの Texture2D のアルファマップ）、木と草（``m_DetailDatabase``）を持つ。

- 高さマップ: ``m_Resolution``（2019.3 より前は ``m_Width`` / ``m_Height``）× 同じ数の 16 ビットの値。``m_Heights[z * 幅 + x]``
  が 0〜32766 で、32766 が ``m_Scale.y`` の高さ。点の間隔は ``m_Scale.x`` / ``m_Scale.z``。Terrain の原点は角で、+X・+Z に広がる。
- 穴: 高さの点の間の (幅 − 1) × (奥行き − 1) のマス。0 が穴。
- アルファマップ: レイヤー k の重みは k // 4 枚目の k % 4 チャンネル。テクスチャの左下（1 行目）が Terrain の原点の角。
- TerrainLayer: テクスチャ（ディフューズ・法線・マスク）と、``m_TileSize``（テクスチャ 1 枚が覆う大きさ）・``m_TileOffset``。
  Unity のシェーダーは、Terrain 全体の UV に ``大きさ / TileSize`` を掛けて ``TileOffset / TileSize`` を足した UV で読む。
  2018.3 より前は TerrainData の ``m_Splats``（SplatPrototype）に同じ値を持つ。

Terrain は Transform の回転・拡大縮小を受けない（位置だけを使う）。木と草は読まない（数を警告に出す）。
"""

from __future__ import annotations

import struct
import zlib
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field

from .transform import Mat4, inverse_affine, multiply
from .unity_binary import load_documents
from .unity_yaml import UnityRef, ref_guid, to_float

__all__ = [
    "CLASS_TERRAIN_DATA",
    "CLASS_TERRAIN_LAYER",
    "MAX_HEIGHT",
    "Alphamap",
    "TerrainData",
    "TerrainError",
    "TerrainLayer",
    "TerrainMesh",
    "grid_indices",
    "layer_uv_transform",
    "png_rgba",
    "read_terrain_data",
    "read_terrain_layer",
    "terrain_data_ref",
    "terrain_local",
    "terrain_mesh",
]

CLASS_TEXTURE_2D = 28
CLASS_TERRAIN_DATA = 156
CLASS_TERRAIN_LAYER = 1953259897
MAX_HEIGHT = 32766  # 高さマップの値で、m_Scale.y の高さを表すもの
_FORMAT_RGBA32 = 4
_FORMAT_ARGB32 = 5
MAX_RESOLUTION = 8193  # Unity の高さマップの上限は 4097。細工されたファイルで巨大な配列を作らないための上限


class TerrainError(ValueError):
    """TerrainData として読めないとき。"""


@dataclass
class TerrainLayer:
    """TerrainLayer 1 つ（2018.3 より前は SplatPrototype）。テクスチャは GUID。"""

    name: str = ""
    diffuse: str | None = None
    normal: str | None = None
    mask: str | None = None
    tile_size: tuple[float, float] = (15.0, 15.0)
    tile_offset: tuple[float, float] = (0.0, 0.0)
    metallic: float = 0.0
    smoothness: float = 0.0
    normal_scale: float = 1.0
    diffuse_remap_max: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    mask_remap_min: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    mask_remap_max: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    source: str | None = None  # .terrainlayer の GUID（SplatPrototype なら None）

    def textures(self) -> list[str]:
        return [g for g in (self.diffuse, self.normal, self.mask) if g]

    def as_dict(self) -> dict:
        return {
            "name": self.name, "diffuse": self.diffuse, "normal": self.normal, "mask": self.mask,
            "tile_size": list(self.tile_size), "tile_offset": list(self.tile_offset),
            "metallic": self.metallic, "smoothness": self.smoothness, "normal_scale": self.normal_scale,
            "diffuse_remap_max": list(self.diffuse_remap_max),
            "mask_remap_min": list(self.mask_remap_min), "mask_remap_max": list(self.mask_remap_max),
            "source": self.source,
        }


@dataclass
class Alphamap:
    """塗り分けの重み 1 枚（RGBA の 4 レイヤー分）。``pixels`` は RGBA の 8 ビットで、1 行目が Terrain の原点側（下）。"""

    name: str
    width: int
    height: int
    pixels: bytes


@dataclass
class TerrainData:
    name: str
    columns: int  # 高さマップの X 方向の点の数
    rows: int  # Z 方向の点の数
    scale: tuple[float, float, float]  # 点の間隔 (x, z) と、高さの最大値 y
    heights: Sequence[int]  # rows × columns（Z の行ごと）
    holes: bytes | None = None  # (rows − 1) × (columns − 1)。0 が穴。無ければ None
    layer_refs: list[str] = field(default_factory=list)  # .terrainlayer の GUID（パッケージに無いものは空文字列）
    legacy_layers: list[TerrainLayer] = field(default_factory=list)  # 2018.3 より前の SplatPrototype
    alphamaps: list[Alphamap] = field(default_factory=list)
    trees: int = 0  # 木の数（読まない）
    detail_prototypes: int = 0  # 草の種類の数（読まない）
    notes: list[str] = field(default_factory=list)  # 読めなかったものの説明

    @property
    def size(self) -> tuple[float, float, float]:
        """Terrain の大きさ（X の幅、高さの最大値、Z の奥行き）。"""
        sx, sy, sz = self.scale
        return ((self.columns - 1) * sx, sy, (self.rows - 1) * sz)

    @property
    def hole_count(self) -> int:
        return self.holes.count(0) if self.holes is not None else 0


def terrain_data_ref(body: dict) -> str | None:
    """Terrain コンポーネントの中身から、TerrainData の GUID。"""
    return ref_guid(body.get("m_TerrainData"))


def _vec(value: object, keys: str, default: tuple[float, ...]) -> tuple[float, ...]:
    if not isinstance(value, dict):
        return default
    return tuple(to_float(value.get(k), d) for k, d in zip(keys, default))


def _layer_from(body: dict, source: str | None) -> TerrainLayer:
    """TerrainLayer の中身（と、同じ名前のフィールドを持つ SplatPrototype）から。"""
    diffuse = body.get("m_DiffuseTexture", body.get("texture"))
    normal = body.get("m_NormalMapTexture", body.get("normalMap"))
    name = body.get("m_Name")
    return TerrainLayer(
        name=name if isinstance(name, str) else "",
        diffuse=ref_guid(diffuse),
        normal=ref_guid(normal),
        mask=ref_guid(body.get("m_MaskMapTexture")),
        tile_size=_vec(body.get("m_TileSize", body.get("tileSize")), "xy", (15.0, 15.0)),  # type: ignore[arg-type]
        tile_offset=_vec(body.get("m_TileOffset", body.get("tileOffset")), "xy", (0.0, 0.0)),  # type: ignore[arg-type]
        metallic=to_float(body.get("m_Metallic", body.get("metallic")), 0.0),
        smoothness=to_float(body.get("m_Smoothness", body.get("smoothness")), 0.0),
        normal_scale=to_float(body.get("m_NormalScale"), 1.0),
        diffuse_remap_max=_vec(body.get("m_DiffuseRemapMax"), "xyzw", (1.0, 1.0, 1.0, 1.0)),  # type: ignore[arg-type]
        mask_remap_min=_vec(body.get("m_MaskMapRemapMin"), "xyzw", (0.0, 0.0, 0.0, 0.0)),  # type: ignore[arg-type]
        mask_remap_max=_vec(body.get("m_MaskMapRemapMax"), "xyzw", (1.0, 1.0, 1.0, 1.0)),  # type: ignore[arg-type]
        source=source,
    )


def read_terrain_layer(data: bytes | str, guid: str | None = None) -> TerrainLayer | None:
    """.terrainlayer を読む。TerrainLayer が無ければ None。"""
    for doc in load_documents(data):
        if doc.class_id == CLASS_TERRAIN_LAYER and isinstance(doc.body, dict):
            return _layer_from(doc.body, guid)
    return None


def _as_ints(value: object, what: str) -> Sequence[int]:
    """バイナリ（compact）の ``array`` か、YAML のリスト。"""
    if isinstance(value, array):
        return value
    if isinstance(value, list) and all(isinstance(v, int) for v in value[:16]):
        return value
    raise TerrainError(f"TerrainData has no readable {what}")


def _alphamap(body: dict) -> Alphamap | str:
    """Texture2D の中身からアルファマップ。読めなければ理由。"""
    name = body.get("m_Name") if isinstance(body.get("m_Name"), str) else "alphamap"
    width, height = int(to_float(body.get("m_Width"), 0)), int(to_float(body.get("m_Height"), 0))
    fmt = int(to_float(body.get("m_TextureFormat"), 0))
    data = body.get("image data")
    if not isinstance(data, (bytes, bytearray)) or not data:
        stream = body.get("m_StreamData")
        if isinstance(stream, dict) and stream.get("path"):
            return f"alphamap {name} is stored outside the TerrainData"
        return f"alphamap {name} has no pixel data"
    if not (0 < width <= MAX_RESOLUTION and 0 < height <= MAX_RESOLUTION):
        return f"alphamap {name} has an invalid size {width}x{height}"
    size = width * height * 4
    if fmt not in (_FORMAT_RGBA32, _FORMAT_ARGB32) or len(data) < size:
        return f"alphamap {name} uses an unsupported texture format {fmt}"
    pixels = bytes(data[:size])
    if fmt == _FORMAT_ARGB32:
        argb = memoryview(pixels)
        rgba = bytearray(size)
        rgba[0::4] = argb[1::4]
        rgba[1::4] = argb[2::4]
        rgba[2::4] = argb[3::4]
        rgba[3::4] = argb[0::4]
        pixels = bytes(rgba)
    return Alphamap(name, width, height, pixels)


def read_terrain_data(data: bytes) -> TerrainData:
    """TerrainData の .asset を読む。読めなければ ``ValueError``（``TerrainError`` か ``UnityBinaryError``）。"""
    docs = load_documents(data, compact=True)
    by_id = {doc.file_id: doc for doc in docs}
    body = next((d.body for d in docs if d.class_id == CLASS_TERRAIN_DATA and isinstance(d.body, dict)), None)
    if body is None:
        raise TerrainError("no TerrainData in the asset")
    heightmap = body.get("m_Heightmap")
    if not isinstance(heightmap, dict):
        raise TerrainError("TerrainData has no heightmap")
    if "m_Resolution" in heightmap:
        columns = rows = int(to_float(heightmap.get("m_Resolution"), 0))
    else:  # 2019.3 より前
        columns, rows = int(to_float(heightmap.get("m_Width"), 0)), int(to_float(heightmap.get("m_Height"), 0))
    if not (2 <= columns <= MAX_RESOLUTION and 2 <= rows <= MAX_RESOLUTION):
        raise TerrainError(f"invalid heightmap resolution {columns}x{rows}")
    heights = _as_ints(heightmap.get("m_Heights"), "heights")
    if len(heights) != columns * rows:
        raise TerrainError(f"heightmap has {len(heights)} values for {columns}x{rows}")
    scale = _vec(heightmap.get("m_Scale"), "xyz", (1.0, 1.0, 1.0))
    holes_value = heightmap.get("m_Holes")
    holes = None
    if isinstance(holes_value, (array, list)) and len(holes_value) == (columns - 1) * (rows - 1):
        raw = bytes(holes_value) if isinstance(holes_value, array) and holes_value.itemsize == 1 else bytes(
            1 if v else 0 for v in holes_value
        )
        holes = raw if raw.count(0) else None

    name = body.get("m_Name") if isinstance(body.get("m_Name"), str) else "Terrain"
    result = TerrainData(name, columns, rows, scale, heights, holes)  # type: ignore[arg-type]

    splat = body.get("m_SplatDatabase") if isinstance(body.get("m_SplatDatabase"), dict) else {}
    for ref in splat.get("m_TerrainLayers") or []:
        result.layer_refs.append(ref_guid(ref) or "")
    for prototype in splat.get("m_Splats") or []:
        if isinstance(prototype, dict):
            result.legacy_layers.append(_layer_from(prototype, None))
    for ref in splat.get("m_AlphaTextures") or []:
        doc = by_id.get(ref.file_id) if isinstance(ref, UnityRef) and not ref.guid else None
        if doc is None or doc.class_id != CLASS_TEXTURE_2D or not isinstance(doc.body, dict):
            result.notes.append("an alphamap is missing from the TerrainData")
            continue
        alphamap = _alphamap(doc.body)
        if isinstance(alphamap, str):
            result.notes.append(alphamap)
        else:
            result.alphamaps.append(alphamap)

    details = body.get("m_DetailDatabase") if isinstance(body.get("m_DetailDatabase"), dict) else {}
    trees = details.get("m_TreeInstances")
    result.trees = len(trees) if isinstance(trees, list) else 0
    prototypes = details.get("m_DetailPrototypes")
    result.detail_prototypes = len(prototypes) if isinstance(prototypes, list) else 0
    return result


# ---------------------------------------------------------------------------
# メッシュ
# ---------------------------------------------------------------------------


def grid_indices(count: int, max_points: int) -> list[int]:
    """高さマップの点 ``count`` 個から、``max_points`` 個以下を等間隔に選ぶ（両端は必ず含める）。0 以下なら全部。"""
    if max_points <= 1 or count <= max_points:
        return list(range(count))
    step = -(-(count - 1) // (max_points - 1))  # 切り上げ
    indices = list(range(0, count, step))
    if indices[-1] != count - 1:
        indices.append(count - 1)
    return indices


@dataclass
class TerrainMesh:
    """Blender の座標系のメッシュ。面はすべて四角形。"""

    vertices: array  # 'f'、x y z の並び
    uvs: array  # 'f'、頂点ごとの u v（Terrain 全体で 0〜1。アルファマップと同じ向き）
    faces: array  # 'i'、四角形ごとに 4 つの頂点番号
    columns: int  # X 方向の点の数（間引いた後）
    rows: int
    removed_holes: int = 0  # 穴として外した面の数


def terrain_mesh(td: TerrainData, max_points: int = 0) -> TerrainMesh:
    """TerrainData の高さマップから、1 辺 ``max_points`` 点以下に間引いたメッシュを作る（0 以下なら元の解像度）。

    Unity の Terrain のローカル (x, y, z) を、Blender の (-x, -z, y) にする（``core/transform.py``）。面の法線が上（+Z）を
    向くように、頂点を (i, j) → (i+1, j) → (i+1, j+1) → (i, j+1) の順に並べる（鏡映で回る向きが逆になるため）。
    穴は、間引いた面が覆う元のマスの半分より多くが穴なら面を作らない。
    """
    xs = grid_indices(td.columns, max_points)
    zs = grid_indices(td.rows, max_points)
    sx, sy, sz = td.scale
    height_scale = sy / MAX_HEIGHT
    nx, nz = len(xs), len(zs)
    u_scale = 1.0 / (td.columns - 1)
    v_scale = 1.0 / (td.rows - 1)
    xs_blender = [-i * sx for i in xs]
    us = [i * u_scale for i in xs]
    full_row = len(xs) == td.columns
    vertices = array("f")
    uvs = array("f")
    heights = td.heights
    for j in zs:
        start = j * td.columns
        row = heights[start : start + td.columns] if full_row else [heights[start + i] for i in xs]
        y_blender = -j * sz
        v = j * v_scale
        for x, u, h in zip(xs_blender, us, row):
            vertices.extend((x, y_blender, h * height_scale))
            uvs.extend((u, v))

    hole_blocks = _hole_blocks(td, xs, zs)
    faces = array("i")
    removed = 0
    for b in range(nz - 1):
        base = b * nx
        for a in range(nx - 1):
            if hole_blocks and (a, b) in hole_blocks:
                removed += 1
                continue
            v0 = base + a
            faces.extend((v0, v0 + 1, v0 + 1 + nx, v0 + nx))
    return TerrainMesh(vertices, uvs, faces, nx, nz, removed)


def _hole_blocks(td: TerrainData, xs: list[int], zs: list[int]) -> set[tuple[int, int]]:
    """間引いた面 (a, b) のうち、覆う元のマスの半分より多くが穴のもの。"""
    holes = td.holes
    if holes is None:
        return set()
    width = td.columns - 1
    block_x = _block_of(xs, width)
    block_z = _block_of(zs, td.rows - 1)
    counts: dict[tuple[int, int], int] = {}
    position = holes.find(0)
    while position >= 0:
        j, i = divmod(position, width)
        key = (block_x[i], block_z[j])
        counts[key] = counts.get(key, 0) + 1
        position = holes.find(0, position + 1)
    result = set()
    for (a, b), count in counts.items():
        area = (xs[a + 1] - xs[a]) * (zs[b + 1] - zs[b])
        if count * 2 > area:
            result.add((a, b))
    return result


def _block_of(indices: list[int], cells: int) -> list[int]:
    """元のマスの番号 → それを含む間引いた面の番号。"""
    result = [0] * cells
    for block in range(len(indices) - 1):
        for cell in range(indices[block], indices[block + 1]):
            result[cell] = block
    return result


def terrain_local(world: Mat4) -> Mat4:
    """Terrain の GameObject の Unity のワールド行列 ``world`` から、その GameObject の空間で Terrain を置く行列。

    Terrain は回転と拡大縮小を受けず、ワールドの位置に軸に沿って置かれるので、GameObject の行列の逆を掛けて打ち消す。
    """
    inverse = inverse_affine(world)
    translation = (
        (1.0, 0.0, 0.0, world[0][3]), (0.0, 1.0, 0.0, world[1][3]), (0.0, 0.0, 1.0, world[2][3]), (0.0, 0.0, 0.0, 1.0),
    )
    return multiply(inverse, translation) if inverse is not None else translation


def layer_uv_transform(layer: TerrainLayer, size: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """Terrain 全体の UV（0〜1）からレイヤーのテクスチャの UV にする (u の倍率, v の倍率, u のずれ, v のずれ)。"""
    tile_x = layer.tile_size[0] or 1.0
    tile_y = layer.tile_size[1] or 1.0
    return (size[0] / tile_x, size[2] / tile_y, layer.tile_offset[0] / tile_x, layer.tile_offset[1] / tile_y)


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def png_rgba(width: int, height: int, pixels: bytes) -> bytes:
    """8 ビット RGBA の PNG。``pixels`` は 1 行目が下（Unity のテクスチャと同じ）で、PNG は上の行から書くので逆順に並べる。"""
    stride = width * 4
    if len(pixels) < stride * height:
        raise ValueError("not enough pixel data")
    view = memoryview(pixels)
    raw = bytearray()
    for row in range(height - 1, -1, -1):
        raw += b"\x00"
        raw += view[row * stride : (row + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + _chunk(b"IEND", b"")

