"""シーン（.unity）や prefab の GameObject 階層を展開し、モデルの配置を求める。bpy 非依存。

展開のしかた:

- 各アセットの Transform（RectTransform を含む）・GameObject・Renderer を読み、PrefabInstance は元のアセットを
  再帰的に展開して差し込む。展開した元のオブジェクトの key は「PrefabInstance の fileID XOR 元の key」
  （上位ビットは落とす。prefab の中で Unity が使う番号と同じ）。
- 外から元のオブジェクトを指す参照（子を付ける親、上書き先）は、stripped ドキュメント
  （``m_CorrespondingSourceObject`` と ``m_PrefabInstance``）を対応表にして引く。シーンの fileID は XOR の規則に
  従わないため（Issue #48 の調査）。
- ``m_Modifications`` のうち、位置・回転・スケール（``m_LocalPosition.x`` など）、``m_IsActive``、``m_Name``、
  Renderer の ``m_Enabled`` とマテリアル（``m_Materials.Array.data[N]`` / ``.size``）を反映する。
  ``m_RemovedGameObjects`` / ``m_RemovedComponents`` で消されたものは除く。
- 元がモデル（FBX 等）の PrefabInstance は、モデルのルートを表す 1 つの Node にする。ルートの Transform の fileID は
  FBX によらず定数なので上書きを当てられるが、中のオブジェクトへの上書きは fileID を名前に結び付けられないため
  読まずに数える（Issue #31）。
- Unity 2018.2 以前の形式も読む。PrefabInstance はクラス名が ``Prefab`` で元を ``m_ParentPrefab`` に持ち、
  stripped ドキュメントの参照は ``m_PrefabParentObject`` / ``m_PrefabInternal``、モデルのルートの fileID は
  GameObject 100000 / Transform 400000。

配置（``ModelPlacement``）は 2 種類:

- モデルの PrefabInstance: そのルートの Node。
- モデルのメッシュを直接指す Renderer（FBX を展開した prefab やシーン）: モデルのルートとみなす Node を Renderer ごとに
  決め、同じルート・同じモデルの Renderer をまとめる。中のノードが上書きで動いていれば、Renderer ごとに
  「そのノードから逆算したルートの行列」を ``offsets`` に持つ。ルートの決め方は ``placements`` を参照（Issue #60）。
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import NamedTuple

from .transform import IDENTITY, Mat4, chain, inverse_affine, multiply, trs
from .unity_binary import load_documents
from .unity_yaml import UnityRef, ref_guid, to_float

CLASS_GAME_OBJECT = 1
CLASS_TRANSFORM = 4
CLASS_CAMERA = 20
CLASS_MESH_RENDERER = 23
CLASS_MESH_FILTER = 33
CLASS_LIGHT = 108
CLASS_SKINNED_MESH_RENDERER = 137
CLASS_TERRAIN = 218
CLASS_CANVAS = 223
CLASS_RECT_TRANSFORM = 224
CLASS_PREFAB_INSTANCE = 1001

_TRANSFORM_CLASSES = (CLASS_TRANSFORM, CLASS_RECT_TRANSFORM)
_RENDERER_CLASSES = (CLASS_MESH_RENDERER, CLASS_SKINNED_MESH_RENDERER)
_COMPONENT_CLASSES = (CLASS_LIGHT, CLASS_CAMERA)  # 中身をそのまま持ち、上書きを当ててから読むコンポーネント

# モデル（FBX 等）の PrefabInstance で、ルートの GameObject / Transform を指す fileID（FBX によらず定数。Issue #31）
MODEL_ROOT_GAME_OBJECT = 919132149155446097
MODEL_ROOT_TRANSFORM = -8679921383154817045
# Unity 2018.2 以前の形式（.meta の fileIDToRecycleName で 100000 / 400000 が //RootNode）でのルート
LEGACY_MODEL_ROOT_GAME_OBJECT = 100000
LEGACY_MODEL_ROOT_TRANSFORM = 400000
_LEGACY_MODEL_IDS = {LEGACY_MODEL_ROOT_GAME_OBJECT: MODEL_ROOT_GAME_OBJECT, LEGACY_MODEL_ROOT_TRANSFORM: MODEL_ROOT_TRANSFORM}

MAX_NESTING = 16  # PrefabInstance をたどる深さの上限
MAX_NODES = 200_000  # 1 つの階層に展開する Transform の上限（細工された巨大なシーンへの備え）
MAX_SLOTS = 1024
_MASK = 0x7FFF_FFFF_FFFF_FFFF
_MATERIAL_PATH = re.compile(r"m_Materials\.Array\.data\[(\d+)\]")
_TRS_PATH = re.compile(r"m_Local(Position|Rotation|Scale)\.([xyzw])")
_TRS_FIELDS = {"Position": "position", "Rotation": "rotation", "Scale": "scale"}
_AXES = {"x": 0, "y": 1, "z": 2, "w": 3}
_CLOSE = 1e-4  # 行列が同じとみなす差（ルートの行列と、中のノードから逆算した行列の比較）
_DUPLICATE_SUFFIX = re.compile(r" \(\d+\)$")  # Unity のエディタで複製したときに付く「 (12)」


def unity_base_name(name: str) -> str:
    """``Rock (12)`` → ``Rock``。Unity が複製で付ける番号を外す（モデルのオブジェクト名と照合するため）。"""
    return _DUPLICATE_SUFFIX.sub("", name)


class HierarchyError(ValueError):
    """展開の上限を超えた階層。"""


def remap(instance_id: int, source_id: int) -> int:
    """PrefabInstance で差し込んだ元のオブジェクトの key。"""
    return (instance_id ^ source_id) & _MASK


# ---------------------------------------------------------------------------
# 1 アセットの読み取り
# ---------------------------------------------------------------------------


@dataclass
class RendererInfo:
    mesh_guid: str | None
    materials: list[str | None]
    renderer_class: int
    enabled: bool = True
    mesh_file_id: int = 0  # メッシュ参照の fileID（モデルの中のどのメッシュか）


@dataclass
class _RawTransform:
    file_id: int
    game_object: int
    position: list[float]
    rotation: list[float]
    scale: list[float]
    father: int
    rect: bool


@dataclass
class _RawInstance:
    file_id: int
    source_guid: str
    parent: int
    modifications: list[tuple[int, str | None, str, object, object]]
    removed_components: list[tuple[int, str | None]]
    removed_game_objects: list[tuple[int, str | None]]


@dataclass
class RawAsset:
    """1 つの .unity / .prefab から読んだ、展開前の中身。"""

    names: dict[int, str] = field(default_factory=dict)  # GameObject の fileID → 名前
    active: dict[int, bool] = field(default_factory=dict)
    transforms: dict[int, _RawTransform] = field(default_factory=dict)
    renderers: dict[int, tuple[int, RendererInfo]] = field(default_factory=dict)  # Renderer の fileID → (GameObject, 情報)
    instances: list[_RawInstance] = field(default_factory=list)
    aliases: dict[int, tuple[int, int]] = field(default_factory=dict)  # stripped の fileID → (PrefabInstance, 元の fileID)
    # ライト・カメラの fileID → (GameObject, クラス ID, ドキュメントの中身)
    components: dict[int, tuple[int, int, dict]] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)  # stripped でないドキュメントのクラス ID ごとの数


def _file_id(ref: object) -> int:
    return ref.file_id if isinstance(ref, UnityRef) else 0


def _vector(value: object, default: tuple[float, ...], keys: str) -> list[float]:
    if not isinstance(value, dict):
        return list(default)
    return [to_float(value.get(k), d) for k, d in zip(keys, default)]


def _targets(items: object) -> list[tuple[int, str | None]]:
    return [(r.file_id, ref_guid(r)) for r in (items if isinstance(items, list) else []) if isinstance(r, UnityRef)]


def parse_asset(data: str | bytes) -> RawAsset:
    """シーンか prefab を読む（テキスト / バイナリは自動判定）。"""
    raw = RawAsset()
    meshes: dict[int, str] = {}  # GameObject → MeshFilter のメッシュの GUID
    renderer_docs = []
    for doc in load_documents(data):
        body = doc.body
        if doc.stripped:
            # 2018.3 以降は m_CorrespondingSourceObject / m_PrefabInstance、それより前は m_PrefabParentObject / m_PrefabInternal
            source = body.get("m_CorrespondingSourceObject", body.get("m_PrefabParentObject"))
            instance = body.get("m_PrefabInstance", body.get("m_PrefabInternal"))
            if isinstance(source, UnityRef) and isinstance(instance, UnityRef) and instance.file_id:
                raw.aliases[doc.file_id] = (instance.file_id, source.file_id)
            continue
        raw.counts[doc.class_id] += 1
        if doc.class_id == CLASS_GAME_OBJECT:
            name = body.get("m_Name")
            raw.names[doc.file_id] = name if isinstance(name, str) else str(name or "")
            raw.active[doc.file_id] = to_float(body.get("m_IsActive"), 1.0) != 0
        elif doc.class_id in _TRANSFORM_CLASSES:
            raw.transforms[doc.file_id] = _RawTransform(
                doc.file_id,
                _file_id(body.get("m_GameObject")),
                _vector(body.get("m_LocalPosition"), (0.0, 0.0, 0.0), "xyz"),
                _vector(body.get("m_LocalRotation"), (0.0, 0.0, 0.0, 1.0), "xyzw"),
                _vector(body.get("m_LocalScale"), (1.0, 1.0, 1.0), "xyz"),
                _file_id(body.get("m_Father")),
                doc.class_id == CLASS_RECT_TRANSFORM,
            )
        elif doc.class_id == CLASS_MESH_FILTER:
            mesh = ref_guid(body.get("m_Mesh"))
            if mesh:
                meshes[_file_id(body.get("m_GameObject"))] = (mesh, _file_id(body.get("m_Mesh")))
        elif doc.class_id in _RENDERER_CLASSES:
            renderer_docs.append(doc)
        elif doc.class_id in _COMPONENT_CLASSES:
            raw.components[doc.file_id] = (_file_id(body.get("m_GameObject")), doc.class_id, body)
        elif doc.class_id == CLASS_PREFAB_INSTANCE:
            # 2018.2 以前は m_ParentPrefab。prefab アセット自身の記録（m_IsPrefabParent: 1）は元の GUID を持たないので飛ばす
            source = ref_guid(body.get("m_SourcePrefab", body.get("m_ParentPrefab")))
            modification = body.get("m_Modification")
            modification = modification if isinstance(modification, dict) else {}
            if not source:
                continue
            mods = []
            items = modification.get("m_Modifications")
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                target, path = item.get("target"), item.get("propertyPath")
                if isinstance(target, UnityRef) and isinstance(path, str):
                    mods.append((target.file_id, ref_guid(target), path, item.get("value"), item.get("objectReference")))
            raw.instances.append(
                _RawInstance(
                    doc.file_id,
                    source,
                    _file_id(modification.get("m_TransformParent")),
                    mods,
                    _targets(modification.get("m_RemovedComponents")),
                    _targets(modification.get("m_RemovedGameObjects")),
                )
            )
    for doc in renderer_docs:
        body = doc.body
        go = _file_id(body.get("m_GameObject"))
        mats = body.get("m_Materials")
        materials = [ref_guid(ref) for ref in (mats if isinstance(mats, list) else [])]
        if doc.class_id == CLASS_SKINNED_MESH_RENDERER:
            mesh, mesh_id = ref_guid(body.get("m_Mesh")), _file_id(body.get("m_Mesh"))
        else:
            mesh, mesh_id = meshes.get(go, (None, 0))
        enabled = to_float(body.get("m_Enabled"), 1.0) != 0
        raw.renderers[doc.file_id] = (go, RendererInfo(mesh, materials, doc.class_id, enabled, mesh_id))
    return raw


# ---------------------------------------------------------------------------
# 展開
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """展開後の Transform 1 つ（GameObject と、あれば Renderer を含む）。"""

    key: int
    name: str
    position: list[float]
    rotation: list[float]
    scale: list[float]
    parent: int | None = None
    active: bool = True
    game_object: int = 0
    renderer: RendererInfo | None = None
    renderer_key: int = 0
    rect: bool = False  # RectTransform（UI）
    model_guid: str | None = None  # モデルの PrefabInstance のルートならそのモデルの GUID
    scope: int = 0  # Transform を直接持つアセットでの最上位の祖先（自分自身のこともある）
    scope_matrix: Mat4 = IDENTITY  # scope の子から自分までの、上書き前の行列の積（scope 自身の行列は含まない）
    components: dict[int, tuple[int, dict]] = field(default_factory=dict)  # ライト・カメラの key → (クラス ID, 中身)
    # モデルの PrefabInstance で、古い形式の .meta（fileIDToRecycleName）から名前を引けた中への上書き
    model_materials: dict[str, list[str | None]] = field(default_factory=dict)  # オブジェクト名 → スロット順の .mat
    model_transforms: dict[str, dict[str, list[float | None]]] = field(default_factory=dict)  # 名前 → position/rotation/scale

    @property
    def local(self) -> Mat4:
        return trs(tuple(self.position), tuple(self.rotation), tuple(self.scale))  # type: ignore[arg-type]

    def copy(self, **changes) -> Node:
        return replace(
            self,
            position=list(self.position),
            rotation=list(self.rotation),
            scale=list(self.scale),
            renderer=replace(self.renderer, materials=list(self.renderer.materials)) if self.renderer else None,
            components={k: (c, copy.deepcopy(body)) for k, (c, body) in self.components.items()},
            model_materials={k: list(v) for k, v in self.model_materials.items()},
            model_transforms=copy.deepcopy(self.model_transforms),
            **changes,
        )


@dataclass
class Hierarchy:
    nodes: dict[int, Node] = field(default_factory=dict)  # 親より先に子が来ることもある
    game_objects: dict[int, int] = field(default_factory=dict)  # GameObject の key → Node の key
    renderers: dict[int, int] = field(default_factory=dict)  # Renderer の key → Node の key
    components: dict[int, int] = field(default_factory=dict)  # ライト・カメラの key → Node の key
    counts: Counter = field(default_factory=Counter)  # 展開したドキュメントのクラス ID ごとの数（モデルの中身は含まない）
    unresolved_overrides: int = 0  # モデルの中のオブジェクトを指すため当てられなかった上書き
    unresolved_material_overrides: int = 0  # そのうちマテリアルの上書き（Models / Prefabs 単位の警告に使う）
    missing_sources: int = 0  # 元がパッケージに無い PrefabInstance
    # MAX_NESTING で打ち切った PrefabInstance を含む（キャッシュした深さより浅い位置から使うときは展開し直す）
    depth_truncated: bool = False

    def children(self) -> dict[int, list[int]]:
        result: dict[int, list[int]] = {}
        for key, node in self.nodes.items():
            if node.parent is not None:
                result.setdefault(node.parent, []).append(key)
        return result


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
        root = Node(
            key=MODEL_ROOT_TRANSFORM & _MASK,
            name=self._models[guid],
            position=[0.0, 0.0, 0.0],
            rotation=[0.0, 0.0, 0.0, 1.0],
            scale=[1.0, 1.0, 1.0],
            game_object=MODEL_ROOT_GAME_OBJECT,
            model_guid=guid,
        )
        root.scope = root.key
        return Hierarchy({root.key: root}, {MODEL_ROOT_GAME_OBJECT: root.key})

    def _build(self, raw: RawAsset, stack: tuple[str, ...]) -> Hierarchy:
        h = Hierarchy(counts=Counter(raw.counts))
        model_instances = {i.file_id for i in raw.instances if i.source_guid in self._models}

        def resolve(file_id: int) -> int:
            alias = raw.aliases.get(file_id)
            if not alias:
                return file_id
            instance_id, source_id = alias
            if instance_id in model_instances:
                source_id = _LEGACY_MODEL_IDS.get(source_id, source_id)
            return remap(instance_id, source_id)

        for t in raw.transforms.values():
            node = Node(
                key=t.file_id,
                name=raw.names.get(t.game_object, ""),
                position=t.position,
                rotation=t.rotation,
                scale=t.scale,
                parent=resolve(t.father) if t.father else None,
                active=raw.active.get(t.game_object, True),
                game_object=t.game_object,
                rect=t.rect,
            )
            h.nodes[node.key] = node
            h.game_objects[t.game_object] = node.key
        for renderer_id, (go, info) in raw.renderers.items():
            key = h.game_objects.get(go)
            if key is not None:
                h.nodes[key].renderer = info
                h.nodes[key].renderer_key = renderer_id
                h.renderers[renderer_id] = key
        for component_id, (go, class_id, body) in raw.components.items():
            key = h.game_objects.get(go)
            if key is not None:
                h.nodes[key].components[component_id] = (class_id, copy.deepcopy(body))
                h.components[component_id] = key
        _assign_scopes(h, set(h.nodes))

        for instance in raw.instances:
            if len(h.nodes) > MAX_NODES:
                raise HierarchyError(f"hierarchy has more than {MAX_NODES} objects")
            self._insert(h, instance, resolve, stack)
        return h

    def _insert(self, h: Hierarchy, instance: _RawInstance, resolve, stack) -> None:
        source = instance.source_guid
        is_model = source in self._models
        if not is_model and source not in stack and len(stack) >= MAX_NESTING:
            h.depth_truncated = True
        sub = self._model(source) if is_model else self._asset(source, stack)
        if sub is None:
            h.missing_sources += 1
            return
        h.depth_truncated = h.depth_truncated or sub.depth_truncated
        iid = instance.file_id
        parent = resolve(instance.parent) if instance.parent else None
        added: dict[int, int] = {}  # 元の key → 差し込んだ key
        for key, node in sub.nodes.items():
            new_key = remap(iid, key)
            added[key] = new_key
            copied = node.copy(
                key=new_key,
                parent=remap(iid, node.parent) if node.parent is not None else parent,
                game_object=remap(iid, node.game_object),
                renderer_key=remap(iid, node.renderer_key) if node.renderer else 0,
                scope=remap(iid, node.scope),
            )
            copied.components = {remap(iid, k): value for k, value in copied.components.items()}
            h.nodes[new_key] = copied
        for go, key in sub.game_objects.items():
            h.game_objects[remap(iid, go)] = remap(iid, key)
        for renderer, key in sub.renderers.items():
            h.renderers[remap(iid, renderer)] = remap(iid, key)
        for component, key in sub.components.items():
            h.components[remap(iid, component)] = remap(iid, key)
        h.counts.update(sub.counts)
        h.unresolved_overrides += sub.unresolved_overrides
        h.unresolved_material_overrides += sub.unresolved_material_overrides
        h.missing_sources += sub.missing_sources

        def target_key(file_id: int, guid: str | None) -> int | None:
            if guid is not None and guid != source:
                return None
            if is_model:
                file_id = _LEGACY_MODEL_IDS.get(file_id, file_id)
            return remap(iid, file_id)

        added_keys = set(added.values())
        children: dict[int, list[int]] | None = None  # 消すたびに全 Node を走査し直さないよう、初めて消すときに 1 回だけ作る
        for file_id, guid in instance.removed_game_objects:
            key = target_key(file_id, guid)
            node_key = h.game_objects.get(key) if key is not None else None
            if node_key is not None and node_key in added_keys:
                if children is None:
                    children = h.children()
                _remove_subtree(h, node_key, children)
            elif is_model:
                h.unresolved_overrides += 1
        for file_id, guid in instance.removed_components:
            key = target_key(file_id, guid)
            node_key = h.renderers.pop(key, None) if key is not None else None
            component_node = h.components.pop(key, None) if key is not None else None
            if node_key is not None and node_key in h.nodes:
                h.nodes[node_key].renderer = None
            elif component_node is not None and component_node in h.nodes:
                h.nodes[component_node].components.pop(key, None)
            elif is_model:
                h.unresolved_overrides += 1

        root_key =remap(iid, MODEL_ROOT_TRANSFORM & _MASK) if is_model else None
        table = self._recycle.get(source, {}) if is_model else {}
        # 1 メッシュの FBX では、モデルのルートの Transform の値が FBX のノードの変換なので、ルートへの位置・回転・
        # スケールの上書きは「どの成分を上書きしたか」も記録する（読み込み側でノードの値に当てるため。#99）
        collapsed = self._collapsed.get(source) if is_model else None
        for file_id, guid, path, value, reference in instance.modifications:
            key = target_key(file_id, guid)
            if key is None:
                continue
            if collapsed is not None and key == root_key and root_key in h.nodes:
                _record_node_transform(h.nodes[root_key], collapsed, path, value)
            if _apply_modification(h, added_keys, key, path, value, reference) or not is_model or not _is_tracked(path):
                continue
            if root_key in h.nodes and _apply_named_model_override(h.nodes[root_key], table, file_id, path, value, reference):
                continue
            h.unresolved_overrides += 1
            if _MATERIAL_PATH.fullmatch(path) or path == "m_Materials.Array.size":
                h.unresolved_material_overrides += 1


_CLASS_PREFIX_TRANSFORM = CLASS_TRANSFORM  # 古い形式の fileID は「クラス ID × 100000 + 通し番号」
_ROOT_NODE_NAME = "//RootNode"


def _model_object_name(table: dict[int, str], file_id: int) -> str | None:
    """古い形式の fileID をモデルのオブジェクト名にする。//RootNode の Renderer は、そのメッシュの名前にする。"""
    name = table.get(file_id)
    if name is None:
        return None
    if name != _ROOT_NODE_NAME:
        return name
    # 1 メッシュの FBX では Renderer がルートに載り、Blender ではメッシュ名のオブジェクトになる
    meshes = [n for k, n in sorted(table.items()) if k // 100000 == 43 and n != _ROOT_NODE_NAME]
    return meshes[0] if len(meshes) == 1 else None


def _record_node_transform(root: Node, name: str, path: str, value: object) -> bool:
    """モデルの中のノードへの位置・回転・スケールの上書きを、名前付きで配置のルートに記録する。

    上書きの無い成分は ``None`` のままにする（読み込み側で、Blender のオブジェクトから逆算した元の値を使う）。
    """
    match = _TRS_PATH.fullmatch(path)
    if match is None:
        return False
    field_name = _TRS_FIELDS[match.group(1)]
    size = 4 if field_name == "rotation" else 3
    values = root.model_transforms.setdefault(name, {}).setdefault(field_name, [None] * size)
    index = _AXES[match.group(2)]
    if index < size:
        number = to_float(value, float("nan"))
        values[index] = None if math.isnan(number) else number
    return True


def _apply_named_model_override(root: Node, table: dict[int, str], file_id: int, path: str, value: object, reference: object) -> bool:
    """モデルの中への上書きを、表で引いた名前付きで配置のルートに記録する。名前を引けなければ False。"""
    if not table:
        return False
    class_id = file_id // 100000
    name = _model_object_name(table, file_id)
    if name is None:
        return False
    if class_id == _CLASS_PREFIX_TRANSFORM and _record_node_transform(root, name, path, value):
        return True
    material = _MATERIAL_PATH.fullmatch(path)
    if material and class_id in (CLASS_MESH_RENDERER, CLASS_SKINNED_MESH_RENDERER):
        index = int(material.group(1))
        if index >= MAX_SLOTS:
            return False
        slots = root.model_materials.setdefault(name, [])
        if index >= len(slots):
            slots.extend([None] * (index + 1 - len(slots)))
        slots[index] = ref_guid(reference)
        return True
    return False


def _is_tracked(path: str) -> bool:
    return bool(
        _TRS_PATH.fullmatch(path)
        or _MATERIAL_PATH.fullmatch(path)
        or path in ("m_Materials.Array.size", "m_IsActive", "m_Enabled")
    )


def _apply_modification(h: Hierarchy, added: set[int], key: int, path: str, value: object, reference: object) -> bool:
    """上書きを 1 つ当てる。当てる先が差し込んだオブジェクトに見つからなければ False。"""
    match = _TRS_PATH.fullmatch(path)
    if match:
        node = h.nodes.get(key)
        if node is None or key not in added:
            return False
        values = getattr(node, _TRS_FIELDS[match.group(1)])
        index = _AXES[match.group(2)]
        if index < len(values):
            values[index] = to_float(value, values[index])
        return True
    if path in ("m_IsActive", "m_Name"):
        node_key = h.game_objects.get(key)
        if node_key is None or node_key not in added:
            return False
        if path == "m_IsActive":
            h.nodes[node_key].active = to_float(value, 1.0) != 0
        else:
            h.nodes[node_key].name = "" if value is None else str(value)
        return True
    component_node = h.components.get(key)
    if component_node is not None:
        if component_node not in added or component_node not in h.nodes:
            return False
        _set_property(h.nodes[component_node].components[key][1], path, value, reference)
        return True
    node_key = h.renderers.get(key)
    node = h.nodes.get(node_key) if node_key is not None and node_key in added else None
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
    match = _MATERIAL_PATH.fullmatch(path)
    if match:
        if renderer is None:
            return False
        index = int(match.group(1))
        if index < MAX_SLOTS:
            if index >= len(renderer.materials):
                renderer.materials.extend([None] * (index + 1 - len(renderer.materials)))
            renderer.materials[index] = ref_guid(reference)
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


@dataclass
class PlacedComponent:
    """シーンに置かれたライト・カメラ（上書き済みの中身と、GameObject のワールド行列）。"""

    key: int
    node: int
    name: str
    class_id: int
    body: dict
    world: Mat4
    active: bool  # GameObject がアクティブで、コンポーネントが有効


def components(h: Hierarchy, class_ids: Iterable[int] = _COMPONENT_CLASSES) -> list[PlacedComponent]:
    """階層に現れた順のライト・カメラ。"""
    wanted = set(class_ids)
    worlds = world_matrices(h)
    active = effective_active(h)
    result = []
    for node_key, node in h.nodes.items():
        for key, (class_id, body) in node.components.items():
            if class_id not in wanted:
                continue
            enabled = to_float(body.get("m_Enabled"), 1.0) != 0
            result.append(PlacedComponent(key, node_key, node.name, class_id, body, worlds[node_key], active[node_key] and enabled))
    return result


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
        h.game_objects.pop(node.game_object, None)
        if node.renderer is not None:
            h.renderers.pop(node.renderer_key, None)
        for component in node.components:  # 消した GameObject のライト・カメラへの上書きが残っていることがある（#68）
            h.components.pop(component, None)
        stack.extend(children.get(current, []))


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def world_matrices(h: Hierarchy) -> dict[int, Mat4]:
    """全 Node のワールド行列。親が見つからない・循環している Node は、そこを最上位として扱う。"""
    result: dict[int, Mat4] = {}
    for start in h.nodes:
        if start in result:
            continue
        path = []
        seen = set()
        key: int | None = start
        while key is not None and key in h.nodes and key not in result and key not in seen:
            path.append(key)
            seen.add(key)
            key = h.nodes[key].parent
        base = result.get(key, IDENTITY) if key is not None else IDENTITY
        for k in reversed(path):
            base = multiply(base, h.nodes[k].local)
            result[k] = base
    return result


def effective_active(h: Hierarchy) -> dict[int, bool]:
    """祖先を含めてアクティブか（Unity の activeInHierarchy）。"""
    result: dict[int, bool] = {}
    for start in h.nodes:
        path = []
        seen = set()
        key: int | None = start
        while key is not None and key in h.nodes and key not in result and key not in seen:
            path.append(key)
            seen.add(key)
            key = h.nodes[key].parent
        state = result.get(key, True) if key is not None else True
        for k in reversed(path):
            state = state and h.nodes[k].active
            result[k] = state
    return result


@dataclass
class PlacedRenderer:
    name: str
    materials: list[str | None]
    renderer_class: int
    visible: bool  # GameObject がアクティブで Renderer が有効
    mesh_file_id: int = 0  # メッシュ参照の fileID（表で名前を引けなかったとき、読み込み側がハッシュで照合する）


@dataclass
class ModelPlacement:
    model_guid: str
    root: int  # モデルのルートとみなした Node の key
    world: Mat4  # Unity でのモデルのルートのワールド行列
    active: bool
    # GameObject 名（Unity の複製番号「 (N)」を外したもの）→ Renderer（展開した Renderer のみ）
    renderers: dict[str, PlacedRenderer] = field(default_factory=dict)
    offsets: dict[str, Mat4] = field(default_factory=dict)  # 中のノードが動いた Renderer の名前 → そこから逆算したルートの行列
    # モデルの PrefabInstance の中のノードへの上書き（古い形式の .meta で名前を引けたもの）。名前 → position/rotation/scale
    node_transforms: dict[str, dict[str, list[float | None]]] = field(default_factory=dict)
    # 1 メッシュの FBX をモデルの PrefabInstance で置いた配置での、畳まれたノード（Blender のオブジェクト）の名前。
    # このとき ``world`` は上書きの無い成分に FBX のノードの値が入っていないので、読み込み側が
    # ``node_transforms[root_node]`` とオブジェクトの元の行列から組み立て直す（#99）
    root_node: str = ""

    def signature(self) -> tuple:
        """読み込み結果を使い回せるかの判定に使う値（モデルと、名前ごとのマテリアル）。"""
        return (self.model_guid, tuple(sorted((name, tuple(r.materials)) for name, r in self.renderers.items())))


def _close(a: Mat4, b: Mat4) -> bool:
    return all(abs(x - y) <= _CLOSE * max(1.0, abs(x), abs(y)) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


_CLASS_PREFIX_MESH = 43


def _node_names(table: dict[int, str]) -> set[str] | None:
    """.meta の表にある、ルート以外の GameObject の名前（FBX のノード名）。

    空の集合は 1 メッシュの FBX（GameObject はルートだけで、メッシュも 1 つ）。ノードが分からない表は None
    （GameObject の行が無い表や、古い番号を引き継いだ ``internalIDToNameTable`` で一部の行しか無いもの）。
    """
    game_objects = {n for k, n in table.items() if k // 100000 == CLASS_GAME_OBJECT}
    names = game_objects - {_ROOT_NODE_NAME}
    if names:
        return names
    meshes = sum(1 for k in table if k // 100000 == _CLASS_PREFIX_MESH)
    return set() if game_objects and meshes <= 1 else None


def collapsed_root_name(table: dict[int, str]) -> str | None:
    """1 メッシュの FBX で、Unity が ``//RootNode`` に畳んだノード（Blender ではメッシュ名のオブジェクト）の名前。

    表が無いモデルと、ノードが複数あるモデルは None。この形の FBX では、モデルのルートの Transform が
    FBX のノードの変換を持ち、メッシュの頂点はノード空間のままになる（Issue #98 / #99。Unity 6000.6.0f1 で確認）。
    """
    if _node_names(table) != set():
        return None
    root_id = next((k for k in table if k // 100000 == CLASS_GAME_OBJECT), 0)
    return _model_object_name(table, root_id)


def _fbx_node_name(node: Node, model_guid: str, table: dict[int, str], names: set[str]) -> str | None:
    """Node が FBX のどのノードか。GameObject 名で引き、名前を変えてあれば Renderer のメッシュ名で引く。

    1 メッシュの FBX（``names`` が空）では、Unity が ``//RootNode`` に畳んだノードそのものなので、表から引いた
    メッシュ名を使う（Blender では FBX のノード名のオブジェクトになり、この形の FBX ではメッシュ名と同じ。#98）。
    """
    base = unity_base_name(node.name)
    if base in names:
        return base
    renderer = node.renderer
    if renderer is not None and renderer.mesh_guid == model_guid:
        mesh_name = table.get(renderer.mesh_file_id)
        if mesh_name in names or (not names and mesh_name and mesh_name != _ROOT_NODE_NAME):
            return mesh_name
    return None


def _root_candidates(
    h: Hierarchy, key: int, model_guid: str, table: dict[int, str], names: set[str] | None
) -> tuple[int, int]:
    """Renderer の (まとめる先の候補, 候補を使わないときのルート) を返す。

    ノードが分からなければ、最上位の祖先（scope）が候補。1 メッシュの FBX は Renderer の GameObject 自身がモデルの
    ルート。それ以外は、FBX のノードが続く間は親をたどり（scope は越えない）、その 1 つ上を候補にする。
    """
    scope = h.nodes[key].scope if h.nodes[key].scope in h.nodes else key
    if names is None:
        return scope, key
    if not names:
        return key, key
    current = key
    while current != scope:
        parent = h.nodes[current].parent
        if parent is None or parent not in h.nodes:
            break
        if parent != scope and _fbx_node_name(h.nodes[parent], model_guid, table, names) is not None:
            current = parent
            continue
        return parent, current
    return current, current


def _is_foreign(node: Node, model_guid: str, models: set[str]) -> bool:
    """別のモデルの PrefabInstance か、別のモデルのメッシュを指す Renderer か。"""
    if node.model_guid is not None:
        return True
    renderer = node.renderer
    return renderer is not None and renderer.mesh_guid in models and renderer.mesh_guid != model_guid


_IDENTITY_NODE = {"position": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]}


ModelNames = Mapping[str, str] | Iterable[str]  # モデルの GUID → ルートの名前（名前を使わなければ GUID の並びでもよい）


def _model_names(models: ModelNames) -> dict[str, str | None]:
    """モデルの GUID（小文字）→ ルートの名前。GUID の並びを渡されたら名前は None。"""
    if isinstance(models, Mapping):
        return {g.lower(): n for g, n in models.items()}
    return {g.lower(): None for g in models}


class _RendererRow(NamedTuple):
    """``placements`` の 1 回目で決める、Renderer 1 つの名前とルートの候補。"""

    key: int  # Renderer を持つ Node
    name: str
    candidate: int  # まとめる先の候補
    own: int  # 候補を使わないときのルート


def placements(
    h: Hierarchy, model_guids: ModelNames, name_tables: dict[str, dict[int, str]] | None = None
) -> list[ModelPlacement]:
    """モデルの配置を、階層に現れた順に返す。

    ``model_guids`` にモデルの GUID → ルートの名前の辞書を渡すと、下の「候補の名前がモデルの名前と同じか」に使う。

    ``name_tables``（モデルの GUID → .meta の表の fileID → 名前）があれば、Renderer の名前にはメッシュ参照から引いた
    メッシュの名前（= FBX のノード名、Blender のオブジェクト名）を使う。展開した prefab で GameObject の名前を
    変えていても、モデルのオブジェクトと照合できる（#58）。引けなければ GameObject の名前。

    Renderer をまとめるモデルのルート（Issue #60）: 表の無いモデルは最上位の祖先（scope）。表のあるモデルは
    ``_root_candidates`` の候補にまとめるが、次のときは候補をモデルのルートとみなさず、Renderer の GameObject
    （たどった FBX のノード）自身をルートにする。シーンの共通の親の下に、FBX から切り離した小物を複製して並べた場合。

    - まとめると同じメッシュの Renderer が重なる（複製を並べた入れ物）。
    - 候補の直下に別のモデルがあり、候補の名前がモデルの名前と違う（部屋のような入れ物）。

    FBX の中のノードをルートにした配置は、``node_transforms`` でそのノードを単位行列にする。Unity の GameObject の
    行列がノードの元の変換を含んでいるので、Blender のオブジェクトの元の変換を掛けないようにするため。
    1 メッシュの FBX（Renderer の GameObject 自身がルート）も同じで、Unity はノードを ``//RootNode`` に畳んで
    その変換をモデルのルートの Transform に載せ、メッシュの頂点はノード空間のままにするため、メッシュを直接指す
    Renderer にはノードの変換が掛からない（Unity 6000.6.0f1 で確認。Issue #98）。
    """
    root_names = _model_names(model_guids)
    models = set(root_names)
    tables = {g.lower(): table for g, table in (name_tables or {}).items()}
    node_names = {g: names for g, table in tables.items() if (names := _node_names(table)) is not None}
    # 表に無いメッシュを指す Renderer があれば、その表は一部の行しか無い（古い番号を引き継いだ表）のでノードは分からない
    for node in h.nodes.values():
        renderer = node.renderer
        if (
            renderer is not None and renderer.mesh_guid in node_names
            and renderer.mesh_file_id and renderer.mesh_file_id not in tables[renderer.mesh_guid]
        ):
            del node_names[renderer.mesh_guid]
    worlds = world_matrices(h)
    active = effective_active(h)
    children = h.children()

    # 1 回目: Renderer ごとに名前とルートの候補を決める。モデルの PrefabInstance はそのまま配置にする
    items: list[ModelPlacement | _RendererRow] = []
    groups: dict[tuple[int, str], list[_RendererRow]] = {}
    for key, node in h.nodes.items():
        if node.model_guid is not None:
            placement = ModelPlacement(node.model_guid, key, worlds[key], active[key])
            for name, slots in node.model_materials.items():
                placement.renderers[name] = PlacedRenderer(name, list(slots), CLASS_MESH_RENDERER, True)
            placement.node_transforms = copy.deepcopy(node.model_transforms)
            # 1 メッシュの FBX は、ルートの Transform が FBX のノードの変換なので、読み込み側で組み立て直す（#99）
            collapsed = collapsed_root_name(tables.get(node.model_guid, {}))
            if collapsed is not None:
                placement.root_node = collapsed
                placement.node_transforms.setdefault(collapsed, {})
            items.append(placement)
            continue
        renderer = node.renderer
        if renderer is None or renderer.mesh_guid not in models:
            continue
        # FBX のノード名と同じ GameObject 名を優先する（複数のノードが 1 つのメッシュを共有する FBX もあるため）
        names = node_names.get(renderer.mesh_guid)
        mesh_name = tables.get(renderer.mesh_guid, {}).get(renderer.mesh_file_id)
        name = unity_base_name(node.name)
        if not (names and name in names) and mesh_name and mesh_name != _ROOT_NODE_NAME:
            name = mesh_name
        candidate, own = _root_candidates(h, key, renderer.mesh_guid, tables.get(renderer.mesh_guid, {}), names)
        item = _RendererRow(key, name, candidate, own)
        groups.setdefault((candidate, renderer.mesh_guid), []).append(item)
        items.append(item)

    # 候補をモデルのルートとみなさないグループ
    dissolved: set[tuple[int, str]] = set()
    for (candidate, guid), rows in groups.items():
        if guid not in node_names or all(own == candidate for _, _, _, own in rows):
            continue
        repeated = len({name for _, name, _, _ in rows}) < len(rows)
        # 部品が 1 つだけのときに限る（照明の prefab にろうそくを足したような、モデルのルートに別のモデルを置いたものは分けない）
        mixed = len(rows) == 1 and unity_base_name(h.nodes[candidate].name) != root_names.get(guid) and any(
            _is_foreign(h.nodes[child], guid, models) for child in children.get(candidate, []) if child in h.nodes
        )
        if repeated or mixed:
            dissolved.add((candidate, guid))

    # 2 回目: 配置を作る
    result: list[ModelPlacement] = []
    by_root: dict[tuple[int, str], ModelPlacement] = {}
    for item in items:
        if isinstance(item, ModelPlacement):
            result.append(item)
            continue
        key, name, candidate, own = item
        node = h.nodes[key]
        renderer = node.renderer
        guid = renderer.mesh_guid
        root = own if (candidate, guid) in dissolved else candidate
        placement = by_root.get((root, guid))
        if placement is None:
            placement = ModelPlacement(guid, root, worlds[root], active[root])
            names = node_names.get(guid)
            if names is not None and root == own:
                label = _fbx_node_name(h.nodes[root], guid, tables.get(guid, {}), names)
                if label is not None:  # FBX の中のノードをルートにした
                    placement.node_transforms[label] = copy.deepcopy(_IDENTITY_NODE)
            by_root[(root, guid)] = placement
            result.append(placement)
        if name in placement.renderers:
            continue  # 同じモデルに同名の GameObject があれば先のものを使う（prefab の表と同じ）
        placement.renderers[name] = PlacedRenderer(
            name, list(renderer.materials), renderer.renderer_class, active[key] and renderer.enabled, renderer.mesh_file_id
        )
        # ルートからこの Node までの、上書き前の行列の積で逆算する
        inverse = inverse_affine(node.scope_matrix)
        if inverse is not None and key != root:
            implied = multiply(multiply(worlds[key], inverse), h.nodes[root].scope_matrix)
            if not _close(implied, placement.world):
                placement.offsets[name] = implied
    return result


@dataclass
class SceneContents:
    """ダイアログに出すシーンの概要。"""

    placements: list[ModelPlacement]
    lights: int = 0
    cameras: int = 0
    ui_elements: int = 0
    other_renderers: int = 0  # パッケージ外のメッシュ（Unity 組み込みの Cube など）を指す Renderer
    unresolved_overrides: int = 0
    missing_sources: int = 0

    @property
    def model_guids(self) -> list[str]:
        seen: dict[str, None] = {}
        for p in self.placements:
            seen.setdefault(p.model_guid, None)
        return list(seen)


def summarize(h: Hierarchy, model_guids: ModelNames, name_tables: dict[str, dict[int, str]] | None = None) -> SceneContents:
    """シーンの概要。引数は ``placements`` と同じ。"""
    names = _model_names(model_guids)
    models = set(names)
    found = placements(h, names, name_tables)
    other = sum(
        1 for n in h.nodes.values() if n.renderer is not None and n.model_guid is None and n.renderer.mesh_guid not in models
    )
    return SceneContents(
        found,
        lights=h.counts[CLASS_LIGHT],
        cameras=h.counts[CLASS_CAMERA],
        ui_elements=h.counts[CLASS_RECT_TRANSFORM],
        other_renderers=other,
        unresolved_overrides=h.unresolved_overrides,
        missing_sources=h.missing_sources,
    )
