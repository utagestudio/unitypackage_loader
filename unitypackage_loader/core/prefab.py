"""展開した prefab から、モデルごとの「GameObject 名 → Renderer のマテリアル配列」の表を作る。

Unity で表示されるマテリアルは Renderer の ``m_Materials`` で決まり、モデル .meta の externalObjects は
FBX を置いたときの既定値にすぎない。そのため importer は、prefab に割り当てがあれば .meta や名前一致で
決まった結果より優先する。

prefab の展開（PrefabInstance・Prefab Variant・ネスト、``m_Modifications``、``m_RemovedGameObjects`` /
``m_RemovedComponents``、Unity 2018.2 以前の形式、古い形式の .meta で名前を引けるモデルの中への上書き）は、
Scenes 単位と同じ ``hierarchy.Expander`` で行い、表はその配置（``hierarchy.placements``）から作る。
以前は Models / Prefabs 単位だけ別の解析器を使っていて、対応範囲の違いから読み込む単位によって割り当てが食い違っていた（#71）。

Renderer がどのモデルのものかは、メッシュ参照（MeshRenderer と同じ GameObject の MeshFilter、または
SkinnedMeshRenderer の ``m_Mesh``）の GUID で決める。パッケージ内のモデルを指さない Renderer は使わない
（別モデルの同名オブジェクトに当てはめないため）。名前は配置と同じく、Unity の複製番号「 (N)」を外した GameObject 名か、
.meta の表で引いたメッシュ名。名前で引けないときのために、メッシュ参照の fileID も持つ（``mapping`` がオブジェクト名のハッシュと照合する）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hierarchy import CLASS_MESH_RENDERER, Hierarchy, ModelNames, placements
from .meta import strip_numeric_suffix
from .unity_ids import mesh_file_id


@dataclass
class RendererMaterials:
    game_object: str
    materials: list[str | None] = field(default_factory=list)  # スロット順の .mat GUID
    renderer_class: int = CLASS_MESH_RENDERER
    mesh_guid: str | None = None  # メッシュを持つアセット（モデル）の GUID
    mesh_file_id: int = 0  # メッシュ参照の fileID（モデルの中のどのメッシュか）
    lod_level: int = 0  # LODGroup の段（0 = LOD0、または LODGroup が無い）


def find_renderer(table: dict[str, RendererMaterials], obj_name: str) -> RendererMaterials | None:
    """Blender のオブジェクト名で表を引く（完全一致 → 連番を外した形 → メッシュ参照の fileID のハッシュ）。

    prefab で GameObject の名前を変えてあると名前では引けないので、オブジェクト名から求めたメッシュの fileID を
    表の Renderer のメッシュ参照と照合する（Scenes 単位と同じ。#71）。
    """
    rm = table.get(obj_name) or table.get(strip_numeric_suffix(obj_name))
    if rm is not None:
        return rm
    ids = {mesh_file_id(obj_name), mesh_file_id(strip_numeric_suffix(obj_name))}
    return next((r for r in table.values() if r.mesh_file_id and r.mesh_file_id in ids), None)


def tables_from_hierarchy(
    h: Hierarchy,
    model_names: ModelNames,
    name_tables: dict[str, dict[int, str]] | None = None,
) -> dict[str, dict[str, RendererMaterials]]:
    """展開した prefab の階層から、モデルの GUID → GameObject 名 → Renderer の表を作る。

    ``model_names`` と ``name_tables`` は ``hierarchy.placements`` と同じ（モデルの GUID → ルートの名前、.meta の表）。
    同じモデルに同名の GameObject があれば先のもの（配置の順）を採用する。モデルをそのまま置いた PrefabInstance は、
    中への上書きを名前で引けたものだけが表に入る。1 つも引けなければ表に入れない（FBX を置いただけの prefab を、
    モデルを使う prefab として数えない。読み込む単位 Prefabs の候補は以前と同じ）。
    """
    result: dict[str, dict[str, RendererMaterials]] = {}
    for placement in placements(h, model_names, name_tables):
        if not placement.renderers:
            continue
        table = result.setdefault(placement.model_guid, {})
        for name, renderer in placement.renderers.items():
            table.setdefault(
                name,
                RendererMaterials(
                    name, list(renderer.materials), renderer.renderer_class, placement.model_guid,
                    renderer.mesh_file_id, renderer.lod_level,
                ),
            )
    return result


def merge_prefab_tables(tables: list[dict[str, RendererMaterials]]) -> dict[str, RendererMaterials]:
    """表を先勝ちで統合する。同じ名前に加えて、先の表が割り当て済みのメッシュ（メッシュ参照の fileID）の行も採らない。

    Prefab Variant で GameObject の名前を変えると、同じメッシュの行が別の名前で並ぶ。名前だけで統合すると後の表の
    元の名前の行が名前一致で優先されてしまうので、メッシュでも先勝ちにする（同じ表の中で同じメッシュを使う行は残す）。
    """
    merged: dict[str, RendererMaterials] = {}
    claimed: set[int] = set()
    for table in tables:
        added: set[int] = set()
        for name, rm in table.items():
            if name in merged or (rm.mesh_file_id and rm.mesh_file_id in claimed):
                continue
            merged[name] = rm
            if rm.mesh_file_id:
                added.add(rm.mesh_file_id)
        claimed |= added
    return merged
