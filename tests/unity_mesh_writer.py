"""テスト用に、Unity の Mesh（クラス 43）の YAML のドキュメントを組み立てる（Issue #216）。実在アセットのデータは使わない。"""

from __future__ import annotations

import struct


def mesh_document(
    file_id: int,
    name: str,
    positions: list[tuple[float, float, float]],
    submeshes: list[list[int]],
    *,
    normals: list[tuple[float, float, float]] | None = None,
    uvs: list[tuple[float, float]] | None = None,
    wide_indices: bool = False,
) -> str:
    """Unity の座標の頂点と、サブメッシュごとの頂点番号の並び（3 つずつで三角形）から Mesh のドキュメントを作る。

    頂点は 1 つのストリームに 位置（float × 3）・法線（float × 3）・UV0（float × 2）の順に並べる。
    """
    count = len(positions)
    channels = [(0, 0, 0, 3)]
    stride = 12
    if normals is not None:
        channels.append((0, stride, 0, 3))
        stride += 12
    else:
        channels.append((0, 0, 0, 0))
    channels += [(0, 0, 0, 0)] * 2  # 接線・色
    if uvs is not None:
        channels.append((0, stride, 0, 2))
        stride += 8
    else:
        channels.append((0, 0, 0, 0))
    channels += [(0, 0, 0, 0)] * (14 - len(channels))
    vertex = bytearray()
    for i in range(count):
        vertex += struct.pack("<3f", *positions[i])
        if normals is not None:
            vertex += struct.pack("<3f", *normals[i])
        if uvs is not None:
            vertex += struct.pack("<2f", *uvs[i])
    index = bytearray()
    subs = []
    code = "I" if wide_indices else "H"
    for indices in submeshes:
        subs.append((len(index), len(indices)))
        index += struct.pack(f"<{len(indices)}{code}", *indices)
    sub_text = "".join(
        f"  - serializedVersion: 2\n    firstByte: {first}\n    indexCount: {n}\n    topology: 0\n    baseVertex: 0\n"
        f"    firstVertex: 0\n    vertexCount: {count}\n" for first, n in subs
    )
    channel_text = "".join(
        f"    - stream: {s}\n      offset: {o}\n      format: {f}\n      dimension: {d}\n" for s, o, f, d in channels
    )
    return (
        f"--- !u!43 &{file_id}\nMesh:\n  m_Name: {name}\n  serializedVersion: 10\n  m_SubMeshes:\n{sub_text}"
        f"  m_IndexFormat: {1 if wide_indices else 0}\n  m_IndexBuffer: {index.hex()}\n  m_MeshCompression: 0\n"
        f"  m_VertexData:\n    serializedVersion: 3\n    m_VertexCount: {count}\n    m_Channels:\n{channel_text}"
        f"    m_DataSize: {len(vertex)}\n    _typelessdata: {vertex.hex()}\n"
        "  m_StreamData:\n    serializedVersion: 2\n    offset: 0\n    size: 0\n    path: \n"
    )
