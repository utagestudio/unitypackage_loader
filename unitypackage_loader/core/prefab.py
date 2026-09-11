"""prefab から「GameObject 名 → Renderer のマテリアル配列」を取り出す。

FBX の .meta（externalObjects）に対応表が無いパッケージ向けのフォールバック。
プレハブ内に直接置かれた MeshRenderer / SkinnedMeshRenderer の ``m_Materials`` を読む。
ネストされた PrefabInstance の上書き（``m_Modifications``）は対象の fileID が FBX 内部 ID
なので名前に結び付けられず、現時点では扱わない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .unity_yaml import UnityRef, parse_documents

CLASS_GAME_OBJECT = 1
CLASS_MESH_RENDERER = 23
CLASS_SKINNED_MESH_RENDERER = 137


@dataclass
class RendererMaterials:
    game_object: str
    materials: list[str | None] = field(default_factory=list)  # スロット順の .mat GUID
    renderer_class: int = CLASS_MESH_RENDERER


def parse_prefab_materials(text: str) -> dict[str, RendererMaterials]:
    """GameObject 名をキーにした Renderer マテリアル表。同名があれば最初のものを採用する。"""
    docs = parse_documents(text)
    names: dict[int, str] = {}
    for doc in docs:
        if doc.class_id == CLASS_GAME_OBJECT and not doc.stripped:
            name = doc.body.get("m_Name")
            if isinstance(name, str):
                names[doc.file_id] = name

    result: dict[str, RendererMaterials] = {}
    for doc in docs:
        if doc.class_id not in (CLASS_MESH_RENDERER, CLASS_SKINNED_MESH_RENDERER) or doc.stripped:
            continue
        go = doc.body.get("m_GameObject")
        if not isinstance(go, UnityRef):
            continue
        name = names.get(go.file_id)
        if name is None or name in result:
            continue
        mats = doc.body.get("m_Materials")
        if not isinstance(mats, list):
            continue
        guids = [m.guid if isinstance(m, UnityRef) and m.guid else None for m in mats]
        result[name] = RendererMaterials(name, guids, doc.class_id)
    return result


def merge_prefab_tables(tables: list[dict[str, RendererMaterials]]) -> dict[str, RendererMaterials]:
    merged: dict[str, RendererMaterials] = {}
    for table in tables:
        for name, rm in table.items():
            merged.setdefault(name, rm)
    return merged
