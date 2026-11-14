"""テスト用に Shader Graph（.shadergraph / .shadersubgraph）を組み立てる（Issue #193）。

ノードとつながりを形式によらない形で書き、``new_format`` / ``old_format`` で Unity の 2 つの保存形式の文字列にする。
スロットの番号と表示名は Unity の Shader Graph と同じにしてある（読み取り側は表示名で入力を探す）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ノードの型 → (スロット番号, 表示名, 入力か) の並び
SLOTS = {
    "PropertyNode": [(0, "Out", False)],
    "UVNode": [(0, "Out", False)],
    "SampleTexture2DNode": [(0, "RGBA", False), (1, "Texture", True), (2, "UV", True), (3, "Sampler", True)],
    "TilingAndOffsetNode": [(0, "UV", True), (1, "Tiling", True), (2, "Offset", True), (3, "Out", False)],
    "SplitNode": [(0, "In", True), (1, "R", False), (2, "G", False), (3, "B", False), (4, "A", False)],
    "CombineNode": [(0, "R", True), (1, "G", True), (2, "B", True), (3, "A", True), (4, "RGBA", False), (5, "RGB", False), (6, "RG", False)],
    "ParallaxOcclusionMappingNode": [(0, "Heightmap", True), (4, "UVs", True), (9, "ParallaxUVs", False)],
    "MultiplyNode": [(0, "A", True), (1, "B", True), (2, "Out", False)],
    "SubGraphOutputNode": [(1, "Out_Vector2", True)],
}


@dataclass
class Graph:
    properties: list[tuple[str, str, str]] = field(default_factory=list)  # (GUID, 参照名, 型: Texture2D / Vector1 / Vector2 / Vector4)
    nodes: list[dict] = field(default_factory=list)  # {"id", "kind", "property"?, "channel"?, "values"?, "subgraph"?, "inputs"?}
    edges: list[tuple[str, int, str, int]] = field(default_factory=list)  # (出力ノード, スロット, 入力ノード, スロット)

    def prop(self, reference: str, kind: str) -> str:
        guid = f"00000000-0000-0000-0000-{len(self.properties):012d}"
        self.properties.append((guid, reference, kind))
        node_id = f"prop_{reference}_{len(self.nodes)}"
        self.nodes.append({"id": node_id, "kind": "PropertyNode", "property": guid})
        return node_id

    def node(self, kind: str, **extra) -> str:
        node_id = f"{kind}_{len(self.nodes)}"
        self.nodes.append({"id": node_id, "kind": kind, **extra})
        return node_id

    def link(self, out_node: str, out_slot: int, in_node: str, in_slot: int) -> None:
        self.edges.append((out_node, out_slot, in_node, in_slot))

    def sample(self, texture: str, uv_node: str | None, uv_slot: int = 0, channel: int = 0) -> str:
        """``texture`` を読む Sample Texture 2D。``uv_node`` が None なら UV 入力のチャンネル ``channel``。"""
        tex = self.prop(texture, "Texture2D")
        sampler = self.node("SampleTexture2DNode", channel=channel)
        self.link(tex, 0, sampler, 1)
        if uv_node is not None:
            self.link(uv_node, uv_slot, sampler, 2)
        return sampler

    def subgraph_node(self, subgraph_guid: str, bindings: list[tuple[str, int]], outputs: list[tuple[int, str]]) -> str:
        """``bindings`` は (サブグラフのプロパティの GUID, このノードの入力スロット番号)。"""
        return self.node("SubGraphNode", subgraph=subgraph_guid, bindings=bindings, outputs=outputs)


def _slots(node: dict) -> list[tuple[int, str, bool]]:
    if node["kind"] == "SubGraphNode":
        names = {i: f"In{i}" for _, i in node["bindings"]}
        return [(i, names[i], True) for i in names] + [(i, name, False) for i, name in node["outputs"]]
    return SLOTS[node["kind"]]


def _slot_data(node: dict, slot_id: int, name: str, is_input: bool) -> dict:
    data = {"m_Id": slot_id, "m_DisplayName": name, "m_SlotType": 0 if is_input else 1, "m_ShaderOutputName": name}
    if node["kind"] == "SampleTexture2DNode" and name == "UV":
        data["m_Type"] = "UnityEditor.ShaderGraph.UVMaterialSlot"
        data["m_Channel"] = node.get("channel", 0)
        data["m_Value"] = {"x": 0.0, "y": 0.0}
    elif node["kind"] == "TilingAndOffsetNode" and name == "Tiling":
        data["m_Value"] = {"x": 1.0, "y": 1.0}
    elif node["kind"] == "TilingAndOffsetNode" and name == "Offset":
        data["m_Value"] = {"x": 0.0, "y": 0.0}
    elif node["kind"] == "TilingAndOffsetNode" and name == "UV":
        data["m_Type"] = "UnityEditor.ShaderGraph.UVMaterialSlot"
        data["m_Channel"] = 0
        data["m_Value"] = {"x": 0.0, "y": 0.0}
    elif node["kind"] == "CombineNode" and is_input:
        data["m_Value"] = node.get("values", {}).get(name, 0.0)
    return data


def _node_extra(node: dict) -> dict:
    extra: dict = {}
    if node["kind"] == "UVNode":
        extra["m_OutputChannel"] = node.get("channel", 0)
    if node["kind"] == "SubGraphNode":
        extra["m_SerializedSubGraph"] = json.dumps({"subGraph": {"fileID": -5475051401550479605, "guid": node["subgraph"], "type": 3}})
        extra["m_PropertyGuids"] = [g for g, _ in node["bindings"]]
        extra["m_PropertyIds"] = [i for _, i in node["bindings"]]
    return extra


def new_format(graph: Graph) -> str:
    """新しい形式（JSON のオブジェクトを並べ、``m_ObjectId`` で参照し合う）。"""
    objects: list[dict] = []
    root = {"m_SGVersion": 3, "m_Type": "UnityEditor.ShaderGraph.GraphData", "m_ObjectId": "graph",
            "m_Properties": [{"m_Id": f"prop{i}"} for i in range(len(graph.properties))],
            "m_Nodes": [{"m_Id": n["id"]} for n in graph.nodes], "m_Edges": []}
    objects.append(root)
    object_ids = {}
    for i, (guid, reference, kind) in enumerate(graph.properties):
        object_ids[guid] = f"prop{i}"
        objects.append({"m_SGVersion": 0, "m_Type": f"UnityEditor.ShaderGraph.Internal.{kind}ShaderProperty",
                        "m_ObjectId": f"prop{i}", "m_Guid": {"m_GuidSerialized": guid}, "m_Name": reference.strip("_"),
                        "m_DefaultReferenceName": f"{kind}_{i}", "m_OverrideReferenceName": reference})
    for node in graph.nodes:
        slot_ids = []
        for slot_id, name, is_input in _slots(node):
            object_id = f"{node['id']}_slot{slot_id}"
            slot_ids.append({"m_Id": object_id})
            objects.append({"m_Type": "UnityEditor.ShaderGraph.Vector1MaterialSlot", "m_ObjectId": object_id,
                            **_slot_data(node, slot_id, name, is_input)})
        data = {"m_SGVersion": 0, "m_Type": f"UnityEditor.ShaderGraph.{node['kind']}", "m_ObjectId": node["id"],
                "m_Slots": slot_ids, **_node_extra(node)}
        if "property" in node:
            data["m_Property"] = {"m_Id": object_ids[node["property"]]}
        objects.append(data)
    for out_node, out_slot, in_node, in_slot in graph.edges:
        root["m_Edges"].append({"m_OutputSlot": {"m_Node": {"m_Id": out_node}, "m_SlotId": out_slot},
                                "m_InputSlot": {"m_Node": {"m_Id": in_node}, "m_SlotId": in_slot}})
    return "\n\n".join(json.dumps(o, indent=4) for o in objects)


def old_format(graph: Graph) -> str:
    """古い形式（1 つの JSON。ノード・プロパティ・つながりは ``JSONnodeData`` の JSON 文字列）。"""
    def wrap(type_name: str, data: dict) -> dict:
        return {"typeInfo": {"fullName": type_name}, "JSONnodeData": json.dumps(data)}

    properties = [
        wrap(f"UnityEditor.ShaderGraph.{'Texture' if kind == 'Texture2D' else kind}ShaderProperty",
             {"m_Name": reference.strip("_"), "m_Guid": {"m_GuidSerialized": guid},
              "m_DefaultReferenceName": f"{kind}_{i}", "m_OverrideReferenceName": reference})
        for i, (guid, reference, kind) in enumerate(graph.properties)
    ]
    nodes = []
    for node in graph.nodes:
        slots = [wrap("UnityEditor.ShaderGraph.Vector1MaterialSlot", _slot_data(node, i, name, is_input))
                 for i, name, is_input in _slots(node)]
        data = {"m_GuidSerialized": node["id"], "m_Name": node["kind"], "m_SerializableSlots": slots, **_node_extra(node)}
        if "property" in node:
            data["m_PropertyGuidSerialized"] = node["property"]
        nodes.append(wrap(f"UnityEditor.ShaderGraph.{node['kind']}", data))
    edges = [
        wrap("UnityEditor.Graphing.Edge", {"m_OutputSlot": {"m_SlotId": out_slot, "m_NodeGUIDSerialized": out_node},
                                           "m_InputSlot": {"m_SlotId": in_slot, "m_NodeGUIDSerialized": in_node}})
        for out_node, out_slot, in_node, in_slot in graph.edges
    ]
    return json.dumps({"m_SerializedProperties": properties, "m_SerializableNodes": nodes, "m_SerializableEdges": edges}, indent=4)


def base_uv_graph(texture: str = "_MainTex", vector: str = "_BaseUV") -> Graph:
    """UnityJapanOffice と同じ作り: Vector4 を Split して、xy を Tiling、zw を Offset にした UV で ``texture`` を読む。"""
    graph = Graph()
    uv = graph.prop(vector, "Vector4")
    split = graph.node("SplitNode")
    graph.link(uv, 0, split, 0)
    tiling, offset = graph.node("CombineNode"), graph.node("CombineNode")
    for out_slot, combine, in_slot in ((1, tiling, 0), (2, tiling, 1), (3, offset, 0), (4, offset, 1)):
        graph.link(split, out_slot, combine, in_slot)
    to = graph.node("TilingAndOffsetNode")
    graph.link(tiling, 6, to, 1)
    graph.link(offset, 6, to, 2)
    graph.sample(texture, to, 3)
    return graph
