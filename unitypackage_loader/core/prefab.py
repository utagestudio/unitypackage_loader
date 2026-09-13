"""prefab から、モデルごとの「GameObject 名 → Renderer のマテリアル配列」を取り出す。

Unity で表示されるマテリアルは Renderer の ``m_Materials`` で決まり、モデル .meta の externalObjects は
FBX を置いたときの既定値にすぎない。そのため importer は、prefab に割り当てがあれば .meta や名前一致で
決まった結果より優先する。

Renderer がどのモデルのものかは、メッシュ参照（MeshRenderer と同じ GameObject の MeshFilter、または
SkinnedMeshRenderer の ``m_Mesh``）の GUID で決める。メッシュ参照が無い Renderer と、パッケージ内の
モデルを指さない Renderer は使わない（別モデルの同名オブジェクトに当てはめないため）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .unity_yaml import UnityRef, parse_documents

CLASS_GAME_OBJECT = 1
CLASS_MESH_FILTER = 33
CLASS_MESH_RENDERER = 23
CLASS_SKINNED_MESH_RENDERER = 137


@dataclass
class RendererMaterials:
    game_object: str
    materials: list[str | None] = field(default_factory=list)  # スロット順の .mat GUID
    renderer_class: int = CLASS_MESH_RENDERER
    mesh_guid: str | None = None  # メッシュを持つアセット（モデル）の GUID


@dataclass
class PrefabDocument:
    renderers: dict[int, RendererMaterials] = field(default_factory=dict)  # prefab 内の fileID → Renderer


def _guid(ref: object) -> str | None:
    return ref.guid.lower() if isinstance(ref, UnityRef) and ref.guid else None


def parse_prefab(text: str) -> PrefabDocument:
    """prefab に直接置かれた MeshRenderer / SkinnedMeshRenderer を読む。"""
    docs = parse_documents(text)
    names: dict[int, str] = {}
    meshes: dict[int, str] = {}  # GameObject の fileID → MeshFilter が指すメッシュの GUID
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

    result = PrefabDocument()
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
        guids = [m.guid if isinstance(m, UnityRef) and m.guid else None for m in mats]
        if doc.class_id == CLASS_SKINNED_MESH_RENDERER:
            mesh = _guid(doc.body.get("m_Mesh"))
        else:
            mesh = meshes.get(go.file_id)
        result.renderers[doc.file_id] = RendererMaterials(name, guids, doc.class_id, mesh)
    return result


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
