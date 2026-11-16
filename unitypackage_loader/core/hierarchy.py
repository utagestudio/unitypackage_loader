"""シーン（.unity）や prefab の GameObject 階層の読み取りと、展開した階層のデータ。bpy 非依存。

1 つのアセットを読む ``parse_asset``（``RawAsset``）、展開した階層の ``Node`` / ``Hierarchy``、祖先をたどる計算
（``world_matrices`` / ``effective_active`` / ``components``）、古い形式の .meta の表から名前を引く関数を置く。
PrefabInstance を差し込んで階層を展開するのは ``expander.py``、モデルの配置を求めるのは ``placement.py``
（以前はこのファイル 1 つ、約 1,500 行だった。#198）。
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from .decals import is_decal_projector
from .transform import IDENTITY, Mat4, multiply, trs
from .unity_binary import load_documents
from .unity_yaml import UnityRef, ref_guid, to_float

CLASS_GAME_OBJECT = 1
CLASS_TRANSFORM = 4
CLASS_CAMERA = 20
CLASS_MESH_RENDERER = 23
CLASS_MESH_FILTER = 33
CLASS_MESH = 43  # メッシュのアセット（古い形式の .meta の表に、モデルの中のメッシュとして現れる）
CLASS_MESH_COLLIDER = 64
CLASS_LIGHT = 108
CLASS_MONO_BEHAVIOUR = 114  # スクリプトのコンポーネント。DecalProjector の形をしたものだけを持つ（#165）
CLASS_SKINNED_MESH_RENDERER = 137
CLASS_LOD_GROUP = 205
CLASS_TERRAIN = 218
CLASS_CANVAS = 223
CLASS_RECT_TRANSFORM = 224
CLASS_PREFAB_INSTANCE = 1001

_TRANSFORM_CLASSES = (CLASS_TRANSFORM, CLASS_RECT_TRANSFORM)
RENDERER_CLASSES = (CLASS_MESH_RENDERER, CLASS_SKINNED_MESH_RENDERER)
# 中身をそのまま持ち、上書きを当ててから読むコンポーネント。ほかに、DecalProjector の形をした MonoBehaviour（#165）と
# Terrain（#205）も持つ（``parse_asset``）。``components()`` の既定はライト・カメラだけ
COMPONENT_CLASSES = (CLASS_LIGHT, CLASS_CAMERA)

# モデル（FBX 等）の PrefabInstance で、ルートの GameObject / Transform を指す fileID（FBX によらず定数。Issue #31）。
# .meta の fileIdsGeneration が 2 のモデルでの値（``unity_ids.hierarchy_file_id`` のパスが空のもの）
MODEL_ROOT_GAME_OBJECT = 919132149155446097
MODEL_ROOT_TRANSFORM = -8679921383154817045
# fileIdsGeneration が 1（キーの無い .meta を含む）のモデルでのルート（``unity_ids.named_file_id`` の //RootNode）。
# Unity 2019.3〜2019.4 ごろに作られたパッケージがこの値で書いている（UnityJapanOffice で確認）
NAMED_MODEL_ROOT_GAME_OBJECT = -927199367670048503
NAMED_MODEL_ROOT_TRANSFORM = -4216859302048453862
# Unity 2018.2 以前の形式（.meta の fileIDToRecycleName で 100000 / 400000 が //RootNode）でのルート
LEGACY_MODEL_ROOT_GAME_OBJECT = 100000
LEGACY_MODEL_ROOT_TRANSFORM = 400000
_LEGACY_ID_STRIDE = 100000  # 古い形式の fileID は「クラス ID × 100000 + 通し番号」
LEGACY_MODEL_IDS = {LEGACY_MODEL_ROOT_GAME_OBJECT: MODEL_ROOT_GAME_OBJECT, LEGACY_MODEL_ROOT_TRANSFORM: MODEL_ROOT_TRANSFORM}

MAX_NESTING = 16  # PrefabInstance をたどる深さの上限
MAX_NODES = 200_000  # 1 つの階層に展開する Transform の上限（細工された巨大なシーンへの備え）
MAX_SLOTS = 1024
ID_MASK = 0x7FFF_FFFF_FFFF_FFFF
MODEL_ROOT_UIDS = {
    MODEL_ROOT_GAME_OBJECT & ID_MASK, MODEL_ROOT_TRANSFORM & ID_MASK,
    NAMED_MODEL_ROOT_GAME_OBJECT & ID_MASK, NAMED_MODEL_ROOT_TRANSFORM & ID_MASK,
}
# モデルの中への上書きのうち、表で名前を引けなかった Renderer へのマテリアルの上書きを記録する名前の接頭辞。後ろに対象の
# fileID（上位ビットを落とした値）が続く。読み込み側が、読み込んだオブジェクトの名前から計算した fileID と突き合わせて
# オブジェクトの名前に付け替える（``prefab.resolve_model_renderers``。#31 / #109）。Unity・Blender の名前には現れない
_PENDING_RENDERER = "\x00renderer:"


def pending_renderer(uid: int) -> str:
    """名前を引けなかった Renderer（fileID の上位ビットを落とした値）の行の名前。"""
    return f"{_PENDING_RENDERER}{uid}"


def pending_renderer_id(name: str) -> int | None:
    """``pending_renderer`` の名前なら、その fileID。ふつうの名前なら None。"""
    if not name.startswith(_PENDING_RENDERER):
        return None
    return int(name[len(_PENDING_RENDERER):])


# 同じく、名前を引けなかった GameObject への表示の上書き・削除を記録する名前の接頭辞（#172）
_PENDING_OBJECT = "\x00object:"


def pending_object(uid: int) -> str:
    """名前を引けなかった GameObject（fileID の上位ビットを落とした値）の行の名前。"""
    return f"{_PENDING_OBJECT}{uid}"


def pending_object_id(name: str) -> int | None:
    """``pending_object`` の名前なら、その fileID。ふつうの名前なら None。"""
    if not name.startswith(_PENDING_OBJECT):
        return None
    return int(name[len(_PENDING_OBJECT):])


@dataclass
class PartState:
    """モデルの PrefabInstance の中のオブジェクトへの、表示の上書きと削除（#172）。

    key（``Node.model_parts`` / ``ModelPlacement.parts``）は、古い形式の .meta の表で引いた名前か、
    ``pending_object`` / ``pending_renderer`` の名前。古い形式では GameObject と Renderer が同じ名前になるので、
    どちらへのものかをフィールドで分ける。
    """

    active: bool | None = None  # GameObject の m_IsActive（子孫も含めて表示しない）
    enabled: bool | None = None  # Renderer の m_Enabled
    removed_object: bool = False  # m_RemovedGameObjects で消した GameObject（子孫も含めて Unity に無い）
    removed_renderer: bool = False  # m_RemovedComponents で消した Renderer

    @property
    def removed(self) -> bool:
        return self.removed_object or self.removed_renderer


# 表の無いモデルの PrefabInstance で、ルートの Transform への位置・回転・スケールの上書きを記録する名前。読み込み側が、
# モデルのオブジェクトが 1 つだけ（Unity がノードを //RootNode に畳む形）ならそのノードへの上書きとして組み立て直す（#111）
SOLE_NODE = "\x00sole-node"
MATERIAL_PATH = re.compile(r"m_Materials\.Array\.data\[(\d+)\]")
TRS_PATH = re.compile(r"m_Local(Position|Rotation|Scale)\.([xyzw])")
TRS_FIELDS = {"Position": "position", "Rotation": "rotation", "Scale": "scale"}
AXES = {"x": 0, "y": 1, "z": 2, "w": 3}
CLOSE = 1e-4  # 行列が同じとみなす差（ルートの行列と、中のノードから逆算した行列の比較）
_DUPLICATE_SUFFIX = re.compile(r" \(\d+\)$")  # Unity のエディタで複製したときに付く「 (12)」


def unity_base_name(name: str) -> str:
    """``Rock (12)`` → ``Rock``。Unity が複製で付ける番号を外す（モデルのオブジェクト名と照合するため）。"""
    return _DUPLICATE_SUFFIX.sub("", name)


class HierarchyError(ValueError):
    """展開の上限を超えた階層。"""


def remap(instance_id: int, source_id: int) -> int:
    """PrefabInstance で差し込んだ元のオブジェクトの、差し込んだ先のアセットでの fileID。"""
    return (instance_id ^ source_id) & ID_MASK


def file_uid(file_id: int) -> int:
    """表の key にする fileID（``remap`` の結果と揃えて上位ビットを落とす）。"""
    return file_id & ID_MASK


def legacy_class(file_id: int) -> int:
    """古い形式の .meta の表（fileIDToRecycleName）の fileID が表すオブジェクトのクラス ID。"""
    return file_id // _LEGACY_ID_STRIDE


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
    lod_level: int = 0  # LODGroup の段（0 = もっとも細かい LOD0、または LODGroup が無い）


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
    # コンポーネント（ライト・カメラ・デカール・Terrain）の fileID → (GameObject, クラス ID, ドキュメントの中身)
    components: dict[int, tuple[int, int, dict]] = field(default_factory=dict)
    lod_groups: list[list[list[int]]] = field(default_factory=list)  # LODGroup ごとの、段の順に並べた Renderer の fileID
    collider_meshes: set[str] = field(default_factory=set)  # MeshCollider の m_Mesh が指すアセットの GUID（#166）
    counts: Counter = field(default_factory=Counter)  # stripped でないドキュメントのクラス ID ごとの数


def _file_id(ref: object) -> int:
    return ref.file_id if isinstance(ref, UnityRef) else 0


def _vector(value: object, default: tuple[float, ...], keys: str) -> list[float]:
    if not isinstance(value, dict):
        return list(default)
    return [to_float(value.get(k), d) for k, d in zip(keys, default)]


def _targets(items: object) -> list[tuple[int, str | None]]:
    return [(r.file_id, ref_guid(r)) for r in (items if isinstance(items, list) else []) if isinstance(r, UnityRef)]


def _lod_levels(body: dict) -> list[list[int]]:
    """LODGroup の ``m_LODs`` を、段の順に並べた Renderer の fileID にする。

    無効な LODGroup（``m_Enabled: 0``）は Unity でもすべての Renderer が描かれるので、空のリストを返す。
    """
    if to_float(body.get("m_Enabled"), 1.0) == 0:
        return []
    lods = body.get("m_LODs")
    levels: list[list[int]] = []
    for lod in lods if isinstance(lods, list) else []:
        renderers = lod.get("renderers") if isinstance(lod, dict) else None
        refs = [item.get("renderer") for item in renderers if isinstance(item, dict)] if isinstance(renderers, list) else []
        levels.append([r.file_id for r in refs if isinstance(r, UnityRef) and r.file_id])
    return levels


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
        elif doc.class_id in RENDERER_CLASSES:
            renderer_docs.append(doc)
        elif (
            doc.class_id in COMPONENT_CLASSES or doc.class_id == CLASS_TERRAIN
            or (doc.class_id == CLASS_MONO_BEHAVIOUR and is_decal_projector(body))
        ):
            raw.components[doc.file_id] = (_file_id(body.get("m_GameObject")), doc.class_id, body)
        elif doc.class_id == CLASS_MESH_COLLIDER:
            mesh = ref_guid(body.get("m_Mesh"))
            if mesh:
                raw.collider_meshes.add(mesh)
        elif doc.class_id == CLASS_LOD_GROUP:
            levels = _lod_levels(body)
            if levels:
                raw.lod_groups.append(levels)
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
    """展開後の Transform 1 つ（GameObject と、あれば Renderer を含む）。

    ``key`` / ``parent`` / ``scope`` は展開ごとの通し番号。``game_object`` / ``renderer_key`` / ``components`` の key は
    Unity の fileID（展開したアセットの空間での値）。
    """

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
    components: dict[int, tuple[int, dict]] = field(default_factory=dict)  # コンポーネント（ライト・カメラ・デカール）の key → (クラス ID, 中身)
    # モデルの PrefabInstance で、古い形式の .meta（fileIDToRecycleName）から名前を引けた中への上書き
    # （名前を引けなかったマテリアルの上書きは、対象の fileID ごとに ``pending_renderer`` の名前で入る。#31）
    model_materials: dict[str, list[str | None]] = field(default_factory=dict)  # オブジェクト名 → スロット順の .mat
    model_transforms: dict[str, dict[str, list[float | None]]] = field(default_factory=dict)  # 名前 → position/rotation/scale
    model_parts: dict[str, PartState] = field(default_factory=dict)  # 中のオブジェクトの表示の上書きと削除（#172）
    # 1 メッシュの FBX（表で分かる畳まれたノードの名前）か表の無いモデル（``SOLE_NODE``）のモデルの PrefabInstance のルート。
    # このルートへの位置・回転・スケールの上書きは、どの階層のものも成分付きで ``model_transforms`` に記録する（#99 / #111）
    collapsed_key: str | None = None
    # モデルの PrefabInstance のルートで、モデルの中の fileID から今のアセットでの fileID への XOR（差し込んだ PrefabInstance の
    # fileID を重ねたもの）。prefab を挟んだ外側からの上書きの対象を、モデルの中の fileID に戻すのに使う（#31）
    id_mask: int = 0

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
            model_parts={k: replace(v) for k, v in self.model_parts.items()},
            **changes,
        )


@dataclass
class Hierarchy:
    nodes: dict[int, Node] = field(default_factory=dict)  # 親より先に子が来ることもある
    # Unity の fileID（このアセットの空間での値）→ Node の key。差し込んだものの fileID（XOR）が既にあれば、先のものを残す
    game_objects: dict[int, int] = field(default_factory=dict)
    renderers: dict[int, int] = field(default_factory=dict)
    components: dict[int, int] = field(default_factory=dict)  # コンポーネント（ライト・カメラ・デカール）
    transforms: dict[int, int] = field(default_factory=dict)  # 消した Node を指したままのことがある
    counts: Counter = field(default_factory=Counter)  # 展開したドキュメントのクラス ID ごとの数（モデルの中身は含まない）
    unresolved_overrides: int = 0  # モデルの中のオブジェクトを指すため当てられなかった上書き
    unresolved_material_overrides: int = 0  # そのうちマテリアルの上書き（Models / Prefabs 単位の警告に使う）
    missing_sources: int = 0  # 元がパッケージに無い PrefabInstance
    # MAX_NESTING で打ち切った PrefabInstance を含む（キャッシュした深さより浅い位置から使うときは展開し直す）
    depth_truncated: bool = False
    next_key: int = 1  # 次に振る Node の key（0 は使わない）

    def new_key(self) -> int:
        key = self.next_key
        self.next_key += 1
        return key

    def children(self) -> dict[int, list[int]]:
        result: dict[int, list[int]] = {}
        for key, node in self.nodes.items():
            if node.parent is not None:
                result.setdefault(node.parent, []).append(key)
        return result


ROOT_NODE_NAME = "//RootNode"


def model_object_name(table: dict[int, str], file_id: int) -> str | None:
    """古い形式の fileID をモデルのオブジェクト名にする。//RootNode の Renderer は、そのメッシュの名前にする。"""
    name = table.get(file_id)
    if name is None:
        return None
    if name != ROOT_NODE_NAME:
        return name
    # 1 メッシュの FBX では Renderer がルートに載り、Blender ではメッシュ名のオブジェクトになる
    meshes = [n for k, n in sorted(table.items()) if legacy_class(k) == CLASS_MESH and n != ROOT_NODE_NAME]
    return meshes[0] if len(meshes) == 1 else None


@dataclass
class PlacedComponent:
    """シーンに置かれたコンポーネント（ライト・カメラ・デカール。上書き済みの中身と、GameObject のワールド行列）。"""

    key: int
    node: int
    name: str
    class_id: int
    body: dict
    world: Mat4
    active: bool  # GameObject がアクティブで、コンポーネントが有効


def components(
    h: Hierarchy,
    class_ids: Iterable[int] = COMPONENT_CLASSES,
    worlds: Mapping[int, Mat4] | None = None,
    active: Mapping[int, bool] | None = None,
) -> list[PlacedComponent]:
    """階層に現れた順のコンポーネントのうち、``class_ids`` のもの（既定はライト・カメラ。デカールは ``CLASS_MONO_BEHAVIOUR``）。

    ``worlds`` / ``active`` に ``world_matrices`` / ``effective_active`` の結果を渡すと、求め直さない（1 つのシーンで何度も呼ぶとき）。
    """
    wanted = set(class_ids)
    worlds = world_matrices(h) if worlds is None else worlds
    active = effective_active(h) if active is None else active
    result = []
    for node_key, node in h.nodes.items():
        for key, (class_id, body) in node.components.items():
            if class_id not in wanted:
                continue
            enabled = to_float(body.get("m_Enabled"), 1.0) != 0
            result.append(PlacedComponent(key, node_key, node.name, class_id, body, worlds[node_key], active[node_key] and enabled))
    return result


def _from_roots(h: Hierarchy, top, step) -> dict:
    """全 Node について、最上位から順に ``step(親の値, Node)`` を重ねた値（最上位の親の値は ``top``）。

    親が見つからない・循環している Node は、そこを最上位として扱う。祖先の値は 1 度だけ求める。
    """
    result: dict = {}
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
        value = result.get(key, top) if key is not None else top
        for k in reversed(path):
            value = step(value, h.nodes[k])
            result[k] = value
    return result


def world_matrices(h: Hierarchy) -> dict[int, Mat4]:
    """全 Node のワールド行列。"""
    return _from_roots(h, IDENTITY, lambda parent, node: multiply(parent, node.local))


def effective_active(h: Hierarchy) -> dict[int, bool]:
    """祖先を含めてアクティブか（Unity の activeInHierarchy）。"""
    return _from_roots(h, True, lambda parent, node: parent and node.active)


def node_names_in_table(table: dict[int, str]) -> set[str] | None:
    """.meta の表にある、ルート以外の GameObject の名前（FBX のノード名）。

    空の集合は 1 メッシュの FBX（GameObject はルートだけで、メッシュも 1 つ）。ノードが分からない表は None
    （GameObject の行が無い表や、古い番号を引き継いだ ``internalIDToNameTable`` で一部の行しか無いもの）。
    """
    game_objects = {n for k, n in table.items() if legacy_class(k) == CLASS_GAME_OBJECT}
    names = game_objects - {ROOT_NODE_NAME}
    if names:
        return names
    meshes = sum(1 for k in table if legacy_class(k) == CLASS_MESH)
    return set() if game_objects and meshes <= 1 else None


def collapsed_root_name(table: dict[int, str]) -> str | None:
    """1 メッシュの FBX で、Unity が ``//RootNode`` に畳んだノード（Blender ではメッシュ名のオブジェクト）の名前。

    表が無いモデルと、ノードが複数あるモデルは None。この形の FBX では、モデルのルートの Transform が
    FBX のノードの変換を持ち、メッシュの頂点はノード空間のままになる（Issue #98 / #99。Unity 6000.6.0f1 で確認）。
    """
    if node_names_in_table(table) != set():
        return None
    root_id = next((k for k in table if legacy_class(k) == CLASS_GAME_OBJECT), 0)
    return model_object_name(table, root_id)
