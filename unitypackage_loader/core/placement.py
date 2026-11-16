"""展開した階層から、モデル（FBX 等）の配置を求める。bpy 非依存。

配置（``ModelPlacement``）は 2 種類:

- モデルの PrefabInstance: そのルートの Node。
- モデルのメッシュを直接指す Renderer（FBX を展開した prefab やシーン）: モデルのルートとみなす Node を Renderer ごとに
  決め、同じルート・同じモデルの Renderer をまとめる。Renderer ごとに、ルートから GameObject までの Unity での行列を
  ``PlacedRenderer.relative`` に持ち、読み込み側が FBX のオブジェクトと比べて、Unity 上で動かした部品を置き直す（Issue #186）。
  FBX 以外のモデルでは、中のノードが上書きで動いていれば、Renderer ごとに「そのノードから逆算したルートの行列」を
  ``offsets`` に持つ。ルートの決め方は ``placements`` を参照（Issue #60）。
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Container, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import NamedTuple

from .expander import Expander
from .hierarchy import (
    CLASS_CAMERA,
    CLASS_LIGHT,
    CLASS_MESH_RENDERER,
    CLASS_MONO_BEHAVIOUR,
    CLASS_RECT_TRANSFORM,
    CLASS_TERRAIN,
    CLOSE,
    ROOT_NODE_NAME,
    SOLE_NODE,
    Hierarchy,
    Node,
    PartState,
    RawAsset,
    collapsed_root_name,
    effective_active,
    node_names_in_table,
    unity_base_name,
    world_matrices,
)
from .primitives import builtin_mesh_kind
from .terrain import terrain_data_ref
from .transform import Mat4, inverse_affine, multiply
from .unity_ids import mesh_file_id


@dataclass
class PlacedRenderer:
    name: str
    materials: list[str | None]
    renderer_class: int
    visible: bool  # GameObject がアクティブで Renderer が有効
    mesh_file_id: int = 0  # メッシュ参照の fileID（表で名前を引けなかったとき、読み込み側がハッシュで照合する）
    lod_level: int = 0  # LODGroup の段（0 = LOD0、または LODGroup が無い）
    # Unity でのモデルのルートからこの GameObject までの行列（ルートのワールド行列の逆 · GameObject のワールド行列）。
    # 展開した Renderer の配置で、ルートより下の Renderer だけが持つ。同じ配置に同名の Renderer があれば、どれのものか
    # 決められないので None（#186）
    relative: Mat4 | None = None
    shadows_only: bool = False  # Shadows Only の Renderer（影だけを落とし、本体は描かない。#232）


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
    # モデルの PrefabInstance の中のオブジェクトへの表示の上書きと削除（``PartState``。#172）
    parts: dict[str, PartState] = field(default_factory=dict)

    def signature(self) -> tuple:
        """読み込み結果を使い回せるかの判定に使う値（モデル、名前ごとのマテリアル、消した中のオブジェクト）。"""
        return (
            self.model_guid,
            tuple(sorted((name, tuple(r.materials)) for name, r in self.renderers.items())),
            tuple(sorted(key for key, state in self.parts.items() if state.removed)),
        )


def built_from_renderers(h: Hierarchy, placement: ModelPlacement) -> bool:
    """展開した prefab・シーンの Renderer から作った配置か（モデルの PrefabInstance で FBX を丸ごと置いた配置ではないか）。

    このとき Unity にあるのは ``renderers`` の Renderer だけで、FBX のほかの部品は使われていない（#58）。
    """
    return placement.root in h.nodes and h.nodes[placement.root].model_guid is None


def _close(a: Mat4, b: Mat4) -> bool:
    return all(abs(x - y) <= CLOSE * max(1.0, abs(x), abs(y)) for ra, rb in zip(a, b) for x, y in zip(ra, rb))


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
        if mesh_name in names or (not names and mesh_name and mesh_name != ROOT_NODE_NAME):
            return mesh_name
    return None


def _is_top_node(node: Node, model_guid: str, top: str, table: dict[int, str]) -> bool:
    """Node が、FBX のただ 1 つの最上位のノード ``top`` を展開した GameObject か（#185）。

    GameObject 名で引き、名前を変えてあれば、Node 自身の Renderer のメッシュ参照（表の名前か、名前から求めた fileID）で引く。
    """
    if unity_base_name(node.name) == top:
        return True
    renderer = node.renderer
    if renderer is None or renderer.mesh_guid != model_guid or not renderer.mesh_file_id:
        return False
    named = table.get(renderer.mesh_file_id)
    return named == top if named is not None else renderer.mesh_file_id == mesh_file_id(top)


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
    h: Hierarchy,
    model_guids: ModelNames,
    name_tables: dict[str, dict[int, str]] | None = None,
    sole_nodes: Mapping[str, str] | None = None,
    top_nodes: Mapping[str, str] | None = None,
) -> list[ModelPlacement]:
    """モデルの配置を、階層に現れた順に返す。

    ``model_guids`` にモデルの GUID → ルートの名前の辞書を渡すと、下の「候補の名前がモデルの名前と同じか」に使う。

    ``name_tables``（モデルの GUID → .meta の表の fileID → 名前）があれば、Renderer の名前にはメッシュ参照から引いた
    メッシュの名前（= FBX のノード名、Blender のオブジェクト名）を使う。展開した prefab で GameObject の名前を
    変えていても、モデルのオブジェクトと照合できる（#58）。引けなければ GameObject の名前。

    Renderer をまとめるモデルのルート（Issue #60）: 表の無いモデルは最上位の祖先（scope。下の ``sole_nodes`` を除く）。表のあるモデルは
    ``_root_candidates`` の候補にまとめるが、次のときは候補をモデルのルートとみなさず、Renderer の GameObject
    （たどった FBX のノード）自身をルートにする。シーンの共通の親の下に、FBX から切り離した小物を複製して並べた場合。

    - まとめると同じメッシュの Renderer が重なる（複製を並べた入れ物）。
    - 候補の直下に別のモデルがあり、候補の名前がモデルの名前と違う（部屋のような入れ物）。

    FBX の中のノードをルートにした配置は、``node_transforms`` でそのノードを単位行列にする。Unity の GameObject の
    行列がノードの元の変換を含んでいるので、Blender のオブジェクトの元の変換を掛けないようにするため。
    1 メッシュの FBX（Renderer の GameObject 自身がルート）も同じで、Unity はノードを ``//RootNode`` に畳んで
    その変換をモデルのルートの Transform に載せ、メッシュの頂点はノード空間のままにするため、メッシュを直接指す
    Renderer にはノードの変換が掛からない（Unity 6000.6.0f1 で確認。Issue #98）。

    ``sole_nodes``（モデルの GUID → FBX のただ 1 つのノードの名前）は、表の無いモデルのうち FBX のノードが 1 つだけの
    もの。表では 1 メッシュの FBX と分からないが、Unity での置かれ方は同じなので、メッシュを直接指す Renderer の配置は
    表のある 1 メッシュの FBX と同じに扱う（メッシュはこのノードのものしか無いので、メッシュ参照の名前もこのノード名）。
    モデルの PrefabInstance の配置は今までどおり（読み込み側がオブジェクトの数で決める。#111）。Issue #181。

    ``top_nodes``（モデルの GUID → FBX のただ 1 つの最上位のノードの名前）: Unity は最上位のノードが 1 つだけの FBX で、
    子があってもそのノードをモデルのルートに畳むので、FBX を展開した GameObject のルートの行列はノードの変換を含む。
    Renderer から作った配置のルートがそのノード（GameObject 名か、ルートの Renderer のメッシュ参照で判定）なら、
    そのノードを単位行列にする。表の有無によらない（表ではルートが ``//RootNode`` になり、ノード名として引けないため）。Issue #185。
    """
    root_names = _model_names(model_guids)
    models = set(root_names)
    tops = {g.lower(): name for g, name in (top_nodes or {}).items() if name}
    tables = {g.lower(): table for g, table in (name_tables or {}).items()}
    node_names = {g: names for g, table in tables.items() if (names := node_names_in_table(table)) is not None}
    # Renderer の配置で使う表。表の無い 1 ノードの FBX は、メッシュ参照の fileID → ノード名の表をその場で作る
    sole = {
        g.lower(): name for g, name in (sole_nodes or {}).items()
        if name and not tables.get(g.lower()) and g.lower() in models
    }
    renderer_tables = {g: table for g, table in tables.items() if g not in sole}
    for node in h.nodes.values():
        renderer = node.renderer
        if renderer is not None and renderer.mesh_guid in sole and renderer.mesh_file_id:
            renderer_tables.setdefault(renderer.mesh_guid, {})[renderer.mesh_file_id] = sole[renderer.mesh_guid]
    for g in sole:
        if g in renderer_tables:
            node_names[g] = set()
    # 表に無いメッシュを指す Renderer があれば、その表は一部の行しか無い（古い番号を引き継いだ表）のでノードは分からない
    for node in h.nodes.values():
        renderer = node.renderer
        if (
            renderer is not None and renderer.mesh_guid in node_names
            and renderer.mesh_file_id and renderer.mesh_file_id not in renderer_tables[renderer.mesh_guid]
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
            placement.parts = {k: replace(v) for k, v in node.model_parts.items()}
            # 1 メッシュの FBX は、ルートの Transform が FBX のノードの変換なので、読み込み側で組み立て直す（#99）
            collapsed = collapsed_root_name(tables.get(node.model_guid, {}))
            if collapsed is not None:
                placement.root_node = collapsed
                placement.node_transforms.setdefault(collapsed, {})
            elif node.collapsed_key == SOLE_NODE and SOLE_NODE in placement.node_transforms:
                # 表の無いモデルでルートを上書きした配置。畳まれた形かは読み込み側がオブジェクトの数で決める（#111）
                placement.root_node = SOLE_NODE
            items.append(placement)
            continue
        renderer = node.renderer
        if renderer is None or renderer.mesh_guid not in models:
            continue
        # FBX のノード名と同じ GameObject 名を優先する（複数のノードが 1 つのメッシュを共有する FBX もあるため）
        names = node_names.get(renderer.mesh_guid)
        table = renderer_tables.get(renderer.mesh_guid, {})
        mesh_name = table.get(renderer.mesh_file_id)
        name = unity_base_name(node.name)
        if not (names and name in names) and mesh_name and mesh_name != ROOT_NODE_NAME:
            name = mesh_name
        candidate, own = _root_candidates(h, key, renderer.mesh_guid, table, names)
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
            if names is not None:
                label = _fbx_node_name(h.nodes[root], guid, renderer_tables.get(guid, {}), names)
                # FBX の中のノードをルートにした。ルートに Renderer が無くても、最初に見たのが子の Renderer でも同じ（#183）
                if label is not None:
                    placement.node_transforms[label] = copy.deepcopy(_IDENTITY_NODE)
            # FBX を展開したルートが、Unity が畳んだ最上位のノード（#185）
            top = tops.get(guid)
            if top and not placement.node_transforms and _is_top_node(h.nodes[root], guid, top, renderer_tables.get(guid, {})):
                placement.node_transforms[top] = copy.deepcopy(_IDENTITY_NODE)
            by_root[(root, guid)] = placement
            result.append(placement)
        if name in placement.renderers:
            placement.renderers[name].relative = None
            continue  # 同じモデルに同名の GameObject があれば先のものを使う（prefab の表と同じ）
        # ルート自身の Renderer は置き直さない。FBX の最上位のノードを Unity がルートに畳んだもので、ルートの Transform が
        # ノードの変換を含むため、オブジェクトの行列との関係が中のノードと違う（置き方は #98 / #181 / #185 で決まっている）
        root_inverse = inverse_affine(placement.world) if key != root else None
        placement.renderers[name] = PlacedRenderer(
            name, list(renderer.materials), renderer.renderer_class, active[key] and renderer.enabled,
            renderer.mesh_file_id, renderer.lod_level,
            multiply(root_inverse, worlds[key]) if root_inverse is not None else None,
            renderer.shadows_only,
        )
        # ルートからこの Node までの、上書き前の行列の積で逆算する
        inverse = inverse_affine(node.scope_matrix)
        if inverse is not None and key != root:
            implied = multiply(multiply(worlds[key], inverse), h.nodes[root].scope_matrix)
            if not _close(implied, placement.world):
                placement.offsets[name] = implied
    return result


@dataclass
class PrimitivePlacement:
    """モデルファイルではないメッシュを使う Renderer。Unity の組み込みメッシュ（Quad・Plane・Cube など。Issue #190）か、
    パッケージの Unity の Mesh（``.asset`` か、prefab に埋め込んだもの。Issue #216）。

    組み込みメッシュはパッケージに無いので、読み込み側が ``core/primitives.py`` で同じ形を作る。Mesh は ``core/unity_mesh.py`` で読む。
    どちらも ``key`` の Node の Empty の下に置く。
    """

    key: int  # Renderer の Node
    kind: str  # 組み込みメッシュの名前（``primitives.BUILTIN_MESHES`` の値）。Mesh なら空
    materials: list[str | None]
    visible: bool  # GameObject が祖先も含めてアクティブで、Renderer が有効
    mesh_guid: str = ""  # Mesh を持つアセットの GUID（組み込みメッシュなら空）
    mesh_file_id: int = 0  # そのアセットの中の Mesh の fileID
    shadows_only: bool = False  # Shadows Only の Renderer（#232）


@dataclass
class SceneContents:
    """ダイアログに出すシーンの概要。"""

    placements: list[ModelPlacement]
    lights: int = 0
    cameras: int = 0
    ui_elements: int = 0  # RectTransform（読み込まない。#202）
    terrains: int = 0  # Terrain のコンポーネントの数（#202）
    terrain_data: list[str] = field(default_factory=list)  # Terrain が指す TerrainData の GUID（重複なし、現れた順。#205）
    other_renderers: int = 0  # パッケージ外のメッシュを指す Renderer（組み込みメッシュの MeshRenderer は ``primitives``）
    decals: int = 0  # DecalProjector（#165）
    primitives: list[PrimitivePlacement] = field(default_factory=list)
    unresolved_overrides: int = 0
    missing_sources: int = 0

    @property
    def model_guids(self) -> list[str]:
        seen: dict[str, None] = {}
        for p in self.placements:
            seen.setdefault(p.model_guid, None)
        return list(seen)


def summarize(
    h: Hierarchy,
    model_guids: ModelNames,
    name_tables: dict[str, dict[int, str]] | None = None,
    sole_nodes: Mapping[str, str] | None = None,
    top_nodes: Mapping[str, str] | None = None,
    mesh_assets: Container[str] = (),
) -> SceneContents:
    """シーンの概要。引数は ``placements`` と同じ。``mesh_assets`` は Unity の Mesh を持つアセットの GUID（#216）。"""
    names = _model_names(model_guids)
    models = set(names)
    found = placements(h, names, name_tables, sole_nodes, top_nodes)
    active = effective_active(h)
    primitives: list[PrimitivePlacement] = []
    other = 0
    for key, n in h.nodes.items():
        renderer = n.renderer
        if renderer is None or n.model_guid is not None or renderer.mesh_guid in models:
            continue
        kind = builtin_mesh_kind(renderer.mesh_guid, renderer.mesh_file_id)
        if kind is None and renderer.mesh_guid in mesh_assets and renderer.renderer_class == CLASS_MESH_RENDERER:
            if renderer.materials:  # マテリアルの無い MeshRenderer は描かれない（組み込みメッシュと同じ）
                primitives.append(PrimitivePlacement(
                    key, "", list(renderer.materials), active[key] and renderer.enabled, renderer.mesh_guid, renderer.mesh_file_id,
                    renderer.shadows_only,
                ))
            continue
        if kind is not None and renderer.renderer_class == CLASS_MESH_RENDERER:
            if not renderer.materials:
                # マテリアルが 1 つも無い MeshRenderer は Unity では何も描かれない（スクリプトの目印に Cube を付けた
                # GameObject など）ので作らない。読み込まない Renderer の警告にも数えない
                continue
            primitives.append(PrimitivePlacement(
                key, kind, list(renderer.materials), active[key] and renderer.enabled, shadows_only=renderer.shadows_only,
            ))
        else:
            other += 1
    return SceneContents(
        found,
        lights=h.counts[CLASS_LIGHT],
        cameras=h.counts[CLASS_CAMERA],
        ui_elements=h.counts[CLASS_RECT_TRANSFORM],
        terrains=h.counts[CLASS_TERRAIN],
        terrain_data=list(dict.fromkeys(
            guid for n in h.nodes.values() for class_id, body in n.components.values()
            if class_id == CLASS_TERRAIN and (guid := terrain_data_ref(body))
        )),
        other_renderers=other,
        primitives=primitives,
        decals=sum(1 for n in h.nodes.values() for class_id, _ in n.components.values() if class_id == CLASS_MONO_BEHAVIOUR),
        unresolved_overrides=h.unresolved_overrides,
        missing_sources=h.missing_sources,
    )


@dataclass(frozen=True)
class ModelCatalog:
    """パッケージのモデルについて、展開と配置に使う表をまとめたもの。

    ``Expander`` / ``placements`` / ``summarize`` には、この 4 つの表を別々の引数で渡す。読み込み側で毎回 4 つを
    並べて渡し、prefab とシーンで 2 回作っていたので、1 つにまとめて使い回す（#198）。
    """

    names: Mapping[str, str] = field(default_factory=dict)  # モデルの GUID → ルートの名前（ファイル名から拡張子を除いたもの）
    recycle_names: Mapping[str, dict[int, str]] = field(default_factory=dict)  # 古い形式の .meta の fileIDToRecycleName（#53）
    sole_nodes: Mapping[str, str] = field(default_factory=dict)  # FBX のノードが 1 つだけのモデル → そのノード名（#181）
    top_nodes: Mapping[str, str] = field(default_factory=dict)  # FBX の最上位のノードが 1 つだけのモデル → そのノード名（#185）
    mesh_assets: frozenset[str] = frozenset()  # Unity の Mesh を持つアセット（.asset と、Mesh を埋め込んだ prefab・シーン。#216）

    def expander(self, read_asset: Callable[[str], RawAsset | None]) -> Expander:
        return Expander(read_asset, dict(self.names), dict(self.recycle_names))

    def placements(self, h: Hierarchy) -> list[ModelPlacement]:
        return placements(h, self.names, self.recycle_names, self.sole_nodes, self.top_nodes)

    def summarize(self, h: Hierarchy) -> SceneContents:
        return summarize(h, self.names, self.recycle_names, self.sole_nodes, self.top_nodes, self.mesh_assets)
