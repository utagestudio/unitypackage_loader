"""prefab から、モデルごとの「GameObject 名 → Renderer のマテリアル配列」を取り出す。

Unity で表示されるマテリアルは Renderer の ``m_Materials`` で決まり、モデル .meta の externalObjects は
FBX を置いたときの既定値にすぎない。そのため importer は、prefab に割り当てがあれば .meta や名前一致で
決まった結果より優先する。

Renderer がどのモデルのものかは、メッシュ参照（MeshRenderer と同じ GameObject の MeshFilter、または
SkinnedMeshRenderer の ``m_Mesh``）の GUID で決める。メッシュ参照が無い Renderer と、パッケージ内の
モデルを指さない Renderer は使わない（別モデルの同名オブジェクトに当てはめないため）。

PrefabInstance（ネストされた prefab、Prefab Variant）は、元がパッケージ内の prefab ならその Renderer を
引き継ぎ、``m_Modifications`` のマテリアル上書き（``m_Materials.Array.data[N]`` / ``m_Materials.Array.size``）を
重ねる。引き継いだオブジェクトの fileID は Unity と同じく「PrefabInstance の fileID XOR 元の fileID」とする。
元がモデル（FBX 等）の上書きは、対象の fileID がモデル内部の ID で名前に結び付けられないため読まず、
``unresolved_material_overrides`` で数だけ数える。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from .unity_yaml import UnityRef, parse_documents

CLASS_GAME_OBJECT = 1
CLASS_MESH_FILTER = 33
CLASS_MESH_RENDERER = 23
CLASS_SKINNED_MESH_RENDERER = 137
CLASS_PREFAB_INSTANCE = 1001

MAX_NESTING = 16  # PrefabInstance をたどる深さの上限
MAX_SLOTS = 1024  # 上書きで受け付けるマテリアル配列の長さの上限（細工された巨大な添字への備え）
_FILE_ID_MASK = 0x7FFF_FFFF_FFFF_FFFF
_MATERIAL_PATH = re.compile(r"m_Materials\.Array\.data\[(\d+)\]")


@dataclass
class RendererMaterials:
    game_object: str
    materials: list[str | None] = field(default_factory=list)  # スロット順の .mat GUID
    renderer_class: int = CLASS_MESH_RENDERER
    mesh_guid: str | None = None  # メッシュを持つアセット（モデル）の GUID


@dataclass
class PrefabInstance:
    file_id: int
    source_guid: str
    material_overrides: dict[int, dict[int, str | None]] = field(default_factory=dict)  # 元の fileID → {スロット: .mat}
    size_overrides: dict[int, int] = field(default_factory=dict)  # 元の fileID → m_Materials の長さ

    def apply(self, source_file_id: int, rm: RendererMaterials) -> RendererMaterials:
        size = self.size_overrides.get(source_file_id)
        slots = self.material_overrides.get(source_file_id, {})
        if size is None and not slots:
            return rm
        materials = list(rm.materials)
        if size is not None:
            materials = (materials + [None] * size)[:size]
        for index, guid in sorted(slots.items()):
            if size is not None and index >= size:
                continue
            if index >= len(materials):
                materials.extend([None] * (index + 1 - len(materials)))
            materials[index] = guid
        return replace(rm, materials=materials)


@dataclass
class PrefabDocument:
    renderers: dict[int, RendererMaterials] = field(default_factory=dict)  # prefab 内の fileID → Renderer
    instances: list[PrefabInstance] = field(default_factory=list)


def _guid(ref: object) -> str | None:
    return ref.guid.lower() if isinstance(ref, UnityRef) and ref.guid else None


def _material_guid(ref: object) -> str | None:
    return ref.guid if isinstance(ref, UnityRef) and ref.guid else None


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_instance(file_id: int, source_guid: str, modification: object) -> PrefabInstance:
    instance = PrefabInstance(file_id, source_guid)
    items = modification.get("m_Modifications") if isinstance(modification, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        target, path = item.get("target"), item.get("propertyPath")
        if not isinstance(target, UnityRef) or _guid(target) != source_guid or not isinstance(path, str):
            continue
        if path == "m_Materials.Array.size":
            size = _int(item.get("value"))
            if size is not None and 0 <= size <= MAX_SLOTS:
                instance.size_overrides[target.file_id] = size
            continue
        match = _MATERIAL_PATH.fullmatch(path)
        if match and int(match.group(1)) < MAX_SLOTS:
            slots = instance.material_overrides.setdefault(target.file_id, {})
            slots[int(match.group(1))] = _material_guid(item.get("objectReference"))
    return instance


def parse_prefab(text: str) -> PrefabDocument:
    """prefab に直接置かれた MeshRenderer / SkinnedMeshRenderer と、PrefabInstance のマテリアル上書きを読む。"""
    docs = parse_documents(text)
    names: dict[int, str] = {}
    meshes: dict[int, str] = {}  # GameObject の fileID → MeshFilter が指すメッシュの GUID
    result = PrefabDocument()
    for doc in docs:
        if doc.stripped:
            continue
        if doc.class_id == CLASS_GAME_OBJECT:
            name = doc.body.get("m_Name")
            if isinstance(name, str):
                names[doc.file_id] = name
        elif doc.class_id == CLASS_MESH_FILTER:
            go, mesh = doc.body.get("m_GameObject"), _guid(doc.body.get("m_Mesh"))
            if isinstance(go, UnityRef) and mesh:
                meshes[go.file_id] = mesh
        elif doc.class_id == CLASS_PREFAB_INSTANCE:
            source = _guid(doc.body.get("m_SourcePrefab"))
            if source:
                result.instances.append(_parse_instance(doc.file_id, source, doc.body.get("m_Modification")))

    for doc in docs:
        if doc.class_id not in (CLASS_MESH_RENDERER, CLASS_SKINNED_MESH_RENDERER) or doc.stripped:
            continue
        go = doc.body.get("m_GameObject")
        if not isinstance(go, UnityRef):
            continue
        name = names.get(go.file_id)
        mats = doc.body.get("m_Materials")
        if name is None or not isinstance(mats, list):
            continue
        guids = [_material_guid(m) for m in mats]
        if doc.class_id == CLASS_SKINNED_MESH_RENDERER:
            mesh = _guid(doc.body.get("m_Mesh"))
        else:
            mesh = meshes.get(go.file_id)
        result.renderers[doc.file_id] = RendererMaterials(name, guids, doc.class_id, mesh)
    return result


def resolve_renderers(
    guid: str,
    documents: dict[str, PrefabDocument],
    cache: dict[str, dict[int, RendererMaterials]] | None = None,
) -> dict[int, RendererMaterials]:
    """prefab 内の Renderer（fileID → 表）。元がパッケージ内の prefab の PrefabInstance から引き継いだものを含む。

    ``documents`` は prefab の GUID（小文字）→ ``parse_prefab`` の結果。``cache`` を渡すと複数の prefab で結果を共有する。
    """
    return _resolve(guid.lower(), documents, {} if cache is None else cache, ())


def _resolve(guid, documents, cache, stack) -> dict[int, RendererMaterials]:
    if guid in cache:
        return cache[guid]
    document = documents.get(guid)
    if document is None or guid in stack or len(stack) >= MAX_NESTING:
        return {}
    result = dict(document.renderers)
    for instance in document.instances:
        for source_id, rm in _resolve(instance.source_guid, documents, cache, stack + (guid,)).items():
            result.setdefault((instance.file_id ^ source_id) & _FILE_ID_MASK, instance.apply(source_id, rm))
    cache[guid] = result
    return result


def unresolved_material_overrides(
    guid: str,
    documents: dict[str, PrefabDocument],
    cache: dict[str, dict[int, RendererMaterials]] | None = None,
) -> int:
    """prefab 直下の PrefabInstance にある、Renderer を特定できないマテリアル上書きの数（元がモデルの場合など）。"""
    document = documents.get(guid.lower())
    if document is None:
        return 0
    cache = {} if cache is None else cache
    count = 0
    for instance in document.instances:
        targets = set(instance.material_overrides) | set(instance.size_overrides)
        if not targets:
            continue
        known = resolve_renderers(instance.source_guid, documents, cache)
        count += sum(1 for file_id in targets if file_id not in known)
    return count


def tables_by_model(
    renderers: Iterable[RendererMaterials], model_guids: Iterable[str]
) -> dict[str, dict[str, RendererMaterials]]:
    """Renderer をメッシュ参照のモデルごとに分け、GameObject 名をキーにした表にする。

    ``model_guids`` に無いメッシュを指す Renderer とメッシュ参照の無い Renderer は除く。
    同じモデルに同名の GameObject があれば先のものを採用する。
    """
    models = {g.lower() for g in model_guids}
    result: dict[str, dict[str, RendererMaterials]] = {}
    for rm in renderers:
        if rm.mesh_guid not in models:
            continue
        result.setdefault(rm.mesh_guid, {}).setdefault(rm.game_object, rm)
    return result


def merge_prefab_tables(tables: list[dict[str, RendererMaterials]]) -> dict[str, RendererMaterials]:
    merged: dict[str, RendererMaterials] = {}
    for table in tables:
        for name, rm in table.items():
            merged.setdefault(name, rm)
    return merged
