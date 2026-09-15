"""テスト用に、TypeTree 付きのバイナリ SerializedFile（Asset Serialization が Force Binary の形式）を組み立てる。

``core/unity_binary.py`` の読み取りを確かめるための手書きの生成器で、実在アセットのデータは使わない。
bpy に依存しないので、単体テストと ``tests/make_synthetic_package.py`` の両方から使う。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

ALIGN = 0x4000
BUILTIN_EXTRA_PATH = "Resources/unity_builtin_extra"
CLASS_GAME_OBJECT, CLASS_MESH_RENDERER, CLASS_MATERIAL, CLASS_MESH_FILTER = 1, 23, 21, 33
CLASS_MONO_BEHAVIOUR, CLASS_PREFAB = 114, 1001

# 公開されている共通文字列表のオフセット。読み取り側の表と独立に確かめるため、ここでは値を直接書く
COMMON_OFFSETS = {"Array": 49, "Base": 55, "data": 106, "float": 161, "int": 222, "m_Name": 427, "size": 795, "string": 840}

_PRIMITIVES = {
    "SInt8": "b", "UInt8": "B", "char": "B", "SInt16": "h", "UInt16": "H", "int": "i", "SInt32": "i",
    "unsigned int": "I", "UInt32": "I", "SInt64": "q", "UInt64": "Q", "float": "f", "double": "d", "bool": "?",
}


@dataclass
class Node:
    type: str
    name: str
    children: list["Node"] = field(default_factory=list)
    flags: int = 0
    is_array: bool = False


def prim(type_: str, name: str, align: bool = False) -> Node:
    return Node(type_, name, flags=ALIGN if align else 0)


def cls(type_: str, name: str, *children: Node) -> Node:
    return Node(type_, name, list(children))


def _array(element: Node, align: bool = False) -> Node:
    return Node("Array", "Array", [prim("int", "size"), element], flags=ALIGN if align else 0, is_array=True)


def string(name: str) -> Node:
    return Node("string", name, [_array(prim("char", "data"), align=True)])


def vector(name: str, element: Node, align: bool = False) -> Node:
    return Node("vector", name, [_array(element, align)])


def map_(name: str, key: Node, value: Node) -> Node:
    """値は ``[(key, value), ...]`` で渡す。"""
    return Node("map", name, [_array(cls("pair", "data", key, value))])


def pptr(target: str, name: str) -> Node:
    """値は ``(externals の番号（0 は同じファイル）, pathID)`` で渡す。"""
    return cls(f"PPtr<{target}>", name, prim("int", "m_FileID"), prim("SInt64", "m_PathID"))


def guid_bytes(guid: str | None) -> bytes:
    """32 桁の GUID を、各バイトの上下 4 ビットを入れ替えたバイナリの並びにする。"""
    if not guid:
        return bytes(16)
    return bytes(int(guid[i + 1] + guid[i], 16) for i in range(0, 32, 2))


def _encode(node: Node, value: Any, out: bytearray, e: str) -> None:
    align = bool(node.flags & ALIGN)
    fmt = _PRIMITIVES.get(node.type)
    if fmt is not None and not node.children:
        out += struct.pack(e + fmt, value)
    elif node.type == "string":
        raw = value.encode("utf-8")
        out += struct.pack(e + "i", len(raw)) + raw
        align = True
    elif node.type == "pair":
        _encode(node.children[0], value[0], out, e)
        _encode(node.children[1], value[1], out, e)
    elif node.children and node.children[0].type == "Array":
        array = node.children[0]
        out += struct.pack(e + "i", len(value))
        for item in value:
            _encode(array.children[1], item, out, e)
        align = align or bool(array.flags & ALIGN)
    elif node.type.startswith("PPtr<"):
        out += struct.pack(e + "iq", *value)
    else:
        for child in node.children:
            _encode(child, value[child.name], out, e)
    if align:
        out += bytes(-len(out) % 4)


def _type_tree(root: Node, e: str, version: int) -> bytes:
    flat: list[tuple[Node, int]] = []

    def walk(node: Node, level: int) -> None:
        flat.append((node, level))
        for child in node.children:
            walk(child, level + 1)

    walk(root, 0)
    local = bytearray()
    offsets: dict[str, int] = {}

    def offset(text: str) -> int:
        if text in COMMON_OFFSETS:
            return 0x80000000 | COMMON_OFFSETS[text]
        if text not in offsets:
            offsets[text] = len(local)
            local.extend(text.encode("utf-8") + b"\0")
        return offsets[text]

    node_fmt = e + "HBBIIiiI" + ("Q" if version >= 19 else "")
    nodes = bytearray()
    for index, (node, level) in enumerate(flat):
        fields = [1, level, 1 if node.is_array else 0, offset(node.type), offset(node.name), -1, index, node.flags]
        if version >= 19:
            fields.append(0)
        nodes += struct.pack(node_fmt, *fields)
    return struct.pack(e + "ii", len(flat), len(local)) + bytes(nodes) + bytes(local)


def build_serialized_file(
    objects: list[tuple[int, int, Node, Any]],
    externals: list[tuple[str | None, int, str]] = (),
    *,
    version: int = 17,
    big_endian: bool = False,
    type_tree: bool = True,
    unity_version: str = "5.6.0f1",
) -> bytes:
    """``objects`` は ``(pathID, classID, TypeTree のルート, 値)``、``externals`` は ``(GUID, 種別, パス)``。

    対応するのは version 17 以降の並び（型の表を番号で引く形式）。
    """
    e = ">" if big_endian else "<"
    types: dict[int, Node] = {}
    for _, class_id, root, _ in objects:
        types.setdefault(class_id, root)
    type_index = {class_id: i for i, class_id in enumerate(types)}

    data = bytearray()
    placed: list[tuple[int, int, int, int]] = []
    for path_id, class_id, root, value in objects:
        data += bytes(-len(data) % 8)
        body = bytearray()
        _encode(root, value, body, e)
        placed.append((path_id, len(data), len(body), type_index[class_id]))
        data += body

    header_size = 48 if version >= 22 else 20
    meta = bytearray(unity_version.encode() + b"\0")
    meta += struct.pack(e + "i", -2)
    meta.append(1 if type_tree else 0)
    meta += struct.pack(e + "i", len(types))
    for class_id, root in types.items():
        meta += struct.pack(e + "i", class_id)
        meta.append(0)  # stripped
        meta += struct.pack(e + "h", -1)
        if class_id == CLASS_MONO_BEHAVIOUR:
            meta += bytes(16)
        meta += bytes(16)
        if type_tree:
            meta += _type_tree(root, e, version)
            if version >= 21:
                meta += struct.pack(e + "i", 0)
    meta += struct.pack(e + "i", len(placed))
    for path_id, start, size, index in placed:
        meta += bytes(-(header_size + len(meta)) % 4)
        meta += struct.pack(e + "q", path_id)
        meta += struct.pack(e + ("q" if version >= 22 else "I"), start)
        meta += struct.pack(e + "Ii", size, index)
    meta += struct.pack(e + "i", 0)  # script types
    meta += struct.pack(e + "i", len(externals))
    for guid, ext_type, path in externals:
        meta += b"\0" + guid_bytes(guid) + struct.pack(e + "i", ext_type) + path.encode() + b"\0"
    if version >= 20:
        meta += struct.pack(e + "i", 0)  # ref types
    meta += b"\0"  # user information

    data_offset = header_size + len(meta)
    data_offset += -data_offset % 16
    file_size = data_offset + len(data)
    flag = bytes([1 if big_endian else 0, 0, 0, 0])
    if version >= 22:
        header = struct.pack(">IIII", 0, 0, version, 0) + flag + struct.pack(">IqqQ", len(meta), file_size, data_offset, 0)
    else:
        header = struct.pack(">IIII", len(meta), file_size, version, data_offset) + flag
    blob = header + bytes(meta)
    return blob + bytes(data_offset - len(blob)) + bytes(data)


# ---------------------------------------------------------------------------
# よく使う形
# ---------------------------------------------------------------------------


class _Externals:
    def __init__(self) -> None:
        self.items: list[tuple[str | None, int, str]] = []

    def index(self, guid: str | None, ext_type: int, path: str = "") -> int:
        key = (guid, ext_type, path)
        if key not in self.items:
            self.items.append(key)
        return self.items.index(key) + 1


def material(
    name: str,
    *,
    shader: tuple[str | None, int, int] = (None, 46, 0),
    keywords: list[str] = (),
    invalid_keywords: list[str] = (),
    render_queue: int = -1,
    textures: list[tuple[str, str | None, tuple[float, float], tuple[float, float]]] = (),
    floats: list[tuple[str, float]] = (),
    ints: list[tuple[str, int]] = (),
    colors: list[tuple[str, tuple[float, float, float, float]]] = (),
    legacy: bool = True,
    version: int = 17,
    big_endian: bool = False,
) -> bytes:
    """Material 1 つのファイル。

    ``legacy`` は Unity 5.x の並び（キーが FastPropertyName、``m_ShaderKeywords`` が空白区切りの文字列、
    ``m_Ints`` 無し）。False なら新しい並び（キーが文字列、``m_ValidKeywords`` / ``m_InvalidKeywords``、``m_Ints``）。
    ``shader`` は ``(GUID, fileID, 種別)``。GUID が None なら組み込みシェーダー。
    """
    ext = _Externals()
    key = cls("FastPropertyName", "first", string("name")) if legacy else string("first")

    def k(prop: str) -> Any:
        return {"name": prop} if legacy else prop

    vec2 = lambda n: cls("Vector2f", n, prim("float", "x"), prim("float", "y"))  # noqa: E731
    texenv = cls("UnityTexEnv", "second", pptr("Texture", "m_Texture"), vec2("m_Scale"), vec2("m_Offset"))
    color = cls("ColorRGBA", "second", *(prim("float", c) for c in "rgba"))
    sheet = [prim("int", "serializedVersion"), map_("m_TexEnvs", key, texenv)]
    if not legacy:
        sheet.append(map_("m_Ints", key, prim("int", "second")))
    sheet += [map_("m_Floats", key, prim("float", "second")), map_("m_Colors", key, color)]
    root = cls(
        "Material", "Base",
        prim("unsigned int", "m_ObjectHideFlags"),
        string("m_Name"),
        pptr("Shader", "m_Shader"),
        *((string("m_ShaderKeywords"),) if legacy else (vector("m_ValidKeywords", string("data")),
                                                        vector("m_InvalidKeywords", string("data")))),
        prim("int", "m_CustomRenderQueue"),
        cls("UnityPropertySheet", "m_SavedProperties", *sheet),
    )

    shader_guid, shader_file_id, shader_type = shader
    shader_ref = (ext.index(None, 0, BUILTIN_EXTRA_PATH) if shader_guid is None else ext.index(shader_guid, shader_type),
                  shader_file_id)
    props: dict[str, Any] = {
        "serializedVersion": 3,
        "m_TexEnvs": [
            (k(prop), {
                "m_Texture": (ext.index(guid, 3), 2800000) if guid else (0, 0),
                "m_Scale": {"x": scale[0], "y": scale[1]},
                "m_Offset": {"x": offset[0], "y": offset[1]},
            })
            for prop, guid, scale, offset in textures
        ],
        "m_Floats": [(k(prop), value) for prop, value in floats],
        "m_Colors": [(k(prop), dict(zip("rgba", rgba))) for prop, rgba in colors],
    }
    if not legacy:
        props["m_Ints"] = [(k(prop), value) for prop, value in ints]
    value: dict[str, Any] = {
        "m_ObjectHideFlags": 0,
        "m_Name": name,
        "m_Shader": shader_ref,
        "m_CustomRenderQueue": render_queue,
        "m_SavedProperties": props,
    }
    if legacy:
        value["m_ShaderKeywords"] = " ".join(keywords)
    else:
        value["m_ValidKeywords"] = list(keywords)
        value["m_InvalidKeywords"] = list(invalid_keywords)
    return build_serialized_file([(2100000, CLASS_MATERIAL, root, value)], ext.items, version=version, big_endian=big_endian)


def prefab_with_renderers(
    renderers: list[tuple[str, str, list[str | None]]], *, version: int = 17, big_endian: bool = False
) -> bytes:
    """Unity 5.x の prefab（Prefab オブジェクト + GameObject / Transform / MeshFilter / MeshRenderer）。

    ``renderers`` は ``(GameObject 名, メッシュを持つモデルの GUID, m_Materials に並べる .mat の GUID)``。
    """
    ext = _Externals()
    go_tree = cls("GameObject", "Base", string("m_Name"))
    transform_tree = cls("Transform", "Base", pptr("GameObject", "m_GameObject"), pptr("Transform", "m_Father"))
    filter_tree = cls("MeshFilter", "Base", pptr("GameObject", "m_GameObject"), pptr("Mesh", "m_Mesh"))
    renderer_tree = cls("MeshRenderer", "Base", pptr("GameObject", "m_GameObject"),
                        vector("m_Materials", pptr("Material", "data")))
    prefab_tree = cls("Prefab", "Base", pptr("GameObject", "m_RootGameObject"))
    objects: list[tuple[int, int, Node, Any]] = [(100100000, CLASS_PREFAB, prefab_tree, {"m_RootGameObject": (0, 100)})]
    for i, (go_name, model_guid, mats) in enumerate(renderers):
        go = 100 + 10 * i
        objects.append((go, CLASS_GAME_OBJECT, go_tree, {"m_Name": go_name}))
        objects.append((go + 3, 4, transform_tree, {"m_GameObject": (0, go), "m_Father": (0, 0)}))  # 4 = Transform
        objects.append((go + 1, CLASS_MESH_FILTER, filter_tree,
                        {"m_GameObject": (0, go), "m_Mesh": (ext.index(model_guid, 3), 4300000)}))
        objects.append((go + 2, CLASS_MESH_RENDERER, renderer_tree, {
            "m_GameObject": (0, go),
            "m_Materials": [(ext.index(m, 2), 2100000) if m else (0, 0) for m in mats],
        }))
    return build_serialized_file(objects, ext.items, version=version, big_endian=big_endian)
