"""シーン（.unity）や prefab の階層の展開。PrefabInstance の元を再帰的に差し込み、上書きと削除を当てる。bpy 非依存。

展開のしかた:

- 各アセットの Transform（RectTransform を含む）・GameObject・Renderer を読み、PrefabInstance は元のアセットを
  再帰的に展開して差し込む。Node の key は展開ごとに振る通し番号で、fileID からは表（``Hierarchy.transforms`` など）で
  引く。差し込んだ元のオブジェクトの fileID は「PrefabInstance の fileID XOR 元の fileID」（上位ビットは落とす。
  prefab の中で Unity が使う番号と同じ）。
- 外から元のオブジェクトを指す参照（子を付ける親、上書き先）は、stripped ドキュメント
  （``m_CorrespondingSourceObject`` と ``m_PrefabInstance``）を対応表にして引く。シーンの fileID は XOR の規則に
  従わないため（Issue #48 の調査）。PrefabInstance の上書き・削除と stripped の参照は、PrefabInstance ごとの
  「元のアセットでの fileID → 差し込んだ Node」の表で引き、XOR の値は使わない。fileID を連番に振り直したパッケージでは
  XOR の値が別の PrefabInstance のものと重なるため（Issue #107）。
- ``LODGroup`` の ``m_LODs`` は、段ごとに並んだ Renderer の fileID として読み、``RendererInfo.lod_level`` に付ける
  （0 = もっとも細かい LOD0）。読み込み側は 0 以外を非表示にする。
- ``m_Modifications`` のうち、位置・回転・スケール（``m_LocalPosition.x`` など）、``m_IsActive``、``m_Name``、
  Renderer の ``m_Enabled`` とマテリアル（``m_Materials.Array.data[N]`` / ``.size``）を反映する。
  ``m_RemovedGameObjects`` / ``m_RemovedComponents`` で消されたものは除く。
- 元がモデル（FBX 等）の PrefabInstance は、モデルのルートを表す 1 つの Node にする。ルートの Transform の fileID は
  FBX によらず定数（.meta の世代で 2 通り）なので上書きを当てられる。中のオブジェクトの fileID は名前と階層パスの
  ハッシュで、ここでは名前に結び付けられない。マテリアルの上書きは対象の fileID ごとに残して読み込み側が突き合わせ
  （``pending_renderer``。Issue #31）、表示（``m_IsActive`` / ``m_Enabled``）の上書きと削除も同じく対象ごとに
  ``PartState`` として残す（``pending_object``。Issue #172）。位置などそれ以外は読まずに数える。
- Unity 2018.2 以前の形式も読む。PrefabInstance はクラス名が ``Prefab`` で元を ``m_ParentPrefab`` に持ち、
  stripped ドキュメントの参照は ``m_PrefabParentObject`` / ``m_PrefabInternal``、モデルのルートの fileID は
  GameObject 100000 / Transform 400000。
"""

from __future__ import annotations

import copy
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

from .hierarchy import (
    CLASS_GAME_OBJECT,
    CLASS_TRANSFORM,
    LEGACY_MODEL_IDS,
    MAX_NESTING,
    MAX_NODES,
    MAX_SLOTS,
    MATERIAL_PATH,
    MODEL_ROOT_GAME_OBJECT,
    MODEL_ROOT_TRANSFORM,
    MODEL_ROOT_UIDS,
    NAMED_MODEL_ROOT_GAME_OBJECT,
    NAMED_MODEL_ROOT_TRANSFORM,
    RENDERER_CLASSES,
    SOLE_NODE,
    TRS_FIELDS,
    TRS_PATH,
    AXES,
    Hierarchy,
    HierarchyError,
    Node,
    PartState,
    RawAsset,
    _RawInstance,
    collapsed_root_name,
    legacy_class,
    model_object_name,
    pending_object,
    pending_renderer,
    remap,
    file_uid,
    ID_MASK,
)
from .transform import chain
from .unity_yaml import UnityRef, ref_guid, to_float


_TABLES = ("transforms", "game_objects", "renderers", "components")


@dataclass
class _Inserted:
    """差し込んだ PrefabInstance の中のオブジェクトの表（元のアセットでの fileID → 差し込んだ Node の key）。

    上書き・削除・stripped の参照はこの表で引く。XOR した fileID は別の PrefabInstance のものと重なることがあるため。
    """

    instance_id: int
    transforms: dict[int, int] = field(default_factory=dict)
    game_objects: dict[int, int] = field(default_factory=dict)
    renderers: dict[int, int] = field(default_factory=dict)
    components: dict[int, int] = field(default_factory=dict)


class Expander:
    """アセットの GUID から ``RawAsset`` を得る関数を受け取り、階層を展開する（prefab の展開結果はキャッシュする）。"""

    def __init__(
        self,
        read_asset: Callable[[str], RawAsset | None],
        model_names: dict[str, str],
        model_recycle_names: dict[str, dict[int, str]] | None = None,
    ):
        self._read = read_asset
        self._models = {g.lower(): name for g, name in model_names.items()}  # モデルの GUID → ルートの名前
        # モデルの GUID → 古い形式の .meta の fileIDToRecycleName（中への上書きを名前に結び付ける）
        self._recycle = {g.lower(): table for g, table in (model_recycle_names or {}).items() if table}
        # 1 メッシュの FBX（Unity がノードを //RootNode に畳むモデル）の GUID → そのノードの名前（#99）
        self._collapsed = {g: name for g, table in self._recycle.items() if (name := collapsed_root_name(table))}
        # GUID → (展開結果, 深さで打ち切ったときの展開時のスタックの長さ。打ち切りが無ければ None)
        self._cache: dict[str, tuple[Hierarchy | None, int | None]] = {}

    def expand_asset(self, guid: str) -> Hierarchy | None:
        return self._asset(guid.lower(), ())

    def expand_raw(self, raw: RawAsset) -> Hierarchy:
        """シーンなど、キャッシュしないアセットを展開する。"""
        return self._build(raw, ())

    def _asset(self, guid: str, stack: tuple[str, ...]) -> Hierarchy | None:
        """prefab を展開する（結果はキャッシュする）。

        深さの上限で中の PrefabInstance を打ち切った結果は、同じかそれより深い位置からだけ使い回し、浅い位置から
        呼ばれたら展開し直す（打ち切らずに済むため）。展開し直しは GUID ごとに深さの数までに収まる。
        循環参照（Unity では作れない不正なデータ）で外した結果は、そのまま使い回す。循環のたびに展開し直すと、
        細工されたデータで回数が指数的に増えるため。
        """
        cached = self._cache.get(guid)
        if cached is not None:
            result, truncated_at = cached
            if truncated_at is None or len(stack) >= truncated_at:
                return result
        if guid in stack or len(stack) >= MAX_NESTING:
            return None
        raw = self._read(guid)
        result = self._build(raw, stack + (guid,)) if raw is not None else None
        self._cache[guid] = (result, len(stack) if result is not None and result.depth_truncated else None)
        return result

    def _model(self, guid: str) -> Hierarchy:
        h = Hierarchy()
        root = Node(
            key=h.new_key(),
            name=self._models[guid],
            position=[0.0, 0.0, 0.0],
            rotation=[0.0, 0.0, 0.0, 1.0],
            scale=[1.0, 1.0, 1.0],
            game_object=MODEL_ROOT_GAME_OBJECT,
            model_guid=guid,
        )
        root.scope = root.key
        h.nodes[root.key] = root
        # ルートの fileID は .meta の世代で 2 通りある。上書きはどちらか一方でしか書かれないので、両方を同じ Node に向ける
        for transform_id, game_object_id in (
            (MODEL_ROOT_TRANSFORM, MODEL_ROOT_GAME_OBJECT), (NAMED_MODEL_ROOT_TRANSFORM, NAMED_MODEL_ROOT_GAME_OBJECT)
        ):
            h.transforms[file_uid(transform_id)] = root.key
            h.game_objects[file_uid(game_object_id)] = root.key
        return h

    def _build(self, raw: RawAsset, stack: tuple[str, ...]) -> Hierarchy:
        h = Hierarchy(counts=Counter(raw.counts))
        model_instances = {i.file_id for i in raw.instances if i.source_guid in self._models}
        inserted: dict[int, _Inserted] = {}  # PrefabInstance の fileID → 差し込んだオブジェクトの表

        def resolve(file_id: int, table: str) -> int | None:
            """fileID（stripped の別名を含む）の指すオブジェクトの Node。``table`` は ``_TABLES`` のどれか。"""
            alias = raw.aliases.get(file_id)
            if not alias:
                return getattr(h, table).get(file_uid(file_id))
            instance_id, source_id = alias
            scope = inserted.get(instance_id)
            if scope is None:
                return None
            if instance_id in model_instances:
                source_id = LEGACY_MODEL_IDS.get(source_id, source_id)
            return getattr(scope, table).get(file_uid(source_id))

        fathers: list[tuple[int, int]] = []  # (Node の key, 親の fileID)。差し込みが終わってから解決する
        for t in raw.transforms.values():
            node = Node(
                key=h.new_key(),
                name=raw.names.get(t.game_object, ""),
                position=t.position,
                rotation=t.rotation,
                scale=t.scale,
                active=raw.active.get(t.game_object, True),
                game_object=file_uid(t.game_object),
                rect=t.rect,
            )
            h.nodes[node.key] = node
            h.transforms[file_uid(t.file_id)] = node.key
            h.game_objects[node.game_object] = node.key
            if t.father:
                fathers.append((node.key, t.father))
        own = set(h.nodes)
        # アセット自身の Transform 同士の親子は先に付ける（scope を決めるため）
        for key, father in fathers:
            if father not in raw.aliases:
                h.nodes[key].parent = h.transforms.get(file_uid(father))
        for renderer_id, (go, info) in raw.renderers.items():
            key = h.game_objects.get(file_uid(go))
            if key is not None:
                h.nodes[key].renderer = info
                h.nodes[key].renderer_key = file_uid(renderer_id)
                h.renderers[file_uid(renderer_id)] = key
        for component_id, (go, class_id, body) in raw.components.items():
            key = h.game_objects.get(file_uid(go))
            if key is not None:
                h.nodes[key].components[file_uid(component_id)] = (class_id, copy.deepcopy(body))
                h.components[file_uid(component_id)] = key
        _assign_scopes(h, own)

        roots: list[tuple[int, int]] = []  # (差し込んだ最上位の Node の key, m_TransformParent の fileID)
        for instance in raw.instances:
            if len(h.nodes) > MAX_NODES:
                raise HierarchyError(f"hierarchy has more than {MAX_NODES} objects")
            scope = self._insert(h, instance, stack, roots)
            if scope is not None:
                inserted[instance.file_id] = scope
        # 親は別の PrefabInstance の中にあることもあるので、全部を差し込んでから付ける
        for key, father in fathers + roots:
            if father and key in h.nodes and (father in raw.aliases or h.nodes[key].parent is None):
                parent = resolve(father, "transforms")
                h.nodes[key].parent = parent if parent in h.nodes else None
        # LODGroup は差し込んだ PrefabInstance の中の Renderer を指すことがあるので、差し込みの後に当てる
        _apply_lod_groups(h, raw.lod_groups, lambda file_id: resolve(file_id, "renderers"))
        return h

    def _insert(
        self, h: Hierarchy, instance: _RawInstance, stack: tuple[str, ...], roots: list[tuple[int, int]]
    ) -> _Inserted | None:
        """PrefabInstance の元を差し込み、上書きと削除を当てる。最上位の Node は ``roots`` に足す（親は呼び出し側で付ける）。"""
        source = instance.source_guid
        is_model = source in self._models
        if not is_model and source not in stack and len(stack) >= MAX_NESTING:
            h.depth_truncated = True
        sub = self._model(source) if is_model else self._asset(source, stack)
        if sub is None:
            h.missing_sources += 1
            return None
        h.depth_truncated = h.depth_truncated or sub.depth_truncated
        insertion = _Insertion(self, h, instance, sub, is_model)
        insertion.copy_nodes(roots)
        insertion.remove_game_objects()
        insertion.remove_components()
        insertion.mark_collapsed_root()
        insertion.apply_modifications()
        if insertion.through:
            self._override_through(h, sub, insertion.added, insertion.through)
        return insertion.scope

    def _override_through(self, h: Hierarchy, sub: Hierarchy, added: dict[int, int], overrides: list[_Through]) -> None:
        """prefab を挟んだ外側からの、モデルの中のオブジェクトへの上書き・削除を、モデルのルートに記録する（#31 / #172）。

        外側のアセットは、prefab の中のモデルのオブジェクトを「PrefabInstance の fileID XOR モデルの中の fileID」で指す
        （Prefab Variant をシーンに置き、シーンが部品のマテリアルを替えたときなど）。展開したオブジェクトのどれにも
        当たらなかった上書きの対象を、差し込んだ prefab の中のモデルのルートごとに XOR で戻し、次の順で当てる。

        1. 古い形式の .meta の表で名前を引ける
        2. 内側のアセットが同じオブジェクトを上書きしていて、その fileID の行がある
        3. どちらでもなく、prefab の中のモデルが 1 つだけなら、そのモデルの中の fileID として行を足す。モデルが複数あると
           どのモデルの中かを決められないので、当てない（今までと同じ）

        3 で足した行が実際にモデルのオブジェクトを指しているかは、読み込み側が fileID の計算で確かめる。
        """
        roots = [(node.id_mask, h.nodes[added[key]]) for key, node in sub.nodes.items() if node.model_guid is not None]
        roots = [(mask, root) for mask, root in roots if root.key in h.nodes]

        def named_row(override: _Through, root: Node, inner: int) -> str | None:
            return _part_key(self._recycle.get(root.model_guid or "", {}), inner, inner, override.kind, named_only=True)

        def existing_row(override: _Through, root: Node, inner: int) -> str | None:
            key = _pending_name(override.kind, inner)
            return key if key in root.model_materials or key in root.model_parts else None

        def single_model_row(override: _Through, root: Node, inner: int) -> str | None:
            return _pending_name(override.kind, inner) if len(roots) == 1 and inner not in MODEL_ROOT_UIDS else None

        for override in overrides:
            for find in (named_row, existing_row, single_model_row):
                hit = next(
                    ((root, name) for mask, root in roots if (name := find(override, root, (override.uid ^ mask) & ID_MASK)) is not None),
                    None,
                )
                if hit is not None:
                    override.apply(*hit)
                    break


def _pending_name(kind: str, uid: int) -> str:
    """名前を引けなかったモデルの中のオブジェクトの行の名前（``kind`` は "object" か "renderer"）。"""
    return pending_object(uid) if kind == "object" else pending_renderer(uid)


class _Insertion:
    """PrefabInstance 1 つの差し込み（``Expander._insert``）。上書き・削除を当てる間の状態を持つ。

    以前は _insert の 1 つのメソッド（約 125 行）に、入れ子の関数と lambda で書いていた（#198）。
    """

    def __init__(self, expander: Expander, h: Hierarchy, instance: _RawInstance, sub: Hierarchy, is_model: bool):
        self.h = h
        self.instance = instance
        self.sub = sub
        self.source = instance.source_guid
        self.is_model = is_model
        self.iid = instance.file_id
        self.added: dict[int, int] = {}  # 元の key → 差し込んだ key（copy_nodes で作る）
        self.scope = _Inserted(self.iid)
        # モデルの PrefabInstance なら、そのルートの Node と、古い形式の .meta の表（中への上書きを名前に結び付ける）
        self.root_key: int | None = None
        self.table = expander._recycle.get(self.source, {}) if is_model else {}
        self.collapsed = expander._collapsed.get(self.source) if is_model else None
        self.has_table = self.source in expander._recycle
        # prefab を挟んで、その中のモデルのオブジェクトを指しているかもしれない上書き・削除（#31 / #172）
        self.through: list[_Through] = []

    def copy_nodes(self, roots: list[tuple[int, int]]) -> None:
        """元の階層の Node を差し込み、fileID の表を作る。最上位の Node は ``roots`` に足す。"""
        h, sub, iid = self.h, self.sub, self.iid
        self.added = added = {key: h.new_key() for key in sub.nodes}
        for key, node in sub.nodes.items():
            new_key = added[key]
            copied = node.copy(
                key=new_key,
                parent=added.get(node.parent) if node.parent is not None else None,
                game_object=remap(iid, node.game_object),
                renderer_key=remap(iid, node.renderer_key) if node.renderer else 0,
                scope=added.get(node.scope, 0),
                id_mask=(node.id_mask ^ iid) & ID_MASK,
            )
            copied.components = {remap(iid, k): value for k, value in copied.components.items()}
            h.nodes[new_key] = copied
            if node.parent is None:
                roots.append((new_key, self.instance.parent))
        for table in _TABLES:
            local = {uid: added[key] for uid, key in getattr(sub, table).items() if key in added}
            setattr(self.scope, table, local)
            outer = getattr(h, table)
            for uid, key in local.items():
                outer.setdefault(remap(iid, uid), key)
        h.counts.update(sub.counts)
        h.unresolved_overrides += sub.unresolved_overrides
        h.unresolved_material_overrides += sub.unresolved_material_overrides
        h.missing_sources += sub.missing_sources
        if self.is_model:
            self.root_key = self.scope.transforms.get(file_uid(MODEL_ROOT_TRANSFORM))

    def target(self, file_id: int, guid: str | None) -> int | None:
        """上書き・削除の対象の、元のアセットでの fileID。別のアセットを指していれば None。"""
        if guid is not None and guid != self.source:
            return None
        if self.is_model:
            file_id = LEGACY_MODEL_IDS.get(file_id, file_id)
        return file_uid(file_id)

    def model_root(self) -> Node | None:
        """モデルの PrefabInstance のルートの Node（消されていれば None）。"""
        return self.h.nodes.get(self.root_key) if self.root_key is not None else None

    def model_part(self, file_id: int, uid: int, kind: str, **changes: bool) -> bool:
        """モデルの中のオブジェクトへの表示の上書き・削除を、ルートに記録する。記録できなければ False。"""
        key = _part_key(self.table, file_id, uid, kind)
        root = self.model_root()
        if key is None or root is None:
            return False
        _record_part(root, key, **changes)
        return True

    def remove_game_objects(self) -> None:
        """``m_RemovedGameObjects``: 差し込んだ GameObject を子孫ごと消す。モデルの中のものは記録する。"""
        h = self.h
        children: dict[int, list[int]] | None = None  # 消すたびに全 Node を走査し直さないよう、初めて消すときに 1 回だけ作る
        for file_id, guid in self.instance.removed_game_objects:
            uid = self.target(file_id, guid)
            if uid is None:
                continue
            node_key = self.scope.game_objects.get(uid)
            if node_key is not None and node_key in h.nodes:
                if children is None:
                    children = h.children()
                _remove_subtree(h, node_key, children)
            elif not self.is_model:
                self.through.append(_Through(uid, "object", lambda root, key: _record_part(root, key, removed_object=True)))
            elif not self.model_part(file_id, uid, "object", removed_object=True):
                h.unresolved_overrides += 1

    def remove_components(self) -> None:
        """``m_RemovedComponents``: 差し込んだ Renderer・コンポーネントを消す。モデルの中のものは記録する。"""
        for file_id, guid in self.instance.removed_components:
            uid = self.target(file_id, guid)
            if uid is None or _remove_component(self.h, self.scope, uid):
                continue
            # Renderer 以外（コライダーなど）を消したものもここに来る。表の無いモデルでは区別できないので記録だけし、
            # 読み込み側が Renderer の fileID と突き合わせる
            if not self.is_model:
                self.through.append(_Through(uid, "renderer", lambda root, key: _record_part(root, key, removed_renderer=True)))
            elif not self.model_part(file_id, uid, "renderer", removed_renderer=True):
                self.h.unresolved_overrides += 1

    def mark_collapsed_root(self) -> None:
        """1 メッシュの FBX のモデルの PrefabInstance なら、ルートに畳まれたノードの名前を付ける（#99 / #111）。

        1 メッシュの FBX では、モデルのルートの Transform の値が FBX のノードの変換なので、ルートへの位置・回転・
        スケールの上書きは「どの成分を上書きしたか」も記録する（読み込み側でノードの値に当てるため）。
        """
        collapsed = self.collapsed
        if self.is_model and collapsed is None and not self.has_table:
            # 表が無いと 1 メッシュの FBX か分からないので、成分付きで記録して読み込み側で判定する（#111）
            collapsed = SOLE_NODE
        root = self.model_root()
        if collapsed is not None and root is not None:
            root.collapsed_key = collapsed

    def apply_modifications(self) -> None:
        """``m_Modifications``: 差し込んだオブジェクトへの上書きを当てる。当てられなかったものは記録するか数える。"""
        for file_id, guid, path, value, reference in self.instance.modifications:
            uid = self.target(file_id, guid)
            if uid is None:
                continue
            if _apply_modification(self.h, self.scope, uid, path, value, reference) or not _is_tracked(path):
                continue
            if self.is_model:
                self.model_modification(file_id, uid, path, value, reference)
            else:
                self.through_modification(uid, path, value, reference)

    def through_modification(self, uid: int, path: str, value: object, reference: object) -> None:
        """prefab の中のモデルのオブジェクトを指しているかもしれない上書きを、後で当てるために残す（``_override_through``）。"""
        material = MATERIAL_PATH.fullmatch(path)
        visibility = _visibility_change(path, value)
        if material and int(material.group(1)) < MAX_SLOTS:
            index, material_guid = int(material.group(1)), ref_guid(reference)
            self.through.append(_Through(
                uid, "renderer", lambda root, key, i=index, m=material_guid: _set_slot(root.model_materials.setdefault(key, []), i, m)
            ))
        elif visibility is not None:
            kind, changes = visibility
            self.through.append(_Through(uid, kind, lambda root, key, c=changes: _record_part(root, key, **c)))

    def model_modification(self, file_id: int, uid: int, path: str, value: object, reference: object) -> None:
        """モデルの中のオブジェクトへの上書きを、モデルのルートに記録する（名前を引ければ名前で、引けなければ fileID で）。"""
        h = self.h
        root = self.model_root()
        if root is not None and _apply_named_model_override(root, self.table, file_id, path, value, reference):
            return
        visibility = _visibility_change(path, value)
        if visibility is not None and self.model_part(file_id, uid, visibility[0], **visibility[1]):
            return
        material = MATERIAL_PATH.fullmatch(path)
        if material and uid not in MODEL_ROOT_UIDS and int(material.group(1)) < MAX_SLOTS and root is not None:
            # 名前を引けない Renderer。対象の fileID ごとに残し、読み込んだオブジェクトと突き合わせる（読み込み側。#31）
            _set_slot(root.model_materials.setdefault(pending_renderer(uid), []), int(material.group(1)), ref_guid(reference))
            return
        h.unresolved_overrides += 1
        if material or path == "m_Materials.Array.size":
            h.unresolved_material_overrides += 1


class _Through(NamedTuple):
    """prefab を挟んだ外側からの、モデルの中のオブジェクトへの上書き・削除 1 つ（``Expander._override_through``）。"""

    uid: int  # 対象の fileID（外側のアセットでの値）
    kind: str  # "object"（GameObject）か "renderer"
    apply: Callable[[Node, str], None]  # モデルのルートと、対象の行の名前を受け取って記録する


def _record_part(root: Node, key: str, **changes: bool) -> None:
    state = root.model_parts.setdefault(key, PartState())
    for name, value in changes.items():
        setattr(state, name, value)


def _part_key(table: dict[int, str], file_id: int, uid: int, kind: str, named_only: bool = False) -> str | None:
    """モデルの中のオブジェクトへの表示の上書き・削除を記録する行の名前（#172）。

    ``kind`` は "object"（GameObject）か "renderer"。古い形式の .meta の表があれば、そのクラスの行の名前を引く
    （引けなければ None）。表が無ければ ``pending_object`` / ``pending_renderer`` の名前にする（``named_only`` なら None）。
    ルートは配置そのものなので、ここでは扱わない。
    """
    if table:
        classes = (CLASS_GAME_OBJECT,) if kind == "object" else RENDERER_CLASSES
        return model_object_name(table, file_id) if legacy_class(file_id) in classes else None
    if named_only or uid in MODEL_ROOT_UIDS:
        return None
    return pending_object(uid) if kind == "object" else pending_renderer(uid)


def _visibility_change(path: str, value: object) -> tuple[str, dict[str, bool]] | None:
    """表示の上書きなら (対象の種類, ``PartState`` に記録する値)。"""
    if path == "m_IsActive":
        return "object", {"active": to_float(value, 1.0) != 0}
    if path == "m_Enabled":
        return "renderer", {"enabled": to_float(value, 1.0) != 0}
    return None


def _set_slot(slots: list[str | None], index: int, material_guid: str | None) -> None:
    """スロット順の .mat の並びの ``index`` 番目に入れる（足りなければ None で伸ばす）。``MAX_SLOTS`` 以上は無視する。"""
    if index >= MAX_SLOTS:
        return
    if index >= len(slots):
        slots.extend([None] * (index + 1 - len(slots)))
    slots[index] = material_guid


def _remove_component(h: Hierarchy, scope: _Inserted, uid: int) -> bool:
    """差し込んだ Renderer かコンポーネント（ライト・カメラ・デカール）を消す。見つからなければ False。"""
    node = h.nodes.get(scope.renderers.get(uid))  # type: ignore[arg-type]
    if node is not None and node.renderer is not None:
        if h.renderers.get(node.renderer_key) == node.key:
            del h.renderers[node.renderer_key]
        node.renderer = None
        return True
    node = h.nodes.get(scope.components.get(uid))  # type: ignore[arg-type]
    key = remap(scope.instance_id, uid)
    if node is not None and key in node.components:
        del node.components[key]
        if h.components.get(key) == node.key:
            del h.components[key]
        return True
    return False


def _apply_lod_groups(h: Hierarchy, groups: list[list[list[int]]], resolve: Callable[[int], int | None]) -> None:
    """LODGroup の段を、そこに並んだ Renderer に付ける。``resolve`` は Renderer の fileID から Node の key を引く。

    1 つの LODGroup の中で同じ Renderer が複数の段にあれば、もっとも細かい段（Unity で LOD0 として描かれる方）を採る。
    既に段が付いている Renderer（ネストした prefab の中の LODGroup で付いたもの）は、粗い方を残す。内側の LODGroup で
    遠景用だった Renderer は、外側で LOD0 に並べ直されても内側の判断で隠れたままにするため。
    """
    for levels in groups:
        best: dict[int, int] = {}
        for level, renderers in enumerate(levels):
            for file_id in renderers:
                key = resolve(file_id)
                if key is None or key not in h.nodes or h.nodes[key].renderer is None:
                    continue
                if level < best.get(key, level + 1):
                    best[key] = level
        for key, level in best.items():
            renderer = h.nodes[key].renderer
            renderer.lod_level = max(renderer.lod_level, level)


def _record_node_transform(root: Node, name: str, path: str, value: object) -> bool:
    """モデルの中のノードへの位置・回転・スケールの上書きを、名前付きで配置のルートに記録する。

    上書きの無い成分は ``None`` のままにする（読み込み側で、Blender のオブジェクトから逆算した元の値を使う）。
    """
    match = TRS_PATH.fullmatch(path)
    if match is None:
        return False
    field_name = TRS_FIELDS[match.group(1)]
    size = 4 if field_name == "rotation" else 3
    values = root.model_transforms.setdefault(name, {}).setdefault(field_name, [None] * size)
    index = AXES[match.group(2)]
    if index < size:
        number = to_float(value, float("nan"))
        values[index] = None if math.isnan(number) else number
    return True


def _apply_named_model_override(root: Node, table: dict[int, str], file_id: int, path: str, value: object, reference: object) -> bool:
    """モデルの中への上書きを、表で引いた名前付きで配置のルートに記録する。名前を引けなければ False。"""
    if not table:
        return False
    class_id = legacy_class(file_id)
    name = model_object_name(table, file_id)
    if name is None:
        return False
    if class_id == CLASS_TRANSFORM and _record_node_transform(root, name, path, value):
        return True
    material = MATERIAL_PATH.fullmatch(path)
    if material and class_id in RENDERER_CLASSES:
        index = int(material.group(1))
        if index >= MAX_SLOTS:
            return False
        _set_slot(root.model_materials.setdefault(name, []), index, ref_guid(reference))
        return True
    return False


def _is_tracked(path: str) -> bool:
    return bool(
        TRS_PATH.fullmatch(path)
        or MATERIAL_PATH.fullmatch(path)
        or path in ("m_Materials.Array.size", "m_IsActive", "m_Enabled")
    )


def _apply_modification(h: Hierarchy, scope: _Inserted, uid: int, path: str, value: object, reference: object) -> bool:
    """上書きを 1 つ当てる。``uid`` は元のアセットでの fileID。当てる先が差し込んだオブジェクトに見つからなければ False。"""
    match = TRS_PATH.fullmatch(path)
    if match:
        node = h.nodes.get(scope.transforms.get(uid))  # type: ignore[arg-type]
        if node is None:
            return False
        values = getattr(node, TRS_FIELDS[match.group(1)])
        index = AXES[match.group(2)]
        if index < len(values):
            values[index] = to_float(value, values[index])
        if node.collapsed_key is not None:
            # prefab で包んだ上から上書きした分も含めて記録する（#111: シーンの上書きが読み込み側に渡っていなかった）
            _record_node_transform(node, node.collapsed_key, path, value)
        return True
    if path in ("m_IsActive", "m_Name"):
        node = h.nodes.get(scope.game_objects.get(uid))  # type: ignore[arg-type]
        if node is None:
            return False
        if path == "m_IsActive":
            node.active = to_float(value, 1.0) != 0
        else:
            node.name = "" if value is None else str(value)
        return True
    component_node = scope.components.get(uid)
    if component_node is not None:
        node = h.nodes.get(component_node)
        component = node.components.get(remap(scope.instance_id, uid)) if node is not None else None
        if component is None:
            return False
        _set_property(component[1], path, value, reference)
        return True
    node = h.nodes.get(scope.renderers.get(uid))  # type: ignore[arg-type]
    renderer = node.renderer if node is not None else None
    if path == "m_Enabled":
        if renderer is None:
            return False
        renderer.enabled = to_float(value, 1.0) != 0
        return True
    if path == "m_Materials.Array.size":
        if renderer is None:
            return False
        size = int(to_float(value, len(renderer.materials)))
        if 0 <= size <= MAX_SLOTS:
            renderer.materials = (renderer.materials + [None] * size)[:size]
        return True
    match = MATERIAL_PATH.fullmatch(path)
    if match:
        if renderer is None:
            return False
        _set_slot(renderer.materials, int(match.group(1)), ref_guid(reference))
        return True
    return False


_MAX_PROPERTY_DEPTH = 8


def _set_property(body: dict, path: str, value: object, reference: object) -> None:
    """``m_Color.r`` のようなパスで、コンポーネントの中身の値を上書きする（配列の要素は対象外）。"""
    parts = path.split(".")
    if not parts or len(parts) > _MAX_PROPERTY_DEPTH or any(p in ("Array", "") or p.startswith("data[") for p in parts):
        return
    target = body
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    if isinstance(reference, UnityRef) and not reference.is_null:
        target[parts[-1]] = reference
    else:
        target[parts[-1]] = value


def _assign_scopes(h: Hierarchy, own: set[int]) -> None:
    """アセットが直接持つ Node に、最上位の祖先（scope）と、そこから自分までの上書き前の行列を付ける。"""
    for key in own:
        path = [key]
        seen = {key}
        parent = h.nodes[key].parent
        while parent is not None and parent in own and parent not in seen:
            path.append(parent)
            seen.add(parent)
            parent = h.nodes[parent].parent
        top = path[-1]
        node = h.nodes[key]
        node.scope = top
        node.scope_matrix = chain([h.nodes[k].local for k in reversed(path[:-1])])


def _remove_subtree(h: Hierarchy, key: int, children: dict[int, list[int]] | None = None) -> None:
    """``key`` の Node と子孫を消す。``children`` は ``h.children()`` の結果（続けて消すときに作り直さないよう渡せる）。"""
    if children is None:
        children = h.children()
    stack = [key]
    while stack:
        current = stack.pop()
        node = h.nodes.pop(current, None)
        if node is None:
            continue
        # 表は同じ fileID の別の Node を指していることがある（XOR の値が重なったとき。#107）ので、自分を指すものだけ消す
        if h.game_objects.get(node.game_object) == current:
            del h.game_objects[node.game_object]
        if node.renderer is not None and h.renderers.get(node.renderer_key) == current:
            del h.renderers[node.renderer_key]
        for component in node.components:  # 消した GameObject のコンポーネントへの上書きが残っていることがある（#68）
            if h.components.get(component) == current:
                del h.components[component]
        stack.extend(children.get(current, []))
