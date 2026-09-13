"""Unity YAML（.mat / .meta / .prefab）を読むための最小パーサー。

Blender 同梱の Python には PyYAML が無いため、Unity のシリアライザが出力する
YAML サブセットだけを依存無しで扱う。対応する構文:

* ブロックマッピング ``key: value`` / ``key:`` + 子ブロック
* ブロックシーケンス ``- item`` と、Unity 特有の「キーと同じインデントで始まる
  シーケンス」および ``- key: value`` 形式の 1 要素マッピング
* フローマッピング ``{fileID: 0, guid: x, type: 3}``（複数行に折り返されたものも可）
* フローシーケンス ``[]`` / ``[a, b]``
* スカラー: int / float / 文字列（クォート有無）。次の行以降に折り返された長い値も 1 つにつなぐ

``--- !u!<classID> &<fileID>`` で区切られた複数ドキュメントにも対応する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "UnityRef",
    "UnityDocument",
    "UnityYamlError",
    "MAX_DEPTH",
    "parse_documents",
    "parse_text",
]


class UnityYamlError(ValueError):
    """パースできない行があったとき、またはネストが深すぎるときに送出される。

    ``ValueError`` の派生なので、呼び出し側は ``ValueError`` でまとめて捕捉できる。
    """


# ブロック / フローのネスト深さの上限。Unity の出力は高々 10 段程度なので十分大きく、
# かつ Python の再帰上限（既定 1000 フレーム）より手前で止まる値にする
MAX_DEPTH = 256


def _check_depth(depth: int, what: str) -> None:
    if depth > MAX_DEPTH:
        raise UnityYamlError(f"{what} nested deeper than {MAX_DEPTH} levels")


@dataclass(frozen=True)
class UnityRef:
    """``{fileID: .., guid: .., type: ..}`` 形式のオブジェクト参照。"""

    file_id: int = 0
    guid: str | None = None
    type: int | None = None

    @property
    def is_null(self) -> bool:
        return self.file_id == 0 and not self.guid


@dataclass
class UnityDocument:
    """``--- !u!21 &2100000`` に続く 1 ドキュメント。"""

    class_id: int
    file_id: int
    stripped: bool
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def type_name(self) -> str | None:
        """例: ``Material``, ``GameObject``。"""
        return next(iter(self.data), None)

    @property
    def body(self) -> dict[str, Any]:
        name = self.type_name
        value = self.data.get(name) if name is not None else None
        return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# 行の前処理
# ---------------------------------------------------------------------------

_DOC_HEADER_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)( stripped)?\s*$")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")
# 値が数値に見えても文字列として保持したいキー
_STRING_KEYS = frozenset({"guid", "m_Name", "name", "pathname", "m_Script"})


@dataclass
class _Line:
    indent: int
    content: str
    number: int  # 元テキストの行番号（エラー表示用）


def _split_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append(_Line(indent, raw.strip(), number))
    return lines


# ---------------------------------------------------------------------------
# スカラー / フロー構文
# ---------------------------------------------------------------------------


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        inner = s[1:-1]
        if s[0] == "'":
            return inner.replace("''", "'")
        return bytes(inner, "utf-8").decode("unicode_escape") if "\\" in inner else inner
    return s


def _scalar(text: str, key: str | None = None) -> Any:
    s = text.strip()
    if s == "":
        return ""
    if s[0] in "'\"":
        return _unquote(s)
    if key in _STRING_KEYS:
        return s
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    lowered = s.lower()
    if lowered in (".inf", "inf", "+.inf"):
        return float("inf")
    if lowered in ("-.inf", "-inf"):
        return float("-inf")
    if lowered in (".nan", "nan"):
        return float("nan")
    return s


class _FlowParser:
    """``{...}`` / ``[...]`` を再帰的に読む。"""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.depth = 0

    def parse(self) -> Any:
        value = self._value()
        self._skip_ws()
        if self.pos != len(self.text):
            raise UnityYamlError(f"unexpected trailing text in flow value: {self.text!r}")
        return value

    # -- helpers --
    def _skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\n":
            self.pos += 1

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _value(self, key: str | None = None) -> Any:
        self._skip_ws()
        ch = self._peek()
        if ch == "{":
            return self._mapping()
        if ch == "[":
            return self._sequence()
        if ch in "'\"":
            return self._quoted()
        return _scalar(self._plain(), key)

    def _quoted(self) -> str:
        quote = self.text[self.pos]
        start = self.pos
        self.pos += 1
        while self.pos < len(self.text):
            c = self.text[self.pos]
            if c == "\\" and quote == '"':
                self.pos += 2
                continue
            if c == quote:
                if quote == "'" and self.text[self.pos + 1 : self.pos + 2] == "'":
                    self.pos += 2
                    continue
                self.pos += 1
                return _unquote(self.text[start : self.pos])
            self.pos += 1
        raise UnityYamlError(f"unterminated quoted string: {self.text!r}")

    def _plain(self) -> str:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in ",}]":
            self.pos += 1
        return self.text[start : self.pos]

    def _mapping(self) -> Any:
        self.pos += 1  # {
        self.depth += 1
        _check_depth(self.depth, "flow value")
        result: dict[str, Any] = {}
        while True:
            self._skip_ws()
            if self._peek() == "}":
                self.pos += 1
                break
            if self._peek() == "":
                raise UnityYamlError(f"unterminated flow mapping: {self.text!r}")
            key_start = self.pos
            while self.pos < len(self.text) and self.text[self.pos] != ":":
                self.pos += 1
            key = _unquote(self.text[key_start : self.pos].strip())
            self.pos += 1  # :
            result[key] = self._value(key)
            self._skip_ws()
            if self._peek() == ",":
                self.pos += 1
        self.depth -= 1
        return _maybe_ref(result)

    def _sequence(self) -> list[Any]:
        self.pos += 1  # [
        self.depth += 1
        _check_depth(self.depth, "flow value")
        items: list[Any] = []
        while True:
            self._skip_ws()
            if self._peek() == "]":
                self.pos += 1
                break
            if self._peek() == "":
                raise UnityYamlError(f"unterminated flow sequence: {self.text!r}")
            items.append(self._value())
            self._skip_ws()
            if self._peek() == ",":
                self.pos += 1
        self.depth -= 1
        return items


def _maybe_ref(mapping: dict[str, Any]) -> Any:
    if "fileID" in mapping and set(mapping) <= {"fileID", "guid", "type"}:
        file_id = mapping.get("fileID", 0)
        guid = mapping.get("guid")
        return UnityRef(
            file_id=int(file_id) if isinstance(file_id, (int, float)) else 0,
            guid=str(guid) if guid not in (None, "") else None,
            type=int(mapping["type"]) if isinstance(mapping.get("type"), (int, float)) else None,
        )
    return mapping


def _flow_balanced(text: str) -> bool:
    depth = 0
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        i += 1
    return depth <= 0 and not quote


def _escaped_line_end(text: str) -> bool:
    """ダブルクォート内の行末が ``\\``（エスケープされた改行）か。``\\\\`` は文字の ``\\`` なので数える。"""
    return (len(text) - len(text.rstrip("\\"))) % 2 == 1


# ---------------------------------------------------------------------------
# ブロック構文
# ---------------------------------------------------------------------------


def _split_key(content: str, line: _Line) -> tuple[str, str]:
    """``key: value`` / ``key:`` を (key, rest) に分ける。"""
    if content[0] in "'\"":
        parser = _FlowParser(content)
        key = parser._quoted()
        rest = content[parser.pos :].lstrip()
        if not rest.startswith(":"):
            raise UnityYamlError(f"line {line.number}: expected ':' after quoted key: {content!r}")
        return key, rest[1:].strip()
    if content.endswith(":"):
        idx = content.find(": ")
        if idx == -1:
            return content[:-1].strip(), ""
    else:
        idx = content.find(": ")
        if idx == -1:
            raise UnityYamlError(f"line {line.number}: expected 'key: value': {content!r}")
    return content[:idx].strip(), content[idx + 1 :].strip()


def _is_seq_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


class _BlockParser:
    def __init__(self, lines: list[_Line]):
        self.lines = lines

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value, i = self._block(0, self.lines[0].indent, 1)
        if i != len(self.lines):
            line = self.lines[i]
            raise UnityYamlError(f"line {line.number}: unexpected indentation: {line.content!r}")
        return value

    def _block(self, i: int, indent: int, depth: int) -> tuple[Any, int]:
        _check_depth(depth, "block")
        if _is_seq_item(self.lines[i].content):
            return self._sequence(i, indent, depth)
        return self._mapping(i, indent, depth)

    def _mapping(self, i: int, indent: int, depth: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        n = len(self.lines)
        while i < n:
            line = self.lines[i]
            if line.indent != indent or _is_seq_item(line.content):
                break
            key, rest = _split_key(line.content, line)
            if rest == "":
                nxt = self.lines[i + 1] if i + 1 < n else None
                if nxt is not None and nxt.indent > indent:
                    value, i = self._block(i + 1, nxt.indent, depth + 1)
                elif nxt is not None and nxt.indent == indent and _is_seq_item(nxt.content):
                    value, i = self._sequence(i + 1, indent, depth + 1)
                else:
                    # ``key: `` （値が空）は Unity では空文字列
                    value, i = "", i + 1
            else:
                value, i = self._inline(i, rest, key, indent)
            result[key] = value
        return result, i

    def _sequence(self, i: int, indent: int, depth: int) -> tuple[list[Any], int]:
        _check_depth(depth, "block")
        items: list[Any] = []
        n = len(self.lines)
        while i < n:
            line = self.lines[i]
            if line.indent != indent or not _is_seq_item(line.content):
                break
            content = line.content[1:].lstrip()
            if content == "":
                nxt = self.lines[i + 1] if i + 1 < n else None
                if nxt is not None and nxt.indent > indent:
                    value, i = self._block(i + 1, nxt.indent, depth + 1)
                else:
                    value, i = None, i + 1
            elif content[0] in "{[" or content[0] in "'\"":
                value, i = self._inline(i, content, None, indent)
            elif _is_seq_item(content) or ": " in content or content.endswith(":"):
                # ``- key: value`` / ``- - item`` → ダッシュ以降を仮想的なインデントの
                # ブロックとして読み直す
                virtual_indent = indent + (len(line.content) - len(content))
                self.lines[i] = _Line(virtual_indent, content, line.number)
                value, i = self._block(i, virtual_indent, depth + 1)
            else:
                value, i = self._inline(i, content, None, indent)
            items.append(value)
        return items, i

    def _inline(self, i: int, text: str, key: str | None, indent: int) -> tuple[Any, int]:
        if text[0] in "{[":
            joined = text
            while not _flow_balanced(joined):
                i += 1
                if i >= len(self.lines):
                    raise UnityYamlError(f"unterminated flow value: {text!r}")
                joined += " " + self.lines[i].content
            return _FlowParser(joined).parse(), i + 1
        text, i = self._fold(i, text, indent)
        return _scalar(text, key), i + 1

    def _fold(self, i: int, text: str, indent: int) -> tuple[str, int]:
        """親（``indent``）より深いインデントで続く行を、折り返されたスカラーの続きとしてつなぐ。

        Unity は長い値（``m_ShaderKeywords`` など）を次の行に折り返して書く。YAML と同じく行の区切りは空白 1 つにし、
        ダブルクォート内で行末が ``\\`` のときは空白を入れない。クォート付きは閉じるまで読む。
        空行は ``_split_lines`` で落ちるので、値の中の改行は保持しない（読み込みで使うキーには出てこない）。
        """
        n = len(self.lines)
        quoted = text[0] in "'\""
        while i + 1 < n and self.lines[i + 1].indent > indent:
            if quoted and _flow_balanced(text):
                break
            line = self.lines[i + 1]
            if not quoted and (": " in line.content or line.content.endswith(":")):
                raise UnityYamlError(f"line {line.number}: unexpected indentation: {line.content!r}")
            if quoted and text[0] == '"' and _escaped_line_end(text):
                text = text[:-1] + line.content
            else:
                text = f"{text} {line.content}"
            i += 1
        return text, i


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------


def _parse_block(lines: list[_Line]) -> Any:
    """ブロックを解析する。深さ上限で止まるはずだが、万一の RecursionError も UnityYamlError に揃える。"""
    try:
        return _BlockParser(lines).parse()
    except RecursionError as exc:
        raise UnityYamlError("YAML nested too deeply") from exc


def parse_text(text: str) -> Any:
    """ヘッダー無しの YAML（.meta など）を dict / list に変換する。

    構文エラーとネスト過多はいずれも ``UnityYamlError``（``ValueError`` の派生）。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    body = [ln for ln in _split_lines(text) if not ln.content.startswith("%")]
    return _parse_block(body)


def parse_documents(text: str) -> list[UnityDocument]:
    """``--- !u!N &id`` で区切られた Unity YAML を全ドキュメント読み込む。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    docs: list[UnityDocument] = []
    current: UnityDocument | None = None
    buffer: list[_Line] = []

    def flush() -> None:
        nonlocal buffer
        if current is not None:
            current.data = _parse_block(buffer) or {}
            docs.append(current)
        buffer = []

    for line in _split_lines(text):
        if line.indent == 0 and line.content.startswith("%"):
            continue
        m = _DOC_HEADER_RE.match(line.content) if line.indent == 0 else None
        if m:
            flush()
            current = UnityDocument(
                class_id=int(m.group(1)),
                file_id=int(m.group(2)),
                stripped=bool(m.group(3)),
            )
            continue
        if line.indent == 0 and line.content == "---":
            flush()
            current = UnityDocument(class_id=0, file_id=0, stripped=False)
            continue
        if current is None:
            current = UnityDocument(class_id=0, file_id=0, stripped=False)
        buffer.append(line)
    flush()
    return docs
