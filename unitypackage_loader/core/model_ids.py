"""読み込んだモデルのオブジェクトを、Unity がモデルの中のオブジェクトに付ける fileID に結び付ける。bpy 非依存。

.meta に番号の表が無い新しい形式のモデルでは、prefab・シーンからモデルの中への上書きの対象は fileID だけで書かれている。
fileID は名前（と階層パス）のハッシュなので（``unity_ids``）、逆向きには引けない。そこで、読み込んだ Blender の
オブジェクトの名前と親子関係から fileID を計算し、上書きの対象と突き合わせる（Issue #31）。64 bit のハッシュが一致すれば
対象は確定するので、世代（``fileIdsGeneration``）や、最上位のノードを Unity がルートに畳んだかどうかは、ありうる形を
すべて計算して突き合わせればよい。

決められないのは名前のほうで、次の場合は「分からない」として fileID を出さない（推測で当てると別の部位の色が変わるため）。

- FBX の中に同じ名前のノードが複数ある。名前で決まる世代ではオフセットの順が、階層パスで決まる世代でも兄弟の中での
  順（Unity が「名前 1」に変える相手）が、Blender の連番の順と一致しない（Unity 6000.6 で確認）。階層パスで決まる世代で
  親が違う同名ノードは、パスが違うので決まる。
- 先に読み込んであったオブジェクトと名前がぶつかって Blender が連番を付け替え、元の名前が 1 つに決まらない
  （FBX に「Ring」と「Ring.001」が両方あるモデルを 2 回読み込んだときなど）。

FBX のノード名の一覧（``fbx_node_names``）が分かれば、Blender の連番が FBX に元からあるものか、付け替えたものかを区別できる。
分からないモデル（FBX 以外や、読めなかったもの）は、名前の連番を外した形が他と重ならないオブジェクトだけを扱う。

表示の上書きと削除（``hierarchy.PartState``）は ``PartMatcher`` で、隠すオブジェクトと Unity に無いオブジェクトにする（#172）。
"""

from __future__ import annotations

import itertools
import re
import struct
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .hierarchy import PartState, pending_object_id, pending_renderer_id
from .meta import strip_numeric_suffix
from .unity_ids import MODEL_ROOT_NAME, hierarchy_file_id, named_file_id

RENDERER_TYPES = ("MeshRenderer", "SkinnedMeshRenderer")
_MASK = 0x7FFF_FFFF_FFFF_FFFF  # ``hierarchy`` の表の key と同じく上位ビットを落とす
_BINARY_MAGIC = b"Kaydara FBX Binary"
_BINARY_MODEL = b"\x00\x01Model"  # バイナリ FBX のノード名は「<名前>\0\1Model」
_ASCII_MODEL = re.compile(rb'^[ \t]*Model:\s*(?:-?\d+\s*,\s*)?"Model::([^"\r\n]*)"', re.MULTILINE)
_MAX_NAME = 1024
_MAX_FILE = 1 << 30  # これより大きい FBX は読まない（ノード名は分からない扱いになる）
_MAX_PATHS = 64  # 1 つのオブジェクトで試す階層パスの数の上限


@dataclass
class ModelObject:
    """読み込んだモデルのオブジェクト 1 つ。

    ``paths`` は、モデルの最上位からこのオブジェクトまでの名前の並び（Blender での名前）。ボーンの子になっている
    オブジェクトは、アーマチュアのオブジェクトを含む形と含まない形のように、ありうる並びを複数渡してよい。
    """

    name: str
    paths: list[tuple[str, ...]] = field(default_factory=list)
    is_mesh: bool = True
    parent: str | None = None  # 親のオブジェクトの名前（モデルの外の親は None）
    is_armature: bool = False


def parse_fbx_node_names(data: bytes) -> Counter[str] | None:
    """FBX の中身から、ノード（Model）の名前と数を返す。1 つも見つからなければ None。"""
    names: Counter[str] = Counter()
    if data.startswith(_BINARY_MAGIC):
        position = data.find(_BINARY_MODEL)
        while position >= 0:
            name = _binary_string_ending_at(data, position)
            if name is not None:
                names[name] += 1
            position = data.find(_BINARY_MODEL, position + 1)
    else:
        for match in _ASCII_MODEL.finditer(data):
            names[match.group(1).decode("utf-8", "replace")] += 1
    return names or None


def _binary_string_ending_at(data: bytes, end: int) -> str | None:
    """``end`` で名前が終わる文字列属性（``S`` + 長さ + 「<名前>\\0\\1Model」）の名前。"""
    tail = len(_BINARY_MODEL)
    for length in range(0, min(_MAX_NAME, end - 5) + 1):
        start = end - length
        if data[start - 5 : start - 4] == b"S" and struct.unpack_from("<I", data, start - 4)[0] == length + tail:
            return data[start:end].decode("utf-8", "replace")
    return None


def fbx_node_names(path: str | Path) -> Counter[str] | None:
    """FBX ファイルのノードの名前と数。FBX でない・読めない・大きすぎるときは None。"""
    try:
        target = Path(path)
        if target.suffix.lower() != ".fbx" or target.stat().st_size > _MAX_FILE:
            return None
        return parse_fbx_node_names(target.read_bytes())
    except (OSError, ValueError, struct.error):
        return None


class _Names:
    """Blender での名前から、Unity でのノード名の候補を引く。"""

    def __init__(self, objects: Sequence[ModelObject], native: Counter[str] | None, taken: Collection[str]):
        self._native = native
        self._taken = taken  # 読み込む前からあったオブジェクトの名前の、連番を外した形
        self._groups: dict[str, Counter[str]] = {}  # 連番を外した形 → FBX にある名前と数
        for name, count in (native or {}).items():
            self._groups.setdefault(strip_numeric_suffix(name), Counter())[name] += count
        self._bases = Counter(strip_numeric_suffix(o.name) for o in objects)

    def resolve(self, name: str) -> tuple[str | None, bool]:
        """(Unity でのノード名, FBX に同じ名前のノードが複数あるか)。決められなければ名前は None。"""
        base = strip_numeric_suffix(name)
        if self._native is None:
            # FBX の名前が分からない。連番を外した形が他と重なるものは、元の名前か付け替えた名前か区別できない
            if base in self._taken or self._bases[base] > 1:
                return None, False
            return name, False
        group = self._groups.get(base, Counter())
        duplicated = any(count > 1 for count in group.values())
        if duplicated:
            # 同じ名前のノードが複数ある。連番付きの名前も混ざっていれば、どれがどれか決められない
            return (base, True) if len(group) == 1 and base in group else (None, True)
        if base not in self._taken:
            return name, False  # 名前がぶつかる相手がいないので、Blender は名前を変えていない
        if len(group) == 1:
            return next(iter(group)), False  # 付け替えられていても、元の名前は 1 つしか無い
        return None, False


def object_claims(
    objects: Sequence[ModelObject],
    type_names: Iterable[str] = RENDERER_TYPES,
    native: Counter[str] | None = None,
    taken: Collection[str] = (),
    meshes_only: bool = True,
) -> dict[int, str]:
    """fileID（上位ビットを落とした値）→ オブジェクトの名前。対象が 1 つに決まるものだけを返す。

    ``type_names`` は Unity の型の名前（Renderer なら ``RENDERER_TYPES``、位置なら ``Transform``）。``native`` は
    ``fbx_node_names`` の結果、``taken`` は読み込む前からあったオブジェクトの名前（連番を外した形）。
    ``meshes_only`` なら返すのはメッシュのオブジェクトだけ（Renderer の fileID のとき）。同じ fileID になるオブジェクトが
    他にあれば（メッシュでなくても）返さない。
    """
    type_names = tuple(type_names)
    names = _Names(objects, native, taken)
    top_level = {o.paths[0][0] for o in objects if o.paths and o.paths[0]}
    # 最上位のノードが 1 つだけなら Unity はルートに畳む。Blender が読まないノードもあるので、畳まない形も試す
    collapsed = len(top_level) == 1
    claims: dict[int, set[str]] = {}

    def claim(file_id: int, obj: ModelObject) -> None:
        claims.setdefault(file_id & _MASK, set()).add(obj.name)

    for obj in objects:
        leaf, leaf_duplicated = names.resolve(obj.name)
        if leaf is None:
            continue
        seen: set[tuple[str, ...]] = set()
        for raw in itertools.islice(obj.paths, _MAX_PATHS):
            resolved = [names.resolve(part)[0] for part in raw[:-1]]
            variants: list[tuple[str, ...]] = []
            if all(part is not None for part in resolved):
                variants.append((*resolved, leaf))  # type: ignore[arg-type]
            # ルートに畳む形は最上位のノードの名前を使わないので、その名前が決まらなくても計算できる
            # （2 回目の読み込みで、最上位のアーマチュアが「Armature.001」になったときなど）
            if collapsed and len(raw) > 1 and all(part is not None for part in resolved[1:]):
                variants.append((*resolved[1:], leaf))  # type: ignore[arg-type]
            for variant in variants:
                if variant in seen:
                    continue
                seen.add(variant)
                for type_name in type_names:
                    claim(hierarchy_file_id(type_name, variant), obj)
            if collapsed and len(raw) == 1 and not leaf_duplicated:
                for type_name in type_names:
                    claim(hierarchy_file_id(type_name, ()), obj)  # 最上位のメッシュそのものがルートに畳まれる
                for type_name in type_names:
                    claim(named_file_id(type_name, MODEL_ROOT_NAME), obj)
        if not leaf_duplicated:
            for type_name in type_names:
                claim(named_file_id(type_name, leaf), obj)
    meshes = {o.name for o in objects if o.is_mesh or not meshes_only}
    return {
        file_id: name
        for file_id, owners in claims.items()
        if len(owners) == 1 and (name := next(iter(owners))) in meshes
    }


@dataclass
class PartMatch:
    """表示の上書きと削除を、読み込んだオブジェクトに当てた結果（``PartMatcher.match``）。"""

    hidden: set[str] = field(default_factory=set)  # 隠すオブジェクト（非アクティブな GameObject の子孫と、無効な Renderer）
    removed: set[str] = field(default_factory=set)  # Unity で消されたオブジェクト（使われていない部品と同じ扱い）
    unmatched: int = 0  # 当てられなかった上書き・削除の数（見た目を変えないものと、Renderer 以外の削除は数えない）


class PartMatcher:
    """読み込んだモデルのオブジェクトに、モデルの PrefabInstance の中への表示の上書きと削除を当てる（#172）。

    GameObject の fileID はオブジェクトの種類を問わずに、Renderer の fileID はメッシュのオブジェクトだけに結び付ける
    （``object_claims``）。古い形式の .meta の表で引いた名前の行は、名前（完全一致 → 連番を外した形が 1 つだけ）で引く。

    非アクティブな GameObject と消した GameObject は子孫も対象にする。ただしアーマチュアの子はたどらない。Blender の
    FBX インポーターはスキンメッシュをアーマチュアの子にするが、Unity ではスキンメッシュとボーンの GameObject は別の枝にある。
    """

    def __init__(self, objects: Sequence[ModelObject], native: Counter[str] | None = None, taken: Collection[str] = ()):
        self.object_ids = object_claims(objects, ("GameObject",), native, taken, meshes_only=False)
        self.renderer_ids = object_claims(objects, RENDERER_TYPES, native, taken)
        self._names = {o.name for o in objects}
        self._bases: dict[str, list[str]] = {}
        for o in objects:
            self._bases.setdefault(strip_numeric_suffix(o.name), []).append(o.name)
        self._children: dict[str, list[str]] = {}
        armatures = {o.name for o in objects if o.is_armature}
        for o in objects:
            if o.parent is not None and o.parent not in armatures:
                self._children.setdefault(o.parent, []).append(o.name)

    def _by_name(self, name: str) -> str | None:
        if name in self._names:
            return name
        candidates = self._bases.get(strip_numeric_suffix(name), [])
        return candidates[0] if len(candidates) == 1 else None

    def _subtree(self, roots: Iterable[str]) -> set[str]:
        result: set[str] = set()
        stack = list(roots)
        while stack:
            name = stack.pop()
            if name not in result:
                result.add(name)
                stack.extend(self._children.get(name, []))
        return result

    def match(self, parts: Mapping[str, PartState]) -> PartMatch:
        hide_trees: list[str] = []
        hide_own: set[str] = set()
        remove_trees: list[str] = []
        remove_own: set[str] = set()
        unmatched = 0
        for key, state in parts.items():
            hides_tree, hides_own = state.active is False, state.enabled is False
            if not (hides_tree or hides_own or state.removed):
                continue  # 表示する値（Unity の既定値）を書いただけの上書き
            if (uid := pending_object_id(key)) is not None:
                name = self.object_ids.get(uid)
            elif (uid := pending_renderer_id(key)) is not None:
                name = self.renderer_ids.get(uid)
            else:
                name = self._by_name(key)
            if name is None:
                # Renderer 以外（コライダーなど）を消したものは区別できないので数えない
                unmatched += int(hides_tree or hides_own or state.removed_object)
                continue
            if hides_tree:
                hide_trees.append(name)
            if hides_own:
                hide_own.add(name)
            if state.removed_object:
                remove_trees.append(name)
            if state.removed_renderer:
                remove_own.add(name)
        removed = self._subtree(remove_trees) | remove_own
        hidden = (self._subtree(hide_trees) | hide_own) - removed
        return PartMatch(hidden, removed, unmatched)
