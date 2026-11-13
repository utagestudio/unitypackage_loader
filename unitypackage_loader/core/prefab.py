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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from .hierarchy import (
    CLASS_MESH_RENDERER,
    Hierarchy,
    ModelNames,
    ModelPlacement,
    PartState,
    built_from_renderers,
    pending_renderer_id,
    placements,
)
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
    # prefab で表示されているか（GameObject が祖先も含めてアクティブで、Renderer が有効。読み込む単位 Prefabs で隠す。#177）
    visible: bool = True


def find_renderer(table: dict[str, RendererMaterials], obj_name: str) -> RendererMaterials | None:
    """Blender のオブジェクト名で表を引く（完全一致 → 連番を外した形 → メッシュ参照の fileID のハッシュ →
    表の名前の連番も外した形）。

    prefab で GameObject の名前を変えてあると名前では引けないので、オブジェクト名から求めたメッシュの fileID を
    表の Renderer のメッシュ参照と照合する（Scenes 単位と同じ。#71）。
    FBX のノード名がもともと「Base-Ring.001」のように連番付きだと、同じ FBX を 2 回目に読み込んだとき Blender が
    「Base-Ring.002」に付け替え、ここまでのどれでも引けない。最後に表の名前の連番も外して比べ、該当する行が
    1 つだけならそれを使う。複数あれば（「Body」と「Body.001」が両方ある FBX など）どれか決められないので引かない（#155）。
    """
    base = strip_numeric_suffix(obj_name)
    rm = table.get(obj_name) or table.get(base)
    if rm is not None:
        return rm
    ids = {mesh_file_id(obj_name), mesh_file_id(base)}
    rm = next((r for r in table.values() if r.mesh_file_id and r.mesh_file_id in ids), None)
    if rm is not None:
        return rm
    candidates = [r for name, r in table.items() if strip_numeric_suffix(name) == base]
    return candidates[0] if len(candidates) == 1 else None


def parts_from_placements(placed: Iterable[ModelPlacement]) -> dict[str, dict[str, PartState]]:
    """モデルの GUID → モデルの PrefabInstance の中への表示の上書きと削除（``ModelPlacement.parts``。#172）。

    読み込む単位 Prefabs ではモデルを 1 回だけ読み込むので、同じモデルの配置が複数あれば先の配置のものを採る。
    """
    result: dict[str, dict[str, PartState]] = {}
    for placement in placed:
        if placement.parts and placement.model_guid not in result:
            result[placement.model_guid] = dict(placement.parts)
    return result


def tables_from_hierarchy(
    h: Hierarchy,
    model_names: ModelNames,
    name_tables: dict[str, dict[int, str]] | None = None,
    placed: list[ModelPlacement] | None = None,
) -> dict[str, dict[str, RendererMaterials]]:
    """展開した prefab の階層から、モデルの GUID → GameObject 名 → Renderer の表を作る。

    ``model_names`` と ``name_tables`` は ``hierarchy.placements`` と同じ（モデルの GUID → ルートの名前、.meta の表）。
    同じモデルに同名の GameObject があれば先のもの（配置の順）を採用する。モデルをそのまま置いた PrefabInstance は、
    中へのマテリアルの上書き（名前で引けたものと、対象の fileID で残したもの）だけが表に入る。1 つも無ければ表に入れない
    （FBX を置いただけの prefab を、モデルを使う prefab として数えない）。中のオブジェクトの表示を上書き・削除していれば
    （``ModelPlacement.parts``）、空の表で入れる（#172）。
    ``placed`` に ``placements`` の結果を渡すと、配置を求め直さない。

    ``visible`` は、その Renderer が prefab で表示されているか（非アクティブな配置・GameObject の下や、無効な Renderer
    なら False）。同名の行が複数の配置にあれば、どれかで表示されていれば表示する（#177）。
    """
    result: dict[str, dict[str, RendererMaterials]] = {}
    for placement in placed if placed is not None else placements(h, model_names, name_tables):
        if not placement.renderers and not placement.parts:
            continue
        table = result.setdefault(placement.model_guid, {})
        for name, renderer in placement.renderers.items():
            visible = placement.active and renderer.visible
            row = table.get(name)
            if row is not None:
                row.visible = row.visible or visible
                continue
            table[name] = RendererMaterials(
                name, list(renderer.materials), renderer.renderer_class, placement.model_guid,
                renderer.mesh_file_id, renderer.lod_level, visible,
            )
    return result


def inactive_models(placed: list[ModelPlacement]) -> set[str]:
    """prefab の中の配置がすべて非アクティブなモデルの GUID（読み込む単位 Prefabs で、すべての部品を隠す。#177）。

    非アクティブな GameObject の下に FBX を丸ごと置いたときなど。表の行が無くても隠せるよう、行とは別に求める。
    """
    active: dict[str, bool] = {}
    for placement in placed:
        active[placement.model_guid] = active.get(placement.model_guid, False) or placement.active
    return {guid for guid, is_active in active.items() if not is_active}


def models_placed_whole(h: Hierarchy, placed: list[ModelPlacement]) -> set[str]:
    """モデルの PrefabInstance で FBX を丸ごと置いている配置のある、モデルの GUID。

    そのモデルは FBX のすべての部品が Unity にあるので、表に無い部品も隠さない（#151）。
    """
    return {p.model_guid for p in placed if not built_from_renderers(h, p)}


def unused_parts(table: dict[str, RendererMaterials], objects: Iterable[tuple[str, bool]]) -> set[str]:
    """表の Renderer が使っていないメッシュのオブジェクト名（読み込む単位 Prefabs で隠すもの。#151）。

    ``objects`` は (Blender のオブジェクト名, メッシュか)。照合は ``find_renderer``（名前 → 連番を外した形 → メッシュ参照の
    fileID）に加えて、表の名前の連番も外して比べる（Scenes 単位の ``scene_import.unused_in_placement`` と同じ）。
    FBX のノード名がもともと「Base-Ring.001」のように連番付きだと、同じ FBX を 2 回目に読み込んだとき Blender が
    「Base-Ring.002」に付け替えるので、片側だけ外したのでは使っている部品まで隠して（消して）しまう。
    FBX を丸ごと置いたモデル（``models_placed_whole``）には使わない。
    """
    stripped = {strip_numeric_suffix(n) for n in table}
    return {
        name for name, is_mesh in objects
        if is_mesh and strip_numeric_suffix(name) not in stripped and find_renderer(table, name) is None
    }


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


def has_pending_renderers(table: dict[str, RendererMaterials]) -> bool:
    """名前を引けなかった Renderer の行（``hierarchy.pending_renderer``）があるか。"""
    return any(pending_renderer_id(name) is not None for name in table)


def _merge_into(result: dict[str, RendererMaterials], name: str, row: RendererMaterials) -> None:
    """``row`` のマテリアルをオブジェクト ``name`` の行にする。名前で引けた行があれば、その空いたスロットだけを埋める。"""
    named = find_renderer(result, name)
    if named is None:
        result[name] = RendererMaterials(name, list(row.materials), row.renderer_class, row.mesh_guid, visible=row.visible)
        return
    size = max(len(named.materials), len(row.materials))
    own = named.materials + [None] * (size - len(named.materials))
    other = row.materials + [None] * (size - len(row.materials))
    result[named.game_object] = replace(named, materials=[a or b for a, b in zip(own, other)])


def resolve_model_renderers(
    table: dict[str, RendererMaterials], claims: Mapping[int, str], mesh_objects: list[str]
) -> tuple[dict[str, RendererMaterials], int]:
    """名前を引けなかった Renderer の行（モデルの中へのマテリアルの上書き）を、読み込んだオブジェクトの名前に付け替える。

    ``claims`` は ``model_ids.object_claims`` の結果（fileID → オブジェクトの名前）。行の fileID がそこにあれば、
    そのオブジェクトの行にする（名前で引けた行があれば、その行の空いたスロットだけを埋める。#31）。
    無ければ、そういう行が 1 つだけで、読み込んだモデルのメッシュのオブジェクトも 1 つのときに限って、そのオブジェクトに
    当てる（Unity の Renderer もそれ 1 つなので確定する。#109）。それ以外は外す（スロット数などからの推測で当てると、
    別の部位の色が変わるため）。戻り値は (表, 当てられなかった上書きの数)。
    """
    pending = {name: uid for name in table if (uid := pending_renderer_id(name)) is not None}
    if not pending:
        return table, 0
    result = {name: rm for name, rm in table.items() if name not in pending}
    unmatched: list[RendererMaterials] = []
    for name, uid in pending.items():
        owner = claims.get(uid)
        if owner is None:
            unmatched.append(table[name])
        else:
            _merge_into(result, owner, table[name])
    if len(unmatched) == 1 and len(pending) == 1 and len(mesh_objects) == 1:
        _merge_into(result, mesh_objects[0], unmatched[0])
        unmatched = []
    return result, sum(1 for row in unmatched for m in row.materials if m)
