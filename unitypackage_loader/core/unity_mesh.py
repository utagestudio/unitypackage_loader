"""Unity の Mesh（クラス 43）を読み、Blender の座標系の頂点と面にする（Issue #216）。bpy 非依存。

Mesh は ``.asset``（mainObjectFileID 4300000）に入るほか、UModeler のように prefab の中に埋め込まれることもある。
読むのは Unity 2018 以降の形（``m_VertexData`` の ``m_Channels`` と頂点のストリーム）で、圧縮していないもの。

- ``m_VertexData``: ``m_Channels`` の 14 チャンネル（位置・法線・接線・色・UV0〜7・ウェイト・ボーン番号）がそれぞれ
  ストリーム・ストリームの中での位置・形式・次元を持つ。ストリームは 0 から順に並び、ストリームごとの 1 頂点の大きさは
  そのストリームのチャンネルの大きさの和、次のストリームは 16 バイト境界から始まる（UnityPy / AssetStudio の記述と同じ）。
  データは YAML では ``_typelessdata`` の 16 進、バイナリでは TypelessData。
- ``m_IndexBuffer``: ``m_IndexFormat`` が 0 なら 16 ビット、1 なら 32 ビットの頂点番号。サブメッシュは ``firstByte`` からの
  ``indexCount`` 個で、``baseVertex`` を足す。三角形（``topology`` 0）だけを読む。

Unity の (x, y, z) は Blender の (-x, -z, y)（``core/transform.py``）。この変換は鏡映なので、面の頂点の並びを逆にすると
Blender の表（反時計回り）が Unity の表と同じ向きになる（実データの頂点の法線と照合して確かめた）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .unity_yaml import to_float

__all__ = ["MeshData", "MeshError", "CLASS_MESH", "read_mesh"]

CLASS_MESH = 43
_CHANNEL_POSITION, _CHANNEL_NORMAL, _CHANNEL_UV0 = 0, 1, 4
# 頂点の形式 → (struct の書式, 正規化の割り算。0 は正規化しない)
_FORMATS_2019 = {
    0: ("f", 0), 1: ("e", 0), 2: ("B", 255), 3: ("b", 127), 4: ("H", 65535), 5: ("h", 32767),
    6: ("B", 0), 7: ("b", 0), 8: ("H", 0), 9: ("h", 0), 10: ("I", 0), 11: ("i", 0),
}
# Unity 2017〜2018（Mesh の serializedVersion 9）の形式。2 は 4 バイトの色
_FORMATS_2017 = {
    0: ("f", 0), 1: ("e", 0), 2: ("B", 255), 3: ("B", 255), 4: ("b", 127), 5: ("H", 65535), 6: ("h", 32767),
    7: ("B", 0), 8: ("b", 0), 9: ("H", 0), 10: ("h", 0), 11: ("I", 0), 12: ("i", 0),
}
MAX_VERTICES = 16_777_216
_STREAM_ALIGN = 16


class MeshError(ValueError):
    """Mesh として読めないとき（圧縮・外部のストリーム・古い形・壊れたデータ）。"""


@dataclass
class MeshData:
    name: str
    vertices: list[tuple[float, float, float]]  # Blender の座標
    normals: list[tuple[float, float, float]] | None
    uvs: list[tuple[float, float]] | None
    submeshes: list[list[tuple[int, int, int]]] = field(default_factory=list)  # サブメッシュごとの三角形

    @property
    def triangle_count(self) -> int:
        return sum(len(s) for s in self.submeshes)


def _raw_bytes(value: object, what: str) -> bytes:
    """YAML の 16 進の文字列・バイナリの bytes・数値のリストを bytes にする。"""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return bytes.fromhex(value)
        except ValueError as exc:
            raise MeshError(f"invalid {what} data") from exc
    if isinstance(value, list):
        try:
            return bytes(value)
        except (TypeError, ValueError) as exc:
            raise MeshError(f"invalid {what} data") from exc
    if value is None or value == "":
        return b""
    raise MeshError(f"unreadable {what} data")


def _channels(vertex_data: dict) -> list[tuple[int, int, int, int]]:
    channels = vertex_data.get("m_Channels")
    if not isinstance(channels, list):
        raise MeshError("mesh has no vertex channels (older mesh layouts are not supported)")
    result = []
    for ch in channels:
        if not isinstance(ch, dict):
            raise MeshError("invalid vertex channel")
        result.append((
            int(to_float(ch.get("stream"), 0)), int(to_float(ch.get("offset"), 0)),
            int(to_float(ch.get("format"), 0)), int(to_float(ch.get("dimension"), 0)) & 0xF,
        ))
    return result


def read_mesh(body: dict) -> MeshData:
    """Mesh の中身（``UnityDocument.body``）を読む。読めなければ ``MeshError``（``ValueError`` の派生）。"""
    name = body.get("m_Name") if isinstance(body.get("m_Name"), str) else "Mesh"
    stream = body.get("m_StreamData")
    if isinstance(stream, dict) and stream.get("path"):
        raise MeshError(f"mesh {name} stores its vertices outside the asset")
    vertex_data = body.get("m_VertexData")
    if not isinstance(vertex_data, dict):
        raise MeshError(f"mesh {name} has no vertex data")
    count = int(to_float(vertex_data.get("m_VertexCount"), 0))
    if count <= 0 and int(to_float(body.get("m_MeshCompression"), 0)):
        raise MeshError(f"mesh {name} is compressed (Mesh Compression is not supported)")
    if not 0 < count <= MAX_VERTICES:
        raise MeshError(f"mesh {name} has an invalid vertex count {count}")
    # YAML は _typelessdata（m_DataSize はバイト数）、バイナリは m_DataSize そのものが TypelessData
    raw = vertex_data.get("_typelessdata")
    if raw is None and isinstance(vertex_data.get("m_DataSize"), (str, bytes, bytearray)):
        raw = vertex_data.get("m_DataSize")
    data = _raw_bytes(raw, "vertex")
    formats = _FORMATS_2019 if int(to_float(body.get("serializedVersion"), 10)) >= 10 else _FORMATS_2017
    channels = _channels(vertex_data)

    # ストリームごとの 1 頂点の大きさと先頭
    strides: dict[int, int] = {}
    for stream_index, _offset, fmt, dim in channels:
        if dim:
            if fmt not in formats:
                raise MeshError(f"mesh {name} uses an unknown vertex format {fmt}")
            strides[stream_index] = strides.get(stream_index, 0) + dim * struct.calcsize(formats[fmt][0])
    starts: dict[int, int] = {}
    position = 0
    for stream_index in range(max(strides, default=-1) + 1):
        starts[stream_index] = position
        position += count * strides.get(stream_index, 0)
        position = (position + _STREAM_ALIGN - 1) // _STREAM_ALIGN * _STREAM_ALIGN

    def channel(index: int, want: int) -> list[tuple[float, ...]] | None:
        if index >= len(channels):
            return None
        stream_index, offset, fmt, dim = channels[index]
        if dim < want:
            return None
        code, scale = formats[fmt]
        item = struct.Struct("<" + code * want)
        stride = strides[stream_index]
        base = starts[stream_index] + offset
        end = base + (count - 1) * stride + item.size
        if end > len(data):
            raise MeshError(f"mesh {name} vertex data is too short")
        values = [item.unpack_from(data, base + i * stride) for i in range(count)]
        if scale:
            values = [tuple(v / scale for v in value) for value in values]
        return values

    positions = channel(_CHANNEL_POSITION, 3)
    if positions is None:
        raise MeshError(f"mesh {name} has no positions")
    normals = channel(_CHANNEL_NORMAL, 3)
    uvs = channel(_CHANNEL_UV0, 2)

    index_bytes = _raw_bytes(body.get("m_IndexBuffer"), "index")
    wide = int(to_float(body.get("m_IndexFormat"), 0)) == 1
    index_size = 4 if wide else 2
    submeshes: list[list[tuple[int, int, int]]] = []
    for sub in body.get("m_SubMeshes") or []:
        if not isinstance(sub, dict):
            continue
        if int(to_float(sub.get("topology"), 0)) != 0:
            submeshes.append([])  # 三角形以外（線・点）は面にしない
            continue
        first = int(to_float(sub.get("firstByte"), 0))
        n = int(to_float(sub.get("indexCount"), 0))
        base_vertex = int(to_float(sub.get("baseVertex"), 0))
        if first < 0 or n < 0 or first + n * index_size > len(index_bytes):
            raise MeshError(f"mesh {name} index data is too short")
        indices = struct.unpack_from(f"<{n}{'I' if wide else 'H'}", index_bytes, first)
        tris = []
        for k in range(0, n - n % 3, 3):
            a, b, c = (indices[k] + base_vertex, indices[k + 1] + base_vertex, indices[k + 2] + base_vertex)
            if max(a, b, c) >= count or min(a, b, c) < 0:
                raise MeshError(f"mesh {name} has an index out of range")
            if a != b and b != c and a != c:
                tris.append((a, c, b))  # 鏡映で回る向きが変わるので逆に並べる
        submeshes.append(tris)
    if not any(submeshes):
        raise MeshError(f"mesh {name} has no triangles")

    def to_blender(v: tuple[float, ...]) -> tuple[float, float, float]:
        return (-v[0], -v[2], v[1])

    return MeshData(
        name,
        [to_blender(p) for p in positions],
        [to_blender(n) for n in normals] if normals else None,
        [(u[0], u[1]) for u in uvs] if uvs else None,
        submeshes,
    )
