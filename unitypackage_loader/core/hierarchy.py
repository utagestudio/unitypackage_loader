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

配置（``ModelPlacement``）は 2 種類:

- モデルの PrefabInstance: そのルートの Node。
- モデルのメッシュを直接指す Renderer（FBX を展開した prefab やシーン）: Transform を直接持つアセットでの最上位の祖先
  （``Node.scope``）をモデルのルートとみなし、同じ scope・同じモデルの Renderer をまとめる。中のノードが上書きで
  動いていれば、Renderer ごとに「そのノードから逆算したルートの行列」を ``offsets`` に持つ。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from .transform import IDENTITY, Mat4, chain, inverse_affine, multiply, trs
from .unity_binary import load_documents
from .unity_yaml import UnityRef

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

# モデル（FBX 等）の PrefabInstance で、ルートの GameObject / Transform を指す fileID（FBX によらず定数。Issue #31）
MODEL_ROOT_GAME_OBJECT = 919132149155446097
MODEL_ROOT_TRANSFORM = -8679921383154817045

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
    counts: Counter = field(default_factory=Counter)  # stripped でないドキュメントのクラス ID ごとの数


def _guid(ref: object) -> str | None:
    return ref.guid.lower() if isinstance(ref, UnityRef) and ref.guid else None


def _file_id(ref: object) -> int:
    return ref.file_id if isinstance(ref, UnityRef) else 0


def _number(value: object, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _vector(value: object, default: tuple[float, ...], keys: str) -> list[float]:
    if not isinstance(value, dict):
        return list(default)
    return [_number(value.get(k), d) for k, d in zip(keys, default)]


def _targets(items: object) -> list[tuple[int, str | None]]:
    return [(r.file_id, _guid(r)) for r in (items if isinstance(items, list) else []) if isinstance(r, UnityRef)]


def parse_asset(data: str | bytes) -> RawAsset:
    """シーンか prefab を読む（テキスト / バイナリは自動判定）。"""
    raw = RawAsset()
    meshes: dict[int, str] = {}  # GameObject → MeshFilter のメッシュの GUID
    renderer_docs = []
    for doc in load_documents(data):
        body = doc.body
        if doc.stripped:
            source, instance = body.get("m_CorrespondingSourceObject"), body.get("m_PrefabInstance")
            if isinstance(source, UnityRef) and isinstance(instance, UnityRef) and instance.file_id:
                raw.aliases[doc.file_id] = (instance.file_id, source.file_id)
            continue
        raw.counts[doc.class_id] += 1
        if doc.class_id == CLASS_GAME_OBJECT:
            name = body.get("m_Name")
            raw.names[doc.file_id] = name if isinstance(name, str) else str(name or "")
            raw.active[doc.file_id] = _number(body.get("m_IsActive"), 1.0) != 0
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
            mesh = _guid(body.get("m_Mesh"))
            if mesh:
                meshes[_file_id(body.get("m_GameObject"))] = mesh
        elif doc.class_id in _RENDERER_CLASSES:
            renderer_docs.append(doc)
        elif doc.class_id == CLASS_PREFAB_INSTANCE:
            source = _guid(body.get("m_SourcePrefab"))
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
                    mods.append((target.file_id, _guid(target), path, item.get("value"), item.get("objectReference")))
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
        materials = [ref.guid if isinstance(ref, UnityRef) and ref.guid else None for ref in (mats if isinstance(mats, list) else [])]
        mesh = _guid(body.get("m_Mesh")) if doc.class_id == CLASS_SKINNED_MESH_RENDERER else meshes.get(go)
        enabled = _number(body.get("m_Enabled"), 1.0) != 0
        raw.renderers[doc.file_id] = (go, RendererInfo(mesh, materials, doc.class_id, enabled))
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
            **changes,
        )


@dataclass
class Hierarchy:
    nodes: dict[int, Node] = field(default_factory=dict)  # 親より先に子が来ることもある
    game_objects: dict[int, int] = field(default_factory=dict)  # GameObject の key → Node の key
    renderers: dict[int, int] = field(default_factory=dict)  # Renderer の key → Node の key
    counts: Counter = field(default_factory=Counter)  # 展開したドキュメントのクラス ID ごとの数（モデルの中身は含まない）
    unresolved_overrides: int = 0  # モデルの中のオブジェクトを指すため当てられなかった上書き
    missing_sources: int = 0  # 元がパッケージに無い PrefabInstance

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
    ):
        self._read = read_asset
        self._models = {g.lower(): name for g, name in model_names.items()}  # モデルの GUID → ルートの名前
        self._cache: dict[str, Hierarchy | None] = {}

    def expand_asset(self, guid: str) -> Hierarchy | None:
        return self._asset(guid.lower(), ())

    def expand_raw(self, raw: RawAsset) -> Hierarchy:
        """シーンなど、キャッシュしないアセットを展開する。"""
        return self._build(raw, ())

    def _asset(self, guid: str, stack: tuple[str, ...]) -> Hierarchy | None:
        if guid in self._cache:
            return self._cache[guid]
        if guid in stack or len(stack) >= MAX_NESTING:
            return None
        raw = self._read(guid)
        result = self._build(raw, stack + (guid,)) if raw is not None else None
        self._cache[guid] = result
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

        def resolve(file_id: int) -> int:
            alias = raw.aliases.get(file_id)
            return remap(*alias) if alias else file_id

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
        _assign_scopes(h, set(h.nodes))

        for instance in raw.instances:
            if len(h.nodes) > MAX_NODES:
                raise HierarchyError(f"hierarchy has more than {MAX_NODES} objects")
            self._insert(h, instance, resolve, stack)
        return h

    def _insert(self, h: Hierarchy, instance: _RawInstance, resolve, stack) -> None:
        source = instance.source_guid
        is_model = source in self._models
        sub = self._model(source) if is_model else self._asset(source, stack)
        if sub is None:
            h.missing_sources += 1
            return
        iid = instance.file_id
        parent = resolve(instance.parent) if instance.parent else None
        added: dict[int, int] = {}  # 元の key → 差し込んだ key
        for key, node in sub.nodes.items():
            new_key = remap(iid, key)
            added[key] = new_key
            h.nodes[new_key] = node.copy(
                key=new_key,
                parent=remap(iid, node.parent) if node.parent is not None else parent,
                game_object=remap(iid, node.game_object),
                renderer_key=remap(iid, node.renderer_key) if node.renderer else 0,
                scope=remap(iid, node.scope),
            )
        for go, key in sub.game_objects.items():
            h.game_objects[remap(iid, go)] = remap(iid, key)
        for renderer, key in sub.renderers.items():
            h.renderers[remap(iid, renderer)] = remap(iid, key)
        h.counts.update(sub.counts)
        h.unresolved_overrides += sub.unresolved_overrides
        h.missing_sources += sub.missing_sources

        def target_key(file_id: int, guid: str | None) -> int | None:
            if guid is not None and guid != source:
                return None
            return remap(iid, file_id)

        for file_id, guid in instance.removed_game_objects:
            key = target_key(file_id, guid)
            node_key = h.game_objects.get(key) if key is not None else None
            if node_key is not None and node_key in added.values():
                _remove_subtree(h, node_key)
            elif is_model:
                h.unresolved_overrides += 1
        for file_id, guid in instance.removed_components:
            key = target_key(file_id, guid)
            node_key = h.renderers.pop(key, None) if key is not None else None
            if node_key is not None and node_key in h.nodes:
                h.nodes[node_key].renderer = None
            elif is_model:
                h.unresolved_overrides += 1

        added_keys = set(added.values())
        for file_id, guid, path, value, reference in instance.modifications:
            key = target_key(file_id, guid)
            if key is None:
                continue
            if not _apply_modification(h, added_keys, key, path, value, reference) and is_model and _is_tracked(path):
                h.unresolved_overrides += 1


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
            values[index] = _number(value, values[index])
        return True
    if path in ("m_IsActive", "m_Name"):
        node_key = h.game_objects.get(key)
        if node_key is None or node_key not in added:
            return False
        if path == "m_IsActive":
            h.nodes[node_key].active = _number(value, 1.0) != 0
        else:
            h.nodes[node_key].name = "" if value is None else str(value)
        return True
    node_key = h.renderers.get(key)
    node = h.nodes.get(node_key) if node_key is not None and node_key in added else None
    renderer = node.renderer if node is not None else None
    if path == "m_Enabled":
        if renderer is None:
            return False
        renderer.enabled = _number(value, 1.0) != 0
        return True
    if path == "m_Materials.Array.size":
        if renderer is None:
            return False
        size = int(_number(value, len(renderer.materials)))
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
            renderer.materials[index] = reference.guid if isinstance(reference, UnityRef) and reference.guid else None
        return True
    return False


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


def _remove_subtree(h: Hierarchy, key: int) -> None:
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


@dataclass
class ModelPlacement:
    model_guid: str
    root: int  # モデルのルートとみなした Node の key
    world: Mat4  # Unity でのモデルのルートのワールド行列
    active: bool
    # GameObject 名（Unity の複製番号「 (N)」を外したもの）→ Renderer（展開した Renderer のみ）
    renderers: dict[str, PlacedRenderer] = field(default_factory=dict)
    offsets: dict[str, Mat4] = field(default_factory=dict)  # 中のノードが動いた Renderer の名前 → そこから逆算したルートの行列

    def signature(self) -> tuple:
        """読み込み結果を使い回せるかの判定に使う値（モデルと、名前ごとのマテリアル）。"""
        return (self.model_guid, tuple(sorted((name, tuple(r.materials)) for name, r in self.renderers.items())))


def _close(a: Mat4, b: Mat4) -> bool:
    return all(abs(x - y) <= _CLOSE * max(1.0, abs(x), abs(y)) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


def placements(h: Hierarchy, model_guids: Iterable[str]) -> list[ModelPlacement]:
    """モデルの配置を、階層に現れた順に返す。"""
    models = {g.lower() for g in model_guids}
    worlds = world_matrices(h)
    active = effective_active(h)
    result: list[ModelPlacement] = []
    by_scope: dict[tuple[int, str], ModelPlacement] = {}
    for key, node in h.nodes.items():
        if node.model_guid is not None:
            result.append(ModelPlacement(node.model_guid, key, worlds[key], active[key]))
            continue
        renderer = node.renderer
        if renderer is None or renderer.mesh_guid not in models:
            continue
        scope = node.scope if node.scope in h.nodes else key
        placement = by_scope.get((scope, renderer.mesh_guid))
        if placement is None:
            placement = ModelPlacement(renderer.mesh_guid, scope, worlds[scope], active[scope])
            by_scope[(scope, renderer.mesh_guid)] = placement
            result.append(placement)
        name = unity_base_name(node.name)
        if name in placement.renderers:
            continue  # 同じモデルに同名の GameObject があれば先のものを使う（prefab の表と同じ）
        placement.renderers[name] = PlacedRenderer(
            name, list(renderer.materials), renderer.renderer_class, active[key] and renderer.enabled
        )
        inverse = inverse_affine(node.scope_matrix)
        if inverse is not None and key != scope:
            implied = multiply(worlds[key], inverse)
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


def summarize(h: Hierarchy, model_guids: Iterable[str]) -> SceneContents:
    models = {g.lower() for g in model_guids}
    found = placements(h, models)
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
