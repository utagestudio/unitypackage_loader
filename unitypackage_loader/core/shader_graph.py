"""Shader Graph（.shadergraph / .shadersubgraph）のノードのつながりから、テクスチャの UV の Tiling & Offset を読む（Issue #193）。

Shader Graph のシェーダーは、テクスチャの ``m_Scale`` / ``m_Offset``（``_ST``）を使わず、グラフの中の Tiling And Offset
ノードで UV を変えることが多い。繰り返しの回数はマテリアルの Vector のプロパティ（UnityJapanOffice の ``_BaseUV``、
Synty の ``_Base_Tiling`` / ``_Base_Offset`` など）に入っている。プロパティ名から推測せずに、テクスチャのプロパティを読む
Sample Texture 2D の UV 入力をさかのぼり、次のノードだけを式として評価する（ほかのノードが混ざれば分からない扱い）。

- UV（チャンネル）、Tiling And Offset、Property（Float / Vector）、Split、Combine、Vector 2/3/4、Redirect
- Parallax Mapping / Parallax Occlusion Mapping（視線でずらす分は無視し、入力の UV をそのまま渡す近似）
- Sub Graph（中の .shadersubgraph をたどる。入力はサブグラフのプロパティに結び付ける）

結果は、UV0 を ``uv * scale + offset`` にする変換で、各成分は定数かマテリアルのプロパティの成分。2 つ目以降の UV で読む
テクスチャや、同じテクスチャを違う UV で読むものは返さない。グラフの形式は 2 つある。古い形式は 1 つの JSON で、ノードと
つながりは ``m_SerializableNodes`` / ``m_SerializableEdges`` の ``JSONnodeData``（JSON の文字列）。新しい形式は JSON の
オブジェクトを並べたもので、``m_ObjectId`` で参照し合う。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .material import TexRef, UnityMaterial

__all__ = ["GraphError", "UVTransform", "apply_uv_transforms", "texture_uv_transforms"]

_MAX_DEPTH = 64  # 式の深さ（サブグラフの入れ子を含む）の上限
_SAMPLERS = ("SampleTexture2DNode", "SampleTexture2DLODNode")
_PARALLAX = ("ParallaxOcclusionMappingNode", "ParallaxMappingNode")


class GraphError(ValueError):
    """Shader Graph として読めない。"""


@dataclass(frozen=True)
class UVTransform:
    """UV0 を ``uv * scale + offset`` にする変換。各成分は定数（float）か、(プロパティの参照名, 成分の番号)。"""

    scale: tuple
    offset: tuple


@dataclass
class _Slot:
    name: str
    is_input: bool
    data: dict


@dataclass
class _Node:
    kind: str  # 型の名前の最後（``TilingAndOffsetNode`` など）
    data: dict
    slots: dict[int, _Slot] = field(default_factory=dict)


@dataclass
class _Graph:
    nodes: dict[str, _Node]
    sources: dict[tuple[str, int], tuple[str, int]]  # (ノード, 入力スロット) → (ノード, 出力スロット)
    properties: dict[str, tuple[str, str]]  # プロパティの GUID → (参照名, 型の名前の最後)


def _decode_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    i = 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return objects
        obj, i = decoder.raw_decode(text, i)
        if isinstance(obj, dict):
            objects.append(obj)


def _short(type_name: object) -> str:
    return type_name.rsplit(".", 1)[-1] if isinstance(type_name, str) else ""


def _inner(item: object) -> dict:
    """古い形式の要素（``typeInfo`` + ``JSONnodeData``）の中身。"""
    if isinstance(item, dict) and isinstance(item.get("JSONnodeData"), str):
        data = json.loads(item["JSONnodeData"])
        if isinstance(data, dict):
            return data
    return {}


def _guid(data: dict) -> str:
    guid = data.get("m_Guid")
    return guid.get("m_GuidSerialized", "") if isinstance(guid, dict) else ""


def _reference(data: dict) -> str:
    name = data.get("m_OverrideReferenceName") or data.get("m_DefaultReferenceName") or data.get("m_Name") or ""
    return name if isinstance(name, str) else ""


def parse_graph(text: str) -> _Graph:
    """.shadergraph / .shadersubgraph の中身を、形式によらない形にする。"""
    try:
        objects = _decode_objects(text.lstrip("﻿"))
        if objects and "m_SerializableNodes" in objects[0]:
            return _parse_old(objects[0])
        return _parse_new(objects)
    except (json.JSONDecodeError, RecursionError, AttributeError, TypeError, KeyError) as exc:
        raise GraphError(f"not a shader graph: {exc!r}") from exc


def _slots(items: list, lookup: Callable[[object], dict]) -> dict[int, _Slot]:
    slots: dict[int, _Slot] = {}
    for item in items if isinstance(items, list) else ():
        data = lookup(item)
        if isinstance(data.get("m_Id"), int):
            slots[data["m_Id"]] = _Slot(str(data.get("m_DisplayName", "")), data.get("m_SlotType") == 0, data)
    return slots


def _parse_old(root: dict) -> _Graph:
    properties: dict[str, tuple[str, str]] = {}
    for item in root.get("m_SerializedProperties") or ():
        data = _inner(item)
        properties[_guid(data)] = (_reference(data), _short(item.get("typeInfo", {}).get("fullName")))
    nodes: dict[str, _Node] = {}
    for item in root.get("m_SerializableNodes") or ():
        data = _inner(item)
        node = _Node(_short(item.get("typeInfo", {}).get("fullName")), data, _slots(data.get("m_SerializableSlots"), _inner))
        if isinstance(data.get("m_PropertyGuidSerialized"), str):
            node.data = {**data, "_property": data["m_PropertyGuidSerialized"]}
        nodes[str(data.get("m_GuidSerialized", ""))] = node
    sources: dict[tuple[str, int], tuple[str, int]] = {}
    for item in root.get("m_SerializableEdges") or ():
        data = _inner(item)
        out, into = data.get("m_OutputSlot", {}), data.get("m_InputSlot", {})
        sources[(into.get("m_NodeGUIDSerialized"), into.get("m_SlotId"))] = (out.get("m_NodeGUIDSerialized"), out.get("m_SlotId"))
    return _Graph(nodes, sources, properties)


def _parse_new(objects: list[dict]) -> _Graph:
    by_id = {o["m_ObjectId"]: o for o in objects if isinstance(o.get("m_ObjectId"), str)}

    def ref(item: object) -> dict:
        key = item.get("m_Id") if isinstance(item, dict) else None
        found = by_id.get(key) if isinstance(key, str) else None
        return found if isinstance(found, dict) else {}

    root = next((o for o in objects if _short(o.get("m_Type")) in ("GraphData",)), objects[0] if objects else {})
    properties: dict[str, tuple[str, str]] = {}
    object_guids: dict[str, str] = {}  # プロパティの m_ObjectId → GUID
    for item in root.get("m_Properties") or ():
        data = ref(item)
        guid = _guid(data) or data.get("m_ObjectId", "")
        object_guids[data.get("m_ObjectId", "")] = guid
        properties[guid] = (_reference(data), _short(data.get("m_Type")))
    nodes: dict[str, _Node] = {}
    for item in root.get("m_Nodes") or ():
        data = ref(item)
        node = _Node(_short(data.get("m_Type")), data, _slots(data.get("m_Slots"), ref))
        prop = data.get("m_Property")
        if isinstance(prop, dict) and prop.get("m_Id") in object_guids:
            node.data = {**data, "_property": object_guids[prop["m_Id"]]}
        nodes[data.get("m_ObjectId", "")] = node
    sources: dict[tuple[str, int], tuple[str, int]] = {}
    for edge in root.get("m_Edges") or ():
        out, into = edge.get("m_OutputSlot", {}), edge.get("m_InputSlot", {})
        sources[(into.get("m_Node", {}).get("m_Id"), into.get("m_SlotId"))] = (out.get("m_Node", {}).get("m_Id"), out.get("m_SlotId"))
    return _Graph(nodes, sources, properties)


# ---------------------------------------------------------------- 評価

@dataclass(frozen=True)
class _UV:
    channel: int
    scale: tuple
    offset: tuple


_IDENTITY = ((1.0, 1.0), (0.0, 0.0))


class _Evaluator:
    def __init__(self, read_subgraph: Callable[[str], str | None]):
        self._read_subgraph = read_subgraph
        self._subgraphs: dict[str, _Graph | None] = {}
        self._memo: dict[tuple, object] = {}
        self._scopes: list[dict] = []  # 結び付けた入力（id で引くので、評価の間は捨てない）

    def subgraph(self, guid: str) -> _Graph | None:
        if guid not in self._subgraphs:
            self._subgraphs[guid] = None  # 読み込み中の入れ子（自分自身を含むもの）は分からない扱い
            text = self._read_subgraph(guid)
            try:
                self._subgraphs[guid] = parse_graph(text) if text else None
            except GraphError:
                self._subgraphs[guid] = None
        return self._subgraphs[guid]

    def input(self, graph: _Graph, node_id: str, slot_id: int, scope: dict, depth: int):
        """入力スロットの値。つながっていなければスロットの値。"""
        source = graph.sources.get((node_id, slot_id))
        if source is not None:
            return self.output(graph, source[0], source[1], scope, depth + 1)
        slot = graph.nodes[node_id].slots.get(slot_id)
        return _slot_default(slot) if slot is not None else None

    def named_input(self, graph: _Graph, node_id: str, name: str, scope: dict, depth: int):
        node = graph.nodes[node_id]
        slot_id = next((i for i, s in node.slots.items() if s.is_input and s.name == name), None)
        return self.input(graph, node_id, slot_id, scope, depth) if slot_id is not None else None

    def output(self, graph: _Graph, node_id: str, slot_id: int, scope: dict, depth: int):
        """出力スロットの値（成分の並びか ``_UV``。分からなければ None）。

        同じ出力は 1 度だけ評価する（分岐の多いグラフで指数的に増えないように）。評価中の出力にまた来たら、
        つながりが循環しているので分からない扱いにする。
        """
        key = (id(graph), node_id, slot_id, id(scope))
        if key in self._memo:
            return self._memo[key]
        self._memo[key] = None
        value = self._output(graph, node_id, slot_id, scope, depth)
        self._memo[key] = value
        return value

    def _output(self, graph: _Graph, node_id: str, slot_id: int, scope: dict, depth: int):
        if depth > _MAX_DEPTH or node_id not in graph.nodes:
            return None
        node = graph.nodes[node_id]
        kind = node.kind
        slot = node.slots.get(slot_id)
        if kind == "PropertyNode":
            return self.property(graph, node.data.get("_property"), scope)
        if kind == "UVNode":
            channel = node.data.get("m_OutputChannel", 0)
            return _UV(channel if isinstance(channel, int) else -1, *_IDENTITY)
        if kind in ("RedirectNodeData",):
            inputs = [i for i, s in node.slots.items() if s.is_input]
            return self.input(graph, node_id, inputs[0], scope, depth) if inputs else None
        if kind in _PARALLAX:
            # 視線に合わせて UV を少しずらすだけなので、入力の UV をそのまま渡すものとみなす（近似）
            return _as_uv(self.named_input(graph, node_id, "UVs", scope, depth))
        if kind == "TilingAndOffsetNode":
            uv = _as_uv(self.named_input(graph, node_id, "UV", scope, depth))
            tiling = _components(self.named_input(graph, node_id, "Tiling", scope, depth), 2)
            offset = _components(self.named_input(graph, node_id, "Offset", scope, depth), 2)
            if uv is None or tiling is None or offset is None or (uv.scale, uv.offset) != _IDENTITY:
                return None
            return _UV(uv.channel, tiling, offset)
        if kind == "SplitNode":
            value = _components(self.named_input(graph, node_id, "In", scope, depth), None)
            index = "RGBA".find(slot.name) if slot is not None and len(slot.name) == 1 else -1
            return [value[index]] if value is not None and 0 <= index < len(value) else None
        if kind in ("CombineNode", "Vector2Node", "Vector3Node", "Vector4Node"):
            names = ("R", "G", "B", "A") if kind == "CombineNode" else ("X", "Y", "Z", "W")
            count = {"Vector2Node": 2, "Vector3Node": 3}.get(kind, 4)
            if kind == "CombineNode" and slot is not None:
                count = {"RG": 2, "RGB": 3}.get(slot.name, 4)
            values = [_components(self.named_input(graph, node_id, n, scope, depth), 1) for n in names[:count]]
            return None if any(v is None for v in values) else [v[0] for v in values]
        if kind == "SubGraphNode":
            return self.subgraph_output(graph, node, node_id, slot, scope, depth)
        return None

    def property(self, graph: _Graph, guid: object, scope: dict):
        if not isinstance(guid, str):
            return None
        if guid in scope:  # サブグラフの入力
            return scope[guid]()
        reference, kind = graph.properties.get(guid, ("", ""))
        size = {"Vector1ShaderProperty": 1, "Vector2ShaderProperty": 2, "Vector3ShaderProperty": 3,
                "Vector4ShaderProperty": 4}.get(kind)
        if not reference or size is None:
            return None
        return [(reference, i) for i in range(size)]

    def subgraph_output(self, graph: _Graph, node: _Node, node_id: str, slot: _Slot | None, scope: dict, depth: int):
        if slot is None:
            return None
        reference = node.data.get("m_SerializedSubGraph")
        try:
            asset = json.loads(reference) if isinstance(reference, str) else reference
            guid = asset["subGraph"]["guid"]
        except (json.JSONDecodeError, TypeError, KeyError):
            return None
        inner = self.subgraph(guid) if isinstance(guid, str) else None
        if inner is None:
            return None
        # サブグラフのプロパティ（GUID）→ このノードの入力スロットの値
        bound: dict[str, Callable[[], object]] = {}
        for prop_guid, slot_id in zip(node.data.get("m_PropertyGuids") or (), node.data.get("m_PropertyIds") or ()):
            bound[prop_guid] = (lambda s=slot_id: self.input(graph, node_id, s, scope, depth))
        out_node = next((i for i, n in inner.nodes.items() if n.kind == "SubGraphOutputNode"), None)
        if out_node is None:
            return None
        inner_slot = next((i for i, s in inner.nodes[out_node].slots.items() if s.is_input and s.name == slot.name), None)
        if inner_slot is None:
            return None
        self._scopes.append(bound)
        return self.input(inner, out_node, inner_slot, bound, depth + 1)


def _slot_default(slot: _Slot):
    data = slot.data
    if _short(data.get("m_Type")) == "UVMaterialSlot" or "m_Channel" in data:
        channel = data.get("m_Channel", 0)
        return _UV(channel if isinstance(channel, int) else -1, *_IDENTITY)
    value = data.get("m_Value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    if isinstance(value, dict):
        comps = [value.get(k) for k in ("x", "y", "z", "w") if k in value]
        if comps and all(isinstance(c, (int, float)) for c in comps):
            return [float(c) for c in comps]
    return None


def _as_uv(value) -> _UV | None:
    return value if isinstance(value, _UV) else None


def _components(value, size: int | None):
    """成分の並び（``size`` 個に切る。足りなければ None。1 成分は Shader Graph と同じく全成分に広げる）。"""
    if not isinstance(value, list) or not value:
        return None
    if size is None:
        return value
    if len(value) == 1 and size > 1:
        return tuple(value * size)
    return tuple(value[:size]) if len(value) >= size else None


def texture_uv_transforms(text: str, read_subgraph: Callable[[str], str | None]) -> dict[str, UVTransform]:
    """テクスチャのプロパティ（参照名）→ UV の変換。決められたもので、恒等でないものだけを返す。

    ``read_subgraph`` はサブグラフの GUID からその中身を返す（無ければ None）。壊れた入力には ``GraphError`` だけを送出する。
    """
    graph = parse_graph(text)
    evaluator = _Evaluator(read_subgraph)
    found: dict[str, UVTransform | None] = {}
    for node_id, node in graph.nodes.items():
        if node.kind not in _SAMPLERS:
            continue
        texture_slot = next((i for i, s in node.slots.items() if s.is_input and s.name == "Texture"), None)
        source = graph.sources.get((node_id, texture_slot)) if texture_slot is not None else None
        prop_node = graph.nodes.get(source[0]) if source else None
        if prop_node is None or prop_node.kind != "PropertyNode":
            continue
        reference, kind = graph.properties.get(prop_node.data.get("_property"), ("", ""))
        if not reference or "Texture" not in kind:
            continue
        uv = _as_uv(evaluator.named_input(graph, node_id, "UV", {}, 0))
        transform = UVTransform(uv.scale, uv.offset) if uv is not None and uv.channel == 0 else None
        if reference in found and found[reference] != transform:
            transform = None  # 同じテクスチャを違う UV で読む
        found[reference] = transform
    return {k: v for k, v in found.items() if v is not None and (v.scale, v.offset) != _IDENTITY}


def _value(component, mat: UnityMaterial) -> float | None:
    if isinstance(component, float):
        return component
    name, index = component
    if name in mat.colors:
        vector = mat.colors[name]
        return float(vector[index]) if index < len(vector) else None
    if index == 0 and name in mat.floats:
        return float(mat.floats[name])
    return None


def apply_uv_transforms(mat: UnityMaterial, transforms: dict[str, UVTransform]) -> UnityMaterial:
    """グラフの UV の変換を、マテリアルのテクスチャの ``m_Scale`` / ``m_Offset`` に置き換えた写し。

    成分のプロパティがマテリアルに無いものは置き換えない（Unity はグラフの既定値を使うが、その値はここでは読まない）。
    """
    textures = dict(mat.textures)
    changed = False
    for name, transform in transforms.items():
        ref = textures.get(name)
        if ref is None:
            continue
        values = [_value(c, mat) for c in (*transform.scale, *transform.offset)]
        if any(v is None for v in values):
            continue
        textures[name] = TexRef(ref.guid, ref.file_id, (values[0], values[1]), (values[2], values[3]))
        changed = True
    return replace(mat, textures=textures) if changed else mat
