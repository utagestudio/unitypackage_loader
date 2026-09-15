"""バイナリ形式の Unity SerializedFile（.mat / .prefab など）を読むリーダー。

プロジェクト設定の Asset Serialization が Mixed / Force Binary だと、``.mat`` や ``.prefab`` は
YAML テキストではなくバイナリで保存され、そのままパッケージに入る。エディタが書き出すファイルには
TypeTree（フィールド名と型の一覧）が付いているので、それをたどって値を読み、
``unity_yaml.parse_documents`` と同じ ``UnityDocument`` の列に変換する。

形式の読み方は公開されているオープンソース実装（UnityPy、AssetStudio）の記述に従った。
対応するのは SerializedFile version 14〜22（Unity 5.0 〜 Unity 6 のエディタ出力）で、TypeTree が付いたものだけ。

変換の約束（YAML パーサーの出力に揃える）:

* クラス → ``dict``（子の名前をキーにする）。ルートは ``{型名: 本体}`` として ``UnityDocument.data`` に入る
* ``PPtr<...>`` → ``UnityRef``。外部ファイルを指す場合は externals の GUID と種別を入れる
* ``vector`` / ``staticvector`` / ``set`` などの配列 → ``list``
* ``map`` → 1 要素 dict の ``list``（キーが文字列でなければ ``{"first": .., "second": ..}``）。
  Unity YAML の ``m_TexEnvs`` / ``m_Floats`` などと同じ形になる
* ``FastPropertyName``（古い .mat のキー）→ 中の文字列
* ``bool`` → ``0`` / ``1``、``TypelessData`` → 16 進文字列

壊れた / 細工されたファイルでも ``UnityBinaryError``（``ValueError`` の派生）だけを送出する。
範囲外の読み取り、ファイルサイズを超える件数、不正な TypeTree の階層はいずれもここで止める。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .unity_yaml import UnityDocument, UnityRef, parse_documents

__all__ = [
    "UnityBinaryError",
    "MIN_VERSION",
    "MAX_VERSION",
    "is_serialized_file",
    "parse_serialized_file",
    "load_documents",
]

MIN_VERSION = 14  # Unity 5.0
MAX_VERSION = 22  # Unity 2020.1 〜 Unity 6

CLASS_MONO_BEHAVIOUR = 114
# 最小 0 バイトの要素（中身の無い構造体）の配列で受け付ける件数の上限。件数を残りのバイト数で抑えられないため
MAX_EMPTY_ELEMENTS = 4096
_ALIGN_FLAG = 0x4000
_HEADER_SIZE = 20
# 組み込みリソースを指す externals のパスと、YAML 側で使われる GUID の対応
_BUILTIN_GUIDS = {
    "resources/unity_builtin_extra": "0000000000000000f000000000000000",
    "library/unity default resources": "0000000000000000e000000000000000",
}


class UnityBinaryError(ValueError):
    """SerializedFile として読めないとき（未対応の版、TypeTree 無し、壊れたデータ）に送出される。"""


# Unity が全ファイル共通で使う TypeTree の文字列表。オフセットの最上位ビットが立っていればこちらを引く。
# 値は AssetStudio（CommonString.cs）と UnityPy が公開している表と同じ
_COMMON_STRINGS = (
    "AABB", "AnimationClip", "AnimationCurve", "AnimationState", "Array", "Base", "BitField", "bitset",
    "bool", "char", "ColorRGBA", "Component", "data", "deque", "double", "dynamic_array", "FastPropertyName",
    "first", "float", "Font", "GameObject", "Generic Mono", "GradientNEW", "GUID", "GUIStyle", "int", "list",
    "long long", "map", "Matrix4x4f", "MdFour", "MonoBehaviour", "MonoScript", "m_ByteSize", "m_Curve",
    "m_EditorClassIdentifier", "m_EditorHideFlags", "m_Enabled", "m_ExtensionPtr", "m_GameObject", "m_Index",
    "m_IsArray", "m_IsStatic", "m_MetaFlag", "m_Name", "m_ObjectHideFlags", "m_PrefabInternal",
    "m_PrefabParentObject", "m_Script", "m_StaticEditorFlags", "m_Type", "m_Version", "Object", "pair",
    "PPtr<Component>", "PPtr<GameObject>", "PPtr<Material>", "PPtr<MonoBehaviour>", "PPtr<MonoScript>",
    "PPtr<Object>", "PPtr<Prefab>", "PPtr<Sprite>", "PPtr<TextAsset>", "PPtr<Texture>", "PPtr<Texture2D>",
    "PPtr<Transform>", "Prefab", "Quaternionf", "Rectf", "RectInt", "RectOffset", "second", "set", "short",
    "size", "SInt16", "SInt32", "SInt64", "SInt8", "staticvector", "string", "TextAsset", "TextMesh", "Texture",
    "Texture2D", "Transform", "TypelessData", "UInt16", "UInt32", "UInt64", "UInt8", "unsigned int",
    "unsigned long long", "unsigned short", "vector", "Vector2f", "Vector3f", "Vector4f",
    "m_ScriptingClassIdentifier", "Gradient", "Type*", "int2_storage", "int3_storage", "BoundsInt",
    "m_CorrespondingSourceObject", "m_PrefabInstance", "m_PrefabAsset", "FileSize", "Hash128",
)


def _common_string_offsets() -> dict[int, str]:
    table: dict[int, str] = {}
    offset = 0
    for text in _COMMON_STRINGS:
        table[offset] = text
        offset += len(text) + 1  # NUL 区切り
    return table


COMMON_STRINGS = _common_string_offsets()

# struct の書式（エンディアン記号は後で付ける）
_PRIMITIVES = {
    "SInt8": "b", "UInt8": "B", "char": "B",
    "short": "h", "SInt16": "h", "unsigned short": "H", "UInt16": "H",
    "int": "i", "SInt32": "i", "unsigned int": "I", "UInt32": "I", "Type*": "I",
    "long long": "q", "SInt64": "q", "unsigned long long": "Q", "UInt64": "Q", "FileSize": "Q",
    "float": "f", "double": "d", "bool": "?",
}


# ---------------------------------------------------------------------------
# 範囲チェック付きの読み取り
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes, endian: str, pos: int = 0, end: int | None = None):
        self.data = data
        self.endian = endian
        self.pos = pos
        self.base = pos  # 境界揃えの基準（オブジェクトの先頭）
        self.end = len(data) if end is None else end

    @property
    def remaining(self) -> int:
        return self.end - self.pos

    def bytes(self, size: int) -> bytes:
        if size < 0 or size > self.remaining:
            raise UnityBinaryError(f"unexpected end of data (need {size} bytes at offset {self.pos})")
        start = self.pos
        self.pos += size
        return self.data[start : self.pos]

    def unpack(self, fmt: str) -> tuple:
        st = struct.Struct(self.endian + fmt)
        if st.size > self.remaining:
            raise UnityBinaryError(f"unexpected end of data (need {st.size} bytes at offset {self.pos})")
        values = st.unpack_from(self.data, self.pos)
        self.pos += st.size
        return values

    def one(self, fmt: str) -> Any:
        return self.unpack(fmt)[0]

    def count(self, what: str, min_item_size: int = 1) -> int:
        """件数を読み、残りのバイト数で収まらない値は壊れたデータとして扱う（``min_item_size`` は 1 要素の最小バイト数）。"""
        n = self.one("i")
        if n < 0 or n * max(0, min_item_size) > self.remaining:
            raise UnityBinaryError(f"invalid {what} count {n} at offset {self.pos - 4}")
        return n

    def cstring(self) -> str:
        nul = self.data.find(b"\0", self.pos, self.end)
        if nul < 0:
            raise UnityBinaryError(f"unterminated string at offset {self.pos}")
        text = self.data[self.pos : nul].decode("utf-8", "replace")
        self.pos = nul + 1
        return text

    def align(self, size: int = 4) -> None:
        self.pos = min(self.end, self.base + (self.pos - self.base + size - 1) // size * size)


# ---------------------------------------------------------------------------
# TypeTree
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    type: str
    name: str
    level: int
    flags: int
    children: list["_Node"] = field(default_factory=list)

    @property
    def aligned(self) -> bool:
        return bool(self.flags & _ALIGN_FLAG)

    @property
    def is_array_wrapper(self) -> bool:
        return bool(self.children) and self.children[0].type == "Array"


def _read_type_tree(r: _Reader, version: int) -> _Node:
    node_fmt = "HBBIIiiI" + ("Q" if version >= 19 else "")
    node_size = struct.calcsize("<" + node_fmt)
    node_count = r.count("type tree node", node_size)
    if node_count == 0:
        raise UnityBinaryError("empty type tree")
    string_size = r.count("type tree string buffer")
    raw_nodes = [r.unpack(node_fmt) for _ in range(node_count)]
    strings = r.bytes(string_size)

    def text(offset: int) -> str:
        if offset & 0x80000000:
            return COMMON_STRINGS.get(offset & 0x7FFFFFFF, f"<common {offset & 0x7FFFFFFF}>")
        nul = strings.find(b"\0", offset)
        if offset >= len(strings) or nul < 0:
            raise UnityBinaryError(f"type tree string offset {offset} out of range")
        return strings[offset:nul].decode("utf-8", "replace")

    root: _Node | None = None
    stack: list[_Node] = []
    for raw in raw_nodes:
        level, type_off, name_off, flags = raw[1], raw[3], raw[4], raw[7]
        node = _Node(text(type_off), text(name_off), level, flags)
        if root is None:
            if level != 0:
                raise UnityBinaryError("type tree root must be at level 0")
            root = node
        else:
            if level < 1 or level > len(stack):
                raise UnityBinaryError(f"invalid type tree level {level} for {node.name!r}")
            del stack[level:]
            stack[-1].children.append(node)
        stack.append(node)
    assert root is not None
    return root


@dataclass
class _SerializedType:
    class_id: int
    stripped: bool
    tree: _Node | None


def _read_type(r: _Reader, version: int, has_tree: bool) -> _SerializedType:
    class_id = r.one("i")
    stripped = bool(r.one("B")) if version >= 16 else False
    if version >= 17:
        r.one("h")  # script_type_index
    if (version < 16 and class_id < 0) or (version >= 16 and class_id == CLASS_MONO_BEHAVIOUR):
        r.bytes(16)  # script id
    r.bytes(16)  # old type hash
    tree = None
    if has_tree:
        tree = _read_type_tree(r, version)
        if version >= 21:
            r.bytes(4 * r.count("type dependency", 4))
    return _SerializedType(class_id, stripped, tree)


# ---------------------------------------------------------------------------
# 値の読み取り
# ---------------------------------------------------------------------------


def _guid_text(raw: bytes) -> str:
    """バイナリの GUID（各バイトの上下 4 ビットが入れ替わった並び）を YAML と同じ 32 桁の文字列にする。"""
    return "".join(f"{b & 0xF:x}{b >> 4:x}" for b in raw)


@dataclass
class _External:
    guid: str | None
    type: int
    path: str


class _ValueReader:
    def __init__(self, r: _Reader, externals: list[_External]):
        self.r = r
        self.externals = externals
        self._min_sizes: dict[int, int] = {}  # id(node) → 値の最小バイト数

    def _min_size(self, node: _Node) -> int:
        """値が占める最小のバイト数（文字列・配列は件数の 4 バイト）。配列の件数が残りに収まるかの判定に使う。"""
        size = self._min_sizes.get(id(node))
        if size is None:
            fmt = _PRIMITIVES.get(node.type)
            if fmt is not None and not node.children:
                size = struct.calcsize(fmt)
            elif node.type in ("string", "TypelessData") or node.is_array_wrapper:
                size = 4
            else:
                size = sum(self._min_size(child) for child in node.children)
            self._min_sizes[id(node)] = size
        return size

    def read(self, node: _Node) -> Any:
        r = self.r
        align = node.aligned
        fmt = _PRIMITIVES.get(node.type)
        if fmt is not None and not node.children:
            value: Any = r.one(fmt)
            if fmt == "?":
                value = int(value)
        elif node.type == "string":
            value = r.bytes(r.count("string byte")).decode("utf-8", "replace")
            align = True
        elif node.type == "TypelessData":
            value = r.bytes(r.count("byte")).hex()
        elif node.type == "pair" and len(node.children) == 2:
            value = (self.read(node.children[0]), self.read(node.children[1]))
        elif node.is_array_wrapper:
            value = self._array(node)
            align = align or node.children[0].aligned
        elif node.type in ("ManagedReferencesRegistry", "ReferencedObject"):
            # [SerializeReference] のデータは参照型の表が要る。マテリアル割り当てには使わないので読まない
            raise UnityBinaryError(f"managed references are not supported ({node.name})")
        else:
            body = {child.name: self.read(child) for child in node.children}
            value = self._convert_class(node, body)
        if align:
            r.align()
        return value

    def _array(self, node: _Node) -> Any:
        array = node.children[0]
        if len(array.children) < 2:
            raise UnityBinaryError(f"malformed array node {node.name!r}")
        element = array.children[1]
        element_size = self._min_size(element)
        size = self.r.count(f"{node.name!r} element", element_size)
        if element_size == 0 and size > MAX_EMPTY_ELEMENTS:
            raise UnityBinaryError(f"invalid {node.name!r} element count {size} for empty elements")
        fmt = _PRIMITIVES.get(element.type)
        if fmt is not None and not element.children and not element.aligned:
            st = struct.Struct(f"{self.r.endian}{size}{fmt}")
            values = list(st.unpack(self.r.bytes(st.size)))
            return [int(v) for v in values] if fmt == "?" else values
        items = [self.read(element) for _ in range(size)]
        if element.type == "pair" and len(element.children) == 2:
            return [
                {k: v} if isinstance(k, str) else {"first": k, "second": v}
                for k, v in items
            ]
        return items

    def _convert_class(self, node: _Node, body: dict[str, Any]) -> Any:
        if node.type.startswith("PPtr<") and "m_FileID" in body and "m_PathID" in body:
            file_index, path_id = body["m_FileID"], body["m_PathID"]
            if not isinstance(file_index, int) or not isinstance(path_id, int):
                return UnityRef()
            if file_index == 0:
                return UnityRef(file_id=path_id)
            if 0 < file_index <= len(self.externals):
                ext = self.externals[file_index - 1]
                return UnityRef(file_id=path_id, guid=ext.guid, type=ext.type)
            return UnityRef(file_id=path_id)
        if node.type == "FastPropertyName" and isinstance(body.get("name"), str):
            return body["name"]
        return body


# ---------------------------------------------------------------------------
# ファイル全体
# ---------------------------------------------------------------------------


@dataclass
class _Header:
    version: int
    endian: str
    metadata_offset: int
    data_offset: int


def _read_header(data: bytes) -> _Header:
    if len(data) < _HEADER_SIZE:
        raise UnityBinaryError("file too small to be a serialized file")
    _metadata_size, file_size, version, data_offset = struct.unpack_from(">IIII", data, 0)
    if not MIN_VERSION <= version <= MAX_VERSION:
        raise UnityBinaryError(f"unsupported serialized file version {version} (supported: {MIN_VERSION}-{MAX_VERSION})")
    endian = ">" if data[16] else "<"
    offset = _HEADER_SIZE
    if version >= 22:
        if len(data) < 48:
            raise UnityBinaryError("file too small for a version 22 header")
        _metadata_size, file_size, data_offset, _unknown = struct.unpack_from(">IqqQ", data, 20)
        offset = 48
    if file_size != len(data):
        raise UnityBinaryError(f"header file size {file_size} does not match the data ({len(data)} bytes)")
    if not offset <= data_offset <= len(data):
        raise UnityBinaryError(f"data offset {data_offset} out of range")
    return _Header(version, endian, offset, data_offset)


def is_serialized_file(data: bytes) -> bool:
    """バイト列がバイナリの SerializedFile らしいか（ヘッダーのファイルサイズと版が一致するか）。"""
    if len(data) < _HEADER_SIZE or data[:5] == b"%YAML":
        return False
    version = struct.unpack_from(">I", data, 8)[0]
    if version >= 22:
        return len(data) >= 48 and struct.unpack_from(">q", data, 24)[0] == len(data)
    return 9 <= version and struct.unpack_from(">I", data, 4)[0] == len(data)


def parse_serialized_file(data: bytes) -> list[UnityDocument]:
    """SerializedFile の全オブジェクトを ``UnityDocument`` の列にする。

    1 つのオブジェクトだけが読めない場合（参照型のデータなど）はそのオブジェクトを飛ばす。
    1 つも読めなければ最初のエラーを送出する。
    """
    header = _read_header(data)
    version = header.version
    r = _Reader(data, header.endian, header.metadata_offset, header.data_offset)
    r.cstring()  # Unity のバージョン文字列
    r.one("i")  # target platform
    if not r.one("B"):
        raise UnityBinaryError("binary serialized file without a type tree is not supported")

    types = [_read_type(r, version, True) for _ in range(r.count("type", 24))]

    objects: list[tuple[int, int, int, _SerializedType, bool]] = []
    for _ in range(r.count("object", 20)):
        r.align()
        path_id = r.one("q")
        byte_start = r.one("q" if version >= 22 else "I")
        byte_size, type_id = r.unpack("Ii")
        stripped = False
        if version < 16:
            r.one("H")  # class id
            typ = next((t for t in types if t.class_id == type_id), None)
        else:
            typ = types[type_id] if 0 <= type_id < len(types) else None
        if version < 17:
            r.one("h")  # script type index
        if version in (15, 16):
            stripped = bool(r.one("B"))
        if typ is None:
            raise UnityBinaryError(f"object {path_id} refers to unknown type {type_id}")
        objects.append((path_id, header.data_offset + byte_start, byte_size, typ, stripped))

    for _ in range(r.count("script type", 12)):
        r.one("i")
        r.align()
        r.one("q")

    externals: list[_External] = []
    for _ in range(r.count("external", 21)):
        r.cstring()  # 予約（空文字列）
        guid_raw = r.bytes(16)
        ext_type = r.one("i")
        path = r.cstring()
        guid = _guid_text(guid_raw) if any(guid_raw) else _BUILTIN_GUIDS.get(path.replace("\\", "/").lower())
        externals.append(_External(guid, ext_type, path))

    docs: list[UnityDocument] = []
    first_error: UnityBinaryError | None = None
    for path_id, start, size, typ, stripped in objects:
        try:
            if start < header.data_offset or start + size > len(data):
                raise UnityBinaryError(f"object {path_id} lies outside the file")
            reader = _ValueReader(_Reader(data, header.endian, start, start + size), externals)
            tree = typ.tree
            assert tree is not None
            body = reader.read(tree)
        except (UnityBinaryError, RecursionError) as exc:
            if first_error is None:
                first_error = exc if isinstance(exc, UnityBinaryError) else UnityBinaryError("type tree nested too deeply")
            continue
        docs.append(UnityDocument(class_id=typ.class_id, file_id=path_id, stripped=typ.stripped or stripped,
                                  data={tree.type: body}))
    if not docs and first_error is not None:
        raise first_error
    return docs


def load_documents(data: bytes | str) -> list[UnityDocument]:
    """テキスト（Unity YAML）でもバイナリでも、アセットを ``UnityDocument`` の列にする。"""
    if isinstance(data, str):
        return parse_documents(data)
    if is_serialized_file(data):
        return parse_serialized_file(data)
    return parse_documents(data.decode("utf-8", "replace"))
