"""AnimationClip のカーブの階層パスを、読み込んだオブジェクト（とボーン）に結び付ける。bpy 非依存（#209）。

クリップは対象のモデルを持たない（Animator を付けた GameObject からの相対パスだけを持つ）。読み込んだ階層の中で
パスがいちばん多く解決できるオブジェクトを、そのクリップのルート（Animator の GameObject）とみなす。
同じモデルを複数置いたシーンでは、同じだけ解決できるルートがすべて対象になる。

Unity と Blender の階層の違いは次のように吸収する。

- 名前は完全一致を先に、なければ連番（``.001``）を外した形が 1 つだけのものを使う。
- Blender の FBX インポーターはスキンメッシュをアーマチュアの子にするが、Unity ではアーマチュアの GameObject と兄弟。
  アーマチュアのオブジェクトの子（ボーンの子でないもの）は、アーマチュアの親の子としても引けるようにする。
- アーマチュアの下はボーンの名前でたどる（ボーンを対象にしたカーブは #270 まで読み込まない）。
- 読み込み側が足した Empty（FBX のルートなど）で一致しなければ、Empty の子を 1 段だけ探す。
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, field

from .meta import strip_numeric_suffix

__all__ = ["TargetObject", "TargetTree", "Resolved", "Binding", "bind_paths"]

# 対象が決まったとみなす、解決できたパスの割合（ルート自身を指す空のパスは数えない）
MIN_MATCH_RATIO = 0.5


@dataclass(frozen=True)
class TargetObject:
    """読み込んだオブジェクト 1 つ。``name`` は Unity の名前と照合する名前（FBX のノード名）。"""

    key: Hashable
    name: str
    parent: Hashable | None = None
    is_armature: bool = False
    is_empty: bool = False
    bone_parent: str | None = None  # ボーンの子なら、そのボーンの名前
    bones: tuple[tuple[str, str | None], ...] = ()  # アーマチュアなら (ボーン名, 親のボーン名)


@dataclass(frozen=True)
class Resolved:
    """パスの行き先。``bone`` があればアーマチュア ``key`` のボーン。"""

    key: Hashable
    bone: str | None = None


@dataclass
class Binding:
    """クリップのルートと、解決できたパス → 行き先。"""

    root: Hashable | None  # None は「最上位のオブジェクトの集まり」をルートとみなしたもの
    targets: dict[str, Resolved] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)


class _Lookup:
    """名前 → 候補（完全一致 → 連番を外した形が 1 つだけ）。"""

    def __init__(self):
        self.exact: dict[str, list] = {}
        self.base: dict[str, list] = {}

    def add(self, name: str, item) -> None:
        self.exact.setdefault(name, []).append(item)
        self.base.setdefault(strip_numeric_suffix(name), []).append(item)

    def get(self, name: str):
        found = self.exact.get(name)
        if found:
            return found[0]
        found = self.base.get(strip_numeric_suffix(name), [])
        return found[0] if len(found) == 1 else None


class TargetTree:
    """Unity の親子関係に直した、オブジェクトとボーンの木。"""

    def __init__(self, objects: Sequence[TargetObject], top_groups: Iterable[Sequence[Hashable]] = ()):
        self.objects = {o.key: o for o in objects}
        self._children: dict[Hashable | None, _Lookup] = {}
        self._empty_children: dict[Hashable | None, list[Hashable]] = {}
        self._bone_children: dict[tuple[Hashable, str | None], _Lookup] = {}
        self._bone_objects: dict[tuple[Hashable, str], list[Hashable]] = {}
        self._unity_children: dict[Hashable, list[Hashable]] = {}
        for o in objects:
            if o.is_armature:
                for bone, parent in o.bones:
                    self._bone_children.setdefault((o.key, parent), _Lookup()).add(bone, bone)
        for o in objects:
            parent = o.parent if o.parent in self.objects else None
            if parent is None:
                continue
            owner = self.objects[parent]
            if owner.is_armature and o.bone_parent:
                self._bone_objects.setdefault((parent, o.bone_parent), []).append(o.key)
                continue
            if owner.is_armature:
                # スキンメッシュ: Unity ではアーマチュアと兄弟（アーマチュアの子としても引けるようにはしておく）
                self._link(parent, o, unity=False)
                above = owner.parent if owner.parent in self.objects else None
                if above is not None:
                    self._link(above, o)
            else:
                self._link(parent, o)
        self.groups: dict[int, list[Hashable]] = {}
        for index, group in enumerate(top_groups):
            keys = [k for k in group if k in self.objects]
            if keys:
                self.groups[index] = keys
        self._group_lookup: dict[int, _Lookup] = {}
        for index, keys in self.groups.items():
            lookup = _Lookup()
            for k in keys:
                lookup.add(self.objects[k].name, k)
                owner = self.objects[k]
                if owner.is_armature:
                    for child_key, child in self.objects.items():
                        if child.parent == k and not child.bone_parent:
                            lookup.add(child.name, child_key)
            self._group_lookup[index] = lookup

    def _link(self, parent: Hashable, o: TargetObject, unity: bool = True) -> None:
        self._children.setdefault(parent, _Lookup()).add(o.name, o.key)
        if o.is_empty:
            self._empty_children.setdefault(parent, []).append(o.key)
        if unity:
            self._unity_children.setdefault(parent, []).append(o.key)

    # 解決 -----------------------------------------------------------------

    def _child(self, current: Resolved, name: str) -> Resolved | None:
        if current.bone is not None:
            bone = self._bone_children.get((current.key, current.bone))
            found = bone.get(name) if bone else None
            if found is not None:
                return Resolved(current.key, found)
            for key in self._bone_objects.get((current.key, current.bone), []):
                if self.objects[key].name == name or strip_numeric_suffix(self.objects[key].name) == strip_numeric_suffix(name):
                    return Resolved(key)
            return None
        lookup = self._children.get(current.key)
        found = lookup.get(name) if lookup else None
        if found is not None:
            return Resolved(found)
        owner = self.objects.get(current.key)
        if owner is not None and owner.is_armature:
            roots = self._bone_children.get((current.key, None))
            bone = roots.get(name) if roots else None
            if bone is not None:
                return Resolved(current.key, bone)
        for key in self._empty_children.get(current.key, []):
            lookup = self._children.get(key)
            found = lookup.get(name) if lookup else None
            if found is not None:
                return Resolved(found)
        return None

    def resolve(self, root: Hashable | None, path: str, group: int | None = None) -> Resolved | None:
        """``root`` からのパスの行き先（``root`` が None なら ``group`` 番目の最上位の集まりから）。"""
        parts = [p for p in path.split("/")] if path else []
        if root is None:
            if not parts or group is None or group not in self._group_lookup:
                return None
            found = self._group_lookup[group].get(parts[0])
            if found is None:
                return None
            current = Resolved(found)
            parts = parts[1:]
        else:
            current = Resolved(root)
        for part in parts:
            nxt = self._child(current, part)
            if nxt is None:
                return None
            current = nxt
        return current

    def unity_descendants(self, key: Hashable) -> list[Hashable]:
        """Unity の階層での子孫のオブジェクト（自身を含む。GameObject を非アクティブにしたときに隠れるもの）。"""
        result: list[Hashable] = []
        stack = [key]
        seen: set[Hashable] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            stack.extend(self._unity_children.get(current, []))
            owner = self.objects.get(current)
            if owner is not None and owner.is_armature:
                # ボーンの子のオブジェクトはアーマチュアの下にある
                stack.extend(k for (arm, _), keys in self._bone_objects.items() if arm == current for k in keys)
        return result

    # 候補 -----------------------------------------------------------------

    def candidates(self, first_names: set[str]) -> list[Hashable]:
        """パスの先頭の名前を子に持つオブジェクト（ルートの候補）。"""
        bases = {strip_numeric_suffix(n) for n in first_names}
        found = []
        for parent, lookup in self._children.items():
            if any(n in lookup.exact for n in first_names) or any(b in lookup.base for b in bases):
                found.append(parent)
        for key, o in self.objects.items():
            if o.is_armature and key not in found:
                roots = self._bone_children.get((key, None))
                if roots and (any(n in roots.exact for n in first_names) or any(b in roots.base for b in bases)):
                    found.append(key)
        for key, empties in self._empty_children.items():
            if key in found:
                continue
            for empty in empties:
                lookup = self._children.get(empty)
                if lookup and (any(n in lookup.exact for n in first_names) or any(b in lookup.base for b in bases)):
                    found.append(key)
                    break
        return found

    def ancestors(self, key: Hashable) -> set[Hashable]:
        """オブジェクトの祖先（自身を含まない）。"""
        result: set[Hashable] = set()
        current = self.objects.get(key)
        while current is not None and current.parent in self.objects and current.parent not in result:
            result.add(current.parent)
            current = self.objects[current.parent]
        return result

    def depth(self, key: Hashable) -> int:
        depth = 0
        seen: set[Hashable] = set()
        current = self.objects.get(key)
        while current is not None and current.parent in self.objects and current.key not in seen:
            seen.add(current.key)
            depth += 1
            current = self.objects[current.parent]
        return depth


def bind_paths(tree: TargetTree, paths: Iterable[str]) -> list[Binding]:
    """パスをいちばん多く解決できるルートと、その解決結果（同じだけ解決できるルートはすべて）。

    空のパス（ルート自身）だけのクリップは、どのオブジェクトのものか決められないので結び付けない。
    同じオブジェクトに解決する候補（アーマチュアとその親など）は、階層の上のほうを残す。
    """
    paths = sorted(set(paths))
    named = [p for p in paths if p]
    if not named:
        return []
    firsts = {p.split("/")[0] for p in named}
    scored: list[tuple[int, Binding]] = []
    roots: list[tuple[Hashable | None, int | None]] = [(key, None) for key in tree.candidates(firsts)]
    roots += [(None, index) for index in tree.groups]
    for root, group in roots:
        binding = Binding(root)
        for path in paths:
            if not path:
                if root is not None:
                    binding.targets[path] = Resolved(root)
                continue
            found = tree.resolve(root, path, group)
            if found is None:
                binding.unresolved.add(path)
            else:
                binding.targets[path] = found
        count = sum(1 for p in binding.targets if p)
        if count:
            scored.append((count, binding))
    if not scored:
        return []
    best = max(count for count, _ in scored)
    if best < MIN_MATCH_RATIO * len(named):
        return []
    chosen: dict[frozenset, Binding] = {}
    for count, binding in scored:
        if count != best:
            continue
        identity = frozenset((p, r) for p, r in binding.targets.items() if p)
        kept = chosen.get(identity)
        if kept is None or _higher(tree, binding, kept):
            chosen[identity] = binding
    # ルートの子孫を別のルートとみなしたものは外す（prefab の GameObject と、その下に置いた FBX の同じ名前のノード）。
    # 最上位の集まり（root が None）は祖先として扱わない（シーンに置いた複数の配置を残すため）
    roots = {b.root for b in chosen.values() if b.root is not None}
    return [b for b in chosen.values() if b.root is None or not (tree.ancestors(b.root) & roots)]


def _higher(tree: TargetTree, a: Binding, b: Binding) -> bool:
    """a のルートが b より階層の上か（最上位の集まりがいちばん上）。"""
    if b.root is None:
        return False
    if a.root is None:
        return True
    return tree.depth(a.root) < tree.depth(b.root)
