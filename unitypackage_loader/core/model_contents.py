"""走査のときに、モデルの中身から覚えておく補助情報（FBX のノード名・マテリアル名、OBJ のグループ名）。bpy 非依存。

``UnityPackage.scan`` が、モデルの実体を捨てる前に ``note_model_contents`` を呼んで ``AssetEntry`` に書き込む（#263 で分けた）。
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from .model_ids import parse_fbx_material_names, parse_fbx_node_names, parse_fbx_top_nodes

if TYPE_CHECKING:
    from .package import AssetEntry


def obj_group_names(data: bytes) -> list[str]:
    """OBJ のグループ（``g`` の行）の名前。Unity はグループごとにメッシュを作り、グループが無ければ ``default`` 1 つにする（#214）。"""
    names: dict[str, None] = {}
    for line in data.splitlines():
        if line.startswith(b"g ") or line.startswith(b"g\t"):
            name = line[2:].strip().decode("utf-8", "replace")
            if name:
                names.setdefault(name, None)
    return list(names)


def note_model_contents(entry: AssetEntry, data: bytes) -> None:
    """FBX の中身から、ノードが 1 つだけならその名前を、最上位のノードが 1 つだけならその名前を覚える（#181 / #185）。

    マテリアルの名前も覚える（読み込む前に、使う .mat とテクスチャを決めるため。#164）。OBJ はグループの名前を覚える（#214）。
    """
    if entry.ext == ".obj":
        entry.obj_groups = obj_group_names(data)
        return
    if entry.ext != ".fbx":
        return
    try:
        names = parse_fbx_node_names(data)
        tops = parse_fbx_top_nodes(data)
    except (ValueError, IndexError, struct.error):
        return
    try:
        entry.fbx_materials = parse_fbx_material_names(data) if names else None
    except (ValueError, IndexError, struct.error):
        entry.fbx_materials = None
    if names and sum(names.values()) == 1:
        entry.fbx_sole_node = next(iter(names))
    if tops is not None and len(tops) == 1:
        entry.fbx_top_node = tops[0]
    elif tops:
        entry.fbx_top_nodes = tuple(tops)
