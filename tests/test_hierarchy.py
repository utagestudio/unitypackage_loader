"""シーン・prefab の階層の展開と、モデルの配置。

数値は、合成 FBX を Unity 6 で 3 通りに置いたシーン（Issue #48 の調査）に合わせてある。
A = FBX を中に置いた prefab（Nested）、B = 空の親 Holder の下に FBX を直置き、C = FBX を展開した prefab（Unpacked）
を非一様スケールで置き、中の Child の位置を上書き。Unity が書き出した突起の頂点のワールド座標を、
Blender の FBX インポーターで原点に読んだときの頂点の座標から再現できることを確かめる。
"""

import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.hierarchy import (
    Expander,
    components,
    Hierarchy,
    Node,
    effective_active,
    parse_asset,
    placements,
    summarize,
    world_matrices,
)
from unitypackage_loader.core.transform import transform_point, unity_to_blender

MODEL = "a" * 32
NESTED = "b" * 32
UNPACKED = "c" * 32
MISSING = "e" * 32
LOOP = "f" * 32
MAT_A = "1" * 32
MAT_B = "2" * 32

ROOT_TRANSFORM = -8679921383154817045
ROOT_GAME_OBJECT = 919132149155446097

# Blender の FBX インポーターで原点に読んだときの、突起の頂点の座標
SPIKE_TIP = (1.4588, 1.9392, 0.9)
CHILD_TIP = (-0.5821, 0.9568, 1.2883)

HEADER = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"


def game_object(file_id, name, active=1):
    return f"--- !u!1 &{file_id}\nGameObject:\n  m_Name: {name}\n  m_IsActive: {active}\n"


def transform(file_id, go, father=0, pos=(0, 0, 0), rot=(0, 0, 0, 1), scale=(1, 1, 1), class_id=4, kind="Transform"):
    return (
        f"--- !u!{class_id} &{file_id}\n{kind}:\n  m_GameObject: {{fileID: {go}}}\n"
        f"  m_LocalRotation: {{x: {rot[0]}, y: {rot[1]}, z: {rot[2]}, w: {rot[3]}}}\n"
        f"  m_LocalPosition: {{x: {pos[0]}, y: {pos[1]}, z: {pos[2]}}}\n"
        f"  m_LocalScale: {{x: {scale[0]}, y: {scale[1]}, z: {scale[2]}}}\n"
        f"  m_Father: {{fileID: {father}}}\n"
    )


def mesh_renderer(filter_id, renderer_id, go, mesh_guid, mat, mesh_file_id=4300000):
    return (
        f"--- !u!33 &{filter_id}\nMeshFilter:\n  m_GameObject: {{fileID: {go}}}\n"
        f"  m_Mesh: {{fileID: {mesh_file_id}, guid: {mesh_guid}, type: 3}}\n"
        f"--- !u!23 &{renderer_id}\nMeshRenderer:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n"
        f"  m_Materials:\n  - {{fileID: 2100000, guid: {mat}, type: 2}}\n"
    )


def mod(target, guid, path, value="", reference="{fileID: 0}"):
    return (
        f"    - target: {{fileID: {target}, guid: {guid}, type: 3}}\n"
        f"      propertyPath: {path}\n      value: {value}\n      objectReference: {reference}\n"
    )


def instance(file_id, source, parent, mods, removed_game_objects=""):
    removed = f"    m_RemovedGameObjects:\n{removed_game_objects}" if removed_game_objects else "    m_RemovedGameObjects: []\n"
    return (
        f"--- !u!1001 &{file_id}\nPrefabInstance:\n  m_Modification:\n"
        f"    m_TransformParent: {{fileID: {parent}}}\n    m_Modifications:\n{''.join(mods)}"
        f"    m_RemovedComponents: []\n{removed}"
        f"  m_SourcePrefab: {{fileID: 100100000, guid: {source}, type: 3}}\n"
    )


def stripped_transform(file_id, source_file_id, source_guid, instance_id, class_id=4, kind="Transform"):
    return (
        f"--- !u!{class_id} &{file_id} stripped\n{kind}:\n"
        f"  m_CorrespondingSourceObject: {{fileID: {source_file_id}, guid: {source_guid}, type: 3}}\n"
        f"  m_PrefabInstance: {{fileID: {instance_id}}}\n"
    )


def lod_group(file_id, go, levels, enabled=1):
    """LODGroup。``levels`` は段の順に並べた Renderer の fileID のリスト。"""
    body = ""
    for index, renderers in enumerate(levels):
        body += f"  - screenRelativeHeight: {0.5 / (index + 1)}\n    fadeTransitionWidth: 0\n"
        if renderers:
            body += "    renderers:\n" + "".join(f"    - renderer: {{fileID: {r}}}\n" for r in renderers)
        else:
            body += "    renderers: []\n"
    return (
        f"--- !u!205 &{file_id}\nLODGroup:\n  m_GameObject: {{fileID: {go}}}\n  serializedVersion: 2\n"
        f"  m_Size: 2\n  m_FadeMode: 0\n  m_LODs:\n{body}  m_Enabled: {enabled}\n"
    )


UNPACKED_PREFAB = HEADER + "".join([
    game_object(100, "Unpacked"),
    transform(101, 100),
    game_object(200, "Group"),
    transform(201, 200, father=101, pos=(0, 0, -1), rot=(-0.5735765, 0, 0, 0.819152), scale=(100, 99.99999, 99.99999)),
    game_object(300, "Child"),
    transform(301, 300, father=201, pos=(0, 0, 0.0050000004), rot=(0, -0.13052621, 0, 0.9914449), scale=(0.5, 0.50000006, 0.50000006)),
    mesh_renderer(302, 303, 300, MODEL, MAT_A),
    game_object(400, "Spike"),
    transform(401, 400, father=101, pos=(-0.5, 0, 0), rot=(-0.68301266, -0.18301271, -0.18301271, 0.6830127), scale=(100, 100, 100)),
    mesh_renderer(402, 403, 400, MODEL, MAT_A),
    game_object(500, "Extra"),
    transform(501, 500, father=101),
    mesh_renderer(502, 503, 500, MODEL, MAT_A),
])

NESTED_PREFAB = HEADER + "".join([
    game_object(10, "Nested"),
    transform(11, 10),
    instance(20, MODEL, 11, [
        mod(ROOT_TRANSFORM, MODEL, "m_LocalPosition.x", 0.3),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalRotation.y", 0.38268346),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalRotation.w", 0.9238795),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalScale.x", 2),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalScale.y", 2),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalScale.z", 2),
    ]),
    stripped_transform(21, ROOT_TRANSFORM, MODEL, 20),
])

LOOP_PREFAB = HEADER + game_object(1, "Loop") + transform(2, 1) + instance(3, LOOP, 2, [])

SCENE = HEADER + "".join([
    # B: 空の親 Holder の下に FBX を直置き
    game_object(1000, "Holder"),
    transform(1001, 1000, pos=(-2, 0, 1), rot=(0, 0.7071068, 0, 0.7071068), scale=(1.5, 1.5, 1.5)),
    instance(2000, MODEL, 1001, [
        mod(ROOT_TRANSFORM, MODEL, "m_LocalPosition.y", 1),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalRotation.z", 0.38268346),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalRotation.w", 0.9238795),
        mod(ROOT_GAME_OBJECT, MODEL, "m_Name", "B"),
    ]),
    stripped_transform(2001, ROOT_TRANSFORM, MODEL, 2000),
    # A: FBX を中に置いた prefab
    instance(3000, NESTED, 0, [
        mod(11, NESTED, "m_LocalPosition.x", 1),
        mod(11, NESTED, "m_LocalPosition.y", 2),
        mod(11, NESTED, "m_LocalPosition.z", 3),
        mod(11, NESTED, "m_LocalRotation.x", 0.12767945),
        mod(11, NESTED, "m_LocalRotation.y", 0.14487813),
        mod(11, NESTED, "m_LocalRotation.z", 0.23929834),
        mod(11, NESTED, "m_LocalRotation.w", 0.9515485),
        mod(10, NESTED, "m_Name", "A"),
    ]),
    # C: 展開した prefab。中の Child を動かし、マテリアルを差し替え、Extra を削除
    instance(4000, UNPACKED, 0, [
        mod(101, UNPACKED, "m_LocalPosition.z", -3),
        mod(101, UNPACKED, "m_LocalRotation.y", 1),
        mod(101, UNPACKED, "m_LocalRotation.w", -0.00000004371139),
        mod(101, UNPACKED, "m_LocalScale.y", 2),
        mod(301, UNPACKED, "m_LocalPosition.y", 0.5),
        mod(303, UNPACKED, "m_Materials.Array.data[0]", "", f"{{fileID: 2100000, guid: {MAT_B}, type: 2}}"),
        mod(100, UNPACKED, "m_Name", "C"),
    ], removed_game_objects=f"    - {{fileID: 500, guid: {UNPACKED}, type: 3}}\n"),
    # 元がパッケージに無い prefab
    instance(5000, MISSING, 0, []),
    # 非アクティブな親の下の FBX。中のオブジェクトへの上書きは当てられない
    game_object(6000, "Hidden", active=0),
    transform(6001, 6000),
    instance(6002, MODEL, 6001, [
        mod(123456789, MODEL, "m_LocalRotation.x", 0.5),
        mod(987654321, MODEL, "m_Materials.Array.data[0]", "", f"{{fileID: 2100000, guid: {MAT_B}, type: 2}}"),
        mod(987654321, MODEL, "m_AABB.m_Center.x", 0.1),  # 位置・マテリアル以外は数えない
    ]),
    # UI、ライト・カメラ、Unity 組み込みのメッシュ
    game_object(7000, "Canvas"),
    transform(7001, 7000, class_id=224, kind="RectTransform"),
    "--- !u!223 &7002\nCanvas:\n  m_GameObject: {fileID: 7000}\n",
    game_object(8000, "Light"),
    transform(8001, 8000),
    "--- !u!108 &8002\nLight:\n  m_GameObject: {fileID: 8000}\n",
    "--- !u!20 &8003\nCamera:\n  m_GameObject: {fileID: 8000}\n",
    game_object(9000, "Cube"),
    transform(9001, 9000),
    mesh_renderer(9002, 9003, 9000, "0000000000000000e000000000000000", MAT_A, mesh_file_id=10202),
])


def expander():
    assets = {NESTED: NESTED_PREFAB, UNPACKED: UNPACKED_PREFAB, LOOP: LOOP_PREFAB}
    return Expander(lambda guid: parse_asset(assets[guid]) if guid in assets else None, {MODEL: "Probe"})


def blender_point(world, point):
    return transform_point(unity_to_blender(world), point)


class PlacementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hierarchy = expander().expand_raw(parse_asset(SCENE))
        cls.placements = placements(cls.hierarchy, [MODEL])
        cls.by_name = {cls.hierarchy.nodes[p.root].name: p for p in cls.placements}

    def assert_point(self, actual, expected):
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e, places=3)

    def test_found_every_placement_in_order(self):
        self.assertEqual([self.hierarchy.nodes[p.root].name for p in self.placements], ["B", "Probe", "C", "Probe"])
        self.assertTrue(all(p.model_guid == MODEL for p in self.placements))

    def test_model_under_empty_parent(self):
        self.assert_point(blender_point(self.by_name["B"].world, SPIKE_TIP), (4.9088, -3.5019, 0.9073))

    def test_model_inside_nested_prefab(self):
        nested = self.placements[1]
        self.assert_point(blender_point(nested.world, SPIKE_TIP), (3.7824, -3.9004, 1.4346))
        self.assert_point(blender_point(nested.world, CHILD_TIP), (1.0050, -1.8401, 4.4622))
        self.assertEqual(nested.renderers, {})  # モデルの中の Renderer は stripped で、名前が分からない

    def test_unpacked_prefab_uses_its_root(self):
        unpacked = self.by_name["C"]
        self.assert_point(blender_point(unpacked.world, SPIKE_TIP), (-1.4588, 1.0608, 1.8))
        self.assertEqual(set(unpacked.renderers), {"Child", "Spike"})  # 削除した Extra は含まない

    def test_moved_inner_node_gets_offset(self):
        unpacked = self.by_name["C"]
        self.assertEqual(set(unpacked.offsets), {"Child"})
        self.assert_point(blender_point(unpacked.offsets["Child"], CHILD_TIP), (0.5821, -44.9414, 36.7786))

    def test_material_override_on_unpacked_renderer(self):
        unpacked = self.by_name["C"]
        self.assertEqual(unpacked.renderers["Child"].materials, [MAT_B])
        self.assertEqual(unpacked.renderers["Spike"].materials, [MAT_A])

    def test_inactive_parent_hides_placement(self):
        self.assertTrue(self.by_name["B"].active)
        self.assertFalse(self.placements[3].active)

    def test_overrides_inside_model_are_counted(self):
        self.assertEqual(self.hierarchy.unresolved_overrides, 2)

    def test_summary_counts(self):
        contents = summarize(self.hierarchy, [MODEL])
        self.assertEqual(len(contents.placements), 4)
        self.assertEqual(contents.model_guids, [MODEL])
        self.assertEqual((contents.lights, contents.cameras, contents.ui_elements), (1, 1, 1))
        self.assertEqual(contents.other_renderers, 1)  # Unity 組み込みの Cube
        self.assertEqual(contents.missing_sources, 1)


class DuplicateNameTest(unittest.TestCase):
    """Unity のエディタで複製したオブジェクト（「Rock (12)」）は、名前の番号を外して扱う。"""

    SCENE = HEADER + "".join([
        game_object(10, "Rock"),
        transform(11, 10),
        mesh_renderer(12, 13, 10, MODEL, MAT_A),
        game_object(20, "Rock (1)"),
        transform(21, 20, pos=(2, 0, 0)),
        mesh_renderer(22, 23, 20, MODEL, MAT_A),
        game_object(30, "Rock (12)"),
        transform(31, 30, pos=(4, 0, 0)),
        mesh_renderer(32, 33, 30, MODEL, MAT_B),
        game_object(40, "Rock (Old)"),
        transform(41, 40, pos=(6, 0, 0)),
        mesh_renderer(42, 43, 40, MODEL, MAT_A),
    ])

    def test_numbers_are_removed_from_renderer_names(self):
        found = placements(expander().expand_raw(parse_asset(self.SCENE)), [MODEL])
        self.assertEqual([list(p.renderers) for p in found], [["Rock"], ["Rock"], ["Rock"], ["Rock (Old)"]])

    def test_same_materials_share_signature(self):
        found = placements(expander().expand_raw(parse_asset(self.SCENE)), [MODEL])
        self.assertEqual(found[0].signature(), found[1].signature())
        self.assertNotEqual(found[0].signature(), found[2].signature())  # マテリアルが違う


class MeshNameTest(unittest.TestCase):
    """展開した prefab で GameObject の名前を変えていても、.meta の表でメッシュ参照から名前を引く（#58）。"""

    SCENE = HEADER + "".join([
        game_object(10, "House"),  # FBX のノード名は House_LOD0 だが、prefab で名前を変えてある
        transform(11, 10),
        mesh_renderer(12, 13, 10, MODEL, MAT_A, mesh_file_id=4300104),
        game_object(20, "Door"),
        transform(21, 20, father=11),
        mesh_renderer(22, 23, 20, MODEL, MAT_B, mesh_file_id=4300999),  # 表に無い
    ])

    def test_renderer_named_by_mesh(self):
        h = expander().expand_raw(parse_asset(self.SCENE))
        placement = placements(h, [MODEL], {MODEL: {4300104: "House_LOD0"}})[0]
        self.assertEqual(set(placement.renderers), {"House_LOD0", "Door"})
        self.assertEqual(placement.renderers["House_LOD0"].materials, [MAT_A])
        self.assertEqual(placement.renderers["Door"].mesh_file_id, 4300999)

    def test_without_table_uses_game_object_names(self):
        h = expander().expand_raw(parse_asset(self.SCENE))
        self.assertEqual(set(placements(h, [MODEL])[0].renderers), {"House", "Door"})


class ModelRootTest(unittest.TestCase):
    """.meta の表のあるモデルで、Renderer ごとにモデルのルートを決める（Issue #60）。

    シーンの共通の親の下に FBX から切り離した小物を並べると、同じモデルの Renderer が 1 つの配置に潰れていた。
    """

    SINGLE = "d" * 32  # 1 メッシュの FBX
    PARTIAL = "9" * 32  # 古い番号を引き継いだ、一部の行しか無い表
    TABLES = {
        MODEL: {100000: "//RootNode", 100002: "Part", 100004: "Lid", 4300000: "Part", 4300002: "Lid"},
        SINGLE: {100000: "//RootNode", 4300000: "Pot"},
        PARTIAL: {100000: "//RootNode", 4300000: "Wall"},
    }

    SCENE = HEADER + "".join([
        # 部屋: 同じ部品の複製と、別のモデルが並ぶ入れ物
        game_object(10, "Room"),
        transform(11, 10),
        game_object(20, "Part (1)"),
        transform(21, 20, father=11, pos=(1, 0, 0)),
        mesh_renderer(22, 23, 20, MODEL, MAT_A, mesh_file_id=4300000),
        game_object(30, "Part (2)"),
        transform(31, 30, father=11, pos=(2, 0, 0)),
        mesh_renderer(32, 33, 30, MODEL, MAT_A, mesh_file_id=4300000),
        game_object(40, "Pot"),
        transform(41, 40, father=11, pos=(3, 0, 0)),
        mesh_renderer(42, 43, 40, SINGLE, MAT_A, mesh_file_id=4300000),
        game_object(50, "Lid (1)"),
        transform(51, 50, father=11, pos=(4, 0, 0)),
        mesh_renderer(52, 53, 50, MODEL, MAT_A, mesh_file_id=4300002),
        # FBX を展開して名前だけ変えたルート
        game_object(60, "Kit"),
        transform(61, 60, pos=(0, 0, 5)),
        game_object(70, "Part"),
        transform(71, 70, father=61),
        mesh_renderer(72, 73, 70, MODEL, MAT_A, mesh_file_id=4300000),
        game_object(80, "Lid"),
        transform(81, 80, father=61, pos=(0, 0.5, 0)),
        mesh_renderer(82, 83, 80, MODEL, MAT_B, mesh_file_id=4300002),
        # 部品が 1 つだけで、別のモデルと並ぶ入れ物
        game_object(90, "Shelf"),
        transform(91, 90, pos=(0, 1, 0)),
        game_object(100, "Lid (2)"),
        transform(101, 100, father=91, pos=(5, 0, 0)),
        mesh_renderer(102, 103, 100, MODEL, MAT_A, mesh_file_id=4300002),
        game_object(110, "Pot (1)"),
        transform(111, 110, father=91),
        mesh_renderer(112, 113, 110, SINGLE, MAT_A, mesh_file_id=4300000),
        # モデルと同じ名前のルートに別のモデルを足したもの（照明にろうそくを足した prefab など）
        game_object(120, "Probe"),
        transform(121, 120, pos=(0, 0, -5)),
        game_object(130, "Part"),
        transform(131, 130, father=121),
        mesh_renderer(132, 133, 130, MODEL, MAT_A, mesh_file_id=4300000),
        game_object(140, "Pot (2)"),
        transform(141, 140, father=121),
        mesh_renderer(142, 143, 140, SINGLE, MAT_A, mesh_file_id=4300000),
        # 名前を変えた FBX のノード（Door = Part）の子に、別のノード
        game_object(150, "Box"),
        transform(151, 150, pos=(0, 2, 0)),
        game_object(160, "Door"),
        transform(161, 160, father=151),
        mesh_renderer(162, 163, 160, MODEL, MAT_A, mesh_file_id=4300000),
        game_object(170, "Lid"),
        transform(171, 170, father=161),
        mesh_renderer(172, 173, 170, MODEL, MAT_A, mesh_file_id=4300002),
        # 表に無いメッシュ（LOD1）を指す Renderer があるので、表を当てにしない
        game_object(180, "Wall"),
        transform(181, 180, pos=(0, 3, 0)),
        game_object(190, "Wall_LOD0"),
        transform(191, 190, father=181),
        mesh_renderer(192, 193, 190, PARTIAL, MAT_A, mesh_file_id=4300000),
        game_object(200, "Wall_LOD1"),
        transform(201, 200, father=181),
        mesh_renderer(202, 203, 200, PARTIAL, MAT_A, mesh_file_id=-123456789),
        # 1 メッシュの FBX を、名前を変えた GameObject がメッシュ参照で使う（#98）
        game_object(210, "Jar"),
        transform(211, 210, pos=(0, 4, 0)),
        mesh_renderer(212, 213, 210, SINGLE, MAT_A, mesh_file_id=4300000),
    ])

    @classmethod
    def setUpClass(cls):
        names = {MODEL: "Probe", cls.SINGLE: "Pot", cls.PARTIAL: "Wall"}
        cls.hierarchy = Expander(lambda guid: None, names).expand_raw(parse_asset(cls.SCENE))
        cls.placements = placements(cls.hierarchy, names, cls.TABLES)

    def roots(self, model):
        return [self.hierarchy.nodes[p.root].name for p in self.placements if p.model_guid == model]

    def by_root(self, name):
        return next(p for p in self.placements if self.hierarchy.nodes[p.root].name == name)

    def test_copies_in_a_room_are_placed_one_by_one(self):
        self.assertEqual(self.roots(MODEL), ["Part (1)", "Part (2)", "Lid (1)", "Kit", "Lid (2)", "Probe", "Box"])
        self.assertEqual(self.roots(self.SINGLE), ["Pot", "Pot (1)", "Pot (2)", "Jar"])
        self.assertEqual([round(v, 6) for v in transform_point(self.by_root("Part (2)").world, (0, 0, 0))], [2, 0, 0])
        self.assertEqual(set(self.by_root("Lid (1)").renderers), {"Lid"})

    def test_node_used_as_root_is_reset_to_identity(self):
        # Unity の GameObject の行列がノードの変換を含むので、Blender では元の変換を掛けない
        identity = {"position": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]}
        self.assertEqual(self.by_root("Part (1)").node_transforms, {"Part": identity})
        self.assertEqual(self.by_root("Lid (2)").node_transforms, {"Lid": identity})
        # 1 メッシュの FBX も、Unity が //RootNode に畳んだノードなのでメッシュ名で戻す（#98）
        self.assertEqual(self.by_root("Pot").node_transforms, {"Pot": identity})
        self.assertEqual(self.by_root("Jar").node_transforms, {"Pot": identity})  # 名前を変えてあってもメッシュ名で引く
        self.assertEqual(set(self.by_root("Jar").renderers), {"Pot"})

    def test_unpacked_root_keeps_its_parts_together(self):
        kit = self.by_root("Kit")
        self.assertEqual(set(kit.renderers), {"Part", "Lid"})
        self.assertEqual((kit.node_transforms, kit.offsets), ({}, {}))
        self.assertEqual(set(self.by_root("Probe").renderers), {"Part"})

    def test_renamed_node_is_followed_by_its_mesh(self):
        self.assertEqual(set(self.by_root("Box").renderers), {"Part", "Lid"})

    def test_partial_table_falls_back_to_the_top_ancestor(self):
        self.assertEqual(self.roots(self.PARTIAL), ["Wall"])
        self.assertEqual(set(self.by_root("Wall").renderers), {"Wall", "Wall_LOD1"})


class CollapsedModelRootTest(unittest.TestCase):
    """1 メッシュの FBX をモデルの PrefabInstance で置いた配置（Issue #99）。

    この形の FBX では Unity のモデルのルートの Transform の値が FBX のノードの変換で、シーンの上書きはその成分を
    置き換える（上書きしなかった成分はノードの値のまま）。ノードの値は FBX を読むまで分からないので、
    畳まれたノードの名前を ``root_node`` に、上書きした成分だけを ``node_transforms`` に入れて読み込み側へ渡す。
    """

    TABLE = {100000: "//RootNode", 400000: "//RootNode", 2300000: "//RootNode", 3300000: "//RootNode", 4300000: "Slab"}
    # 位置と回転だけを上書きし、スケールは触らない（Unity のエディタで動かしたときと同じ形）
    SCENE = HEADER + instance(2000, MODEL, 0, [
        mod(ROOT_TRANSFORM, MODEL, "m_LocalPosition.x", 2),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalPosition.y", 0.5),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalPosition.z", -6),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalRotation.y", 0.25881905),
        mod(ROOT_TRANSFORM, MODEL, "m_LocalRotation.w", 0.96592583),
        mod(ROOT_GAME_OBJECT, MODEL, "m_Name", "SlabInstance"),
    ])

    def placement(self, table=None):
        exp = Expander(lambda g: None, {MODEL: "Slab"}, {MODEL: table} if table else None)
        h = exp.expand_raw(parse_asset(self.SCENE))
        return h, placements(h, {MODEL: "Slab"}, {MODEL: table} if table else None)[0]

    def test_overridden_components_are_recorded_for_the_collapsed_node(self):
        h, placement = self.placement(self.TABLE)
        self.assertEqual(h.nodes[placement.root].name, "SlabInstance")
        self.assertEqual(placement.root_node, "Slab")
        self.assertEqual(placement.node_transforms, {"Slab": {
            "position": [2.0, 0.5, -6.0], "rotation": [None, 0.25881905, None, 0.96592583],
        }})

    def test_model_with_several_nodes_is_left_alone(self):
        table = self.TABLE | {100002: "Lid", 400002: "Lid", 4300002: "Lid"}
        _, placement = self.placement(table)
        self.assertEqual((placement.root_node, placement.node_transforms), ("", {}))

    def test_without_a_table_the_node_is_unknown(self):
        _, placement = self.placement()
        self.assertEqual((placement.root_node, placement.node_transforms), ("", {}))
        self.assertEqual([round(v, 6) for v in transform_point(placement.world, (0, 0, 0))], [2, 0.5, -6])


class LegacyFormatTest(unittest.TestCase):
    """Unity 2018.2 以前の形式（Prefab / m_ParentPrefab / m_PrefabParentObject / m_PrefabInternal、ルートは 400000）。"""

    PREFAB = HEADER + (
        # prefab アセット自身の記録。元の GUID を持たないので配置ではない
        "--- !u!1001 &100100000\nPrefab:\n  m_Modification:\n    m_TransformParent: {fileID: 0}\n"
        "    m_Modifications: []\n    m_RemovedComponents: []\n  m_ParentPrefab: {fileID: 0}\n"
        "  m_RootGameObject: {fileID: 1}\n  m_IsPrefabParent: 1\n"
    ) + game_object(1, "Crate") + transform(2, 1) + mesh_renderer(3, 4, 1, MODEL, MAT_A)

    @staticmethod
    def legacy_instance(file_id, source, parent, mods):
        return (
            f"--- !u!1001 &{file_id}\nPrefab:\n  serializedVersion: 2\n  m_Modification:\n"
            f"    m_TransformParent: {{fileID: {parent}}}\n    m_Modifications:\n{''.join(mods)}"
            f"    m_RemovedComponents: []\n  m_ParentPrefab: {{fileID: 100100000, guid: {source}, type: 3}}\n"
            "  m_IsPrefabParent: 0\n"
        )

    def scene(self):
        return HEADER + "".join([
            game_object(1000, "Room"),
            transform(1001, 1000, pos=(0, 0, 5)),
            self.legacy_instance(2000, MODEL, 1001, [
                mod(400000, MODEL, "m_LocalPosition.x", 2),
                mod(100000, MODEL, "m_Name", "Pot"),
                mod(2300000, MODEL, "m_Materials.Array.data[0]", "", f"{{fileID: 2100000, guid: {MAT_B}, type: 2}}"),
            ]),
            "--- !u!4 &2001 stripped\nTransform:\n"
            f"  m_PrefabParentObject: {{fileID: 400000, guid: {MODEL}, type: 3}}\n  m_PrefabInternal: {{fileID: 2000}}\n",
            game_object(3000, "Flower"),  # FBX の配置の子に付けた、シーン側の GameObject
            transform(3001, 3000, father=2001, pos=(0, 1, 0)),
            self.legacy_instance(4000, "d" * 32, 0, [mod(2, "d" * 32, "m_LocalPosition.z", -1)]),
        ])

    def test_legacy_model_and_prefab_instances_are_placed(self):
        assets = {"d" * 32: self.PREFAB}
        exp = Expander(lambda g: parse_asset(assets[g]) if g in assets else None, {MODEL: "Probe"})
        h = exp.expand_raw(parse_asset(self.scene()))
        found = placements(h, [MODEL])
        self.assertEqual([h.nodes[p.root].name for p in found], ["Pot", "Crate"])
        self.assertEqual(transform_point(found[0].world, (0, 0, 0)), (2.0, 0.0, 5.0))
        self.assertEqual(transform_point(found[1].world, (0, 0, 0)), (0.0, 0.0, -1.0))
        self.assertEqual(h.missing_sources, 0)
        self.assertEqual(h.unresolved_overrides, 1)  # モデルの中の Renderer（2300000）へのマテリアルの上書き

    def test_recycle_table_resolves_overrides_inside_model(self):
        # 1 メッシュの FBX: Renderer（2300000）は //RootNode に載り、Blender ではメッシュ名 Pot のオブジェクトになる
        table = {100000: "//RootNode", 400000: "//RootNode", 2300000: "//RootNode", 4300000: "Pot"}
        exp = Expander(lambda g: None, {MODEL: "Probe"}, {MODEL: table})
        h = exp.expand_raw(parse_asset(self.scene()))
        pot = placements(h, [MODEL])[0]
        self.assertEqual(pot.renderers["Pot"].materials, [MAT_B])
        self.assertEqual(h.unresolved_overrides, 0)

    def test_recycle_table_resolves_inner_transforms(self):
        table = {100000: "//RootNode", 400000: "//RootNode", 400002: "Lid", 2300002: "Lid", 4300002: "Lid"}
        scene = HEADER + self.legacy_instance(2000, MODEL, 0, [
            mod(400002, MODEL, "m_LocalRotation.x", -0.5),
            mod(400002, MODEL, "m_LocalRotation.w", 0.866),
            mod(400002, MODEL, "m_LocalPosition.y", 0.8),
            mod(2300002, MODEL, "m_Materials.Array.data[1]", "", f"{{fileID: 2100000, guid: {MAT_A}, type: 2}}"),
            mod(123, MODEL, "m_LocalPosition.x", 1),  # 表に無い
        ])
        exp = Expander(lambda g: None, {MODEL: "Probe"}, {MODEL: table})
        h = exp.expand_raw(parse_asset(scene))
        placement = placements(h, [MODEL])[0]
        self.assertEqual(placement.node_transforms["Lid"], {"rotation": [-0.5, None, None, 0.866], "position": [None, 0.8, None]})
        self.assertEqual(placement.renderers["Lid"].materials, [None, MAT_A])
        self.assertEqual(h.unresolved_overrides, 1)

    def test_child_of_legacy_model_instance_uses_stripped_alias(self):
        exp = Expander(lambda g: None, {MODEL: "Probe"})
        h = exp.expand_raw(parse_asset(self.scene()))
        flower = next(n for n in h.nodes.values() if n.name == "Flower")
        self.assertEqual(h.nodes[flower.parent].name, "Pot")
        self.assertEqual(transform_point(world_matrices(h)[flower.key], (0, 0, 0)), (2.0, 1.0, 5.0))


class ComponentTest(unittest.TestCase):
    """ライト・カメラは中身を持ち、prefab の上書き（強さ・色・有効）を当ててから配置する。"""

    LAMP = HEADER + game_object(1, "Lamp") + transform(2, 1) + (
        "--- !u!108 &3\nLight:\n  m_GameObject: {fileID: 1}\n  m_Enabled: 1\n  m_Type: 2\n  m_Intensity: 1\n"
        "  m_Color: {r: 1, g: 1, b: 1, a: 1}\n  m_Range: 10\n"
    )
    SCENE = HEADER + "".join([
        instance(10, "d" * 32, 0, [
            mod(2, "d" * 32, "m_LocalPosition.y", 3),
            mod(3, "d" * 32, "m_Intensity", 2.5),
            mod(3, "d" * 32, "m_Color.g", 0.5),
        ]),
        instance(20, "d" * 32, 0, [mod(3, "d" * 32, "m_Enabled", 0)]),
        game_object(30, "Main Camera"),
        transform(31, 30, pos=(0, 1, -10)),
        "--- !u!20 &32\nCamera:\n  m_GameObject: {fileID: 30}\n  m_Enabled: 1\n  field of view: 40\n",
    ])

    def setUp(self):
        exp = Expander(lambda g: parse_asset(self.LAMP) if g == "d" * 32 else None, {})
        self.found = components(exp.expand_raw(parse_asset(self.SCENE)))

    def test_overrides_are_applied_to_prefab_light(self):
        lamp = self.found[1]  # シーンが直接持つカメラが先、差し込んだ prefab のライトが後
        self.assertEqual((lamp.name, lamp.class_id, lamp.active), ("Lamp", 108, True))
        self.assertEqual(lamp.body["m_Intensity"], 2.5)
        self.assertEqual(lamp.body["m_Color"], {"r": 1, "g": 0.5, "b": 1, "a": 1})
        self.assertEqual(transform_point(lamp.world, (0, 0, 0)), (0.0, 3.0, 0.0))

    def test_instances_do_not_share_bodies(self):
        second = self.found[2]
        self.assertEqual(second.body["m_Intensity"], 1)  # 1 つ目の上書きが漏れない
        self.assertFalse(second.active)  # m_Enabled 0

    def test_scene_camera(self):
        camera = self.found[0]
        self.assertEqual((camera.name, camera.class_id, camera.body["field of view"]), ("Main Camera", 20, 40))


    def test_override_on_removed_game_object_is_ignored(self):
        # Unity は GameObject を消しても、その部品への古い上書きを m_Modifications に残すことがある（#68）
        scene = HEADER + instance(
            10, "d" * 32, 0, [mod(3, "d" * 32, "m_Intensity", 2.5)],
            removed_game_objects=f"    - {{fileID: 1, guid: {'d' * 32}, type: 3}}\n",
        )
        exp = Expander(lambda g: parse_asset(self.LAMP) if g == "d" * 32 else None, {})
        hierarchy = exp.expand_raw(parse_asset(scene))
        self.assertEqual(components(hierarchy), [])
        self.assertEqual(hierarchy.components, {})

class RobustnessTest(unittest.TestCase):
    def test_self_referencing_prefab_stops(self):
        h = expander().expand_asset(LOOP)
        self.assertIsNotNone(h)
        self.assertEqual(h.missing_sources, 1)

    def test_parent_cycle_terminates(self):
        h = Hierarchy()
        for key, parent in ((1, 2), (2, 1)):
            h.nodes[key] = Node(key, str(key), [1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0], [1.0, 1.0, 1.0], parent=parent)
        self.assertEqual(set(world_matrices(h)), {1, 2})
        self.assertEqual(set(effective_active(h)), {1, 2})

    def test_broken_values_fall_back_to_defaults(self):
        raw = parse_asset(HEADER + game_object(1, "X") + (
            "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
            "  m_LocalPosition: {x: nan, y: abc, z: 1}\n  m_LocalRotation: 5\n  m_Father: {fileID: 0}\n"
        ))
        t = raw.transforms[2]
        self.assertEqual((t.position, t.rotation, t.scale), ([0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0], [1.0, 1.0, 1.0]))

    def test_models_without_package_source_are_skipped(self):
        h = Expander(lambda guid: None, {}).expand_raw(parse_asset(SCENE))
        self.assertEqual(placements(h, [MODEL]), [])
        self.assertEqual(h.missing_sources, 5)  # B、A、C、元の無い prefab、Hidden の中の FBX



class NestingLimitCacheTests(unittest.TestCase):
    """深さの上限で打ち切った展開結果を、浅い位置で使い回さない（#78 L10）。"""

    def test_truncated_result_is_expanded_again_from_a_shallower_position(self):
        from unitypackage_loader.core.hierarchy import MAX_NESTING

        count = MAX_NESTING + 2
        guids = [f"{i:032x}" for i in range(0x100, 0x100 + count)]
        assets = {}
        for i, guid in enumerate(guids):
            # prefab ごとに fileID を変える（同じ番号だと、入れ子の XOR で key が元に戻ってぶつかる）
            base = 1000 * (i + 1)
            text = HEADER + game_object(base + 10, f"P{i}") + transform(base + 11, base + 10)
            if i + 1 < count:
                text += instance(base + 20, guids[i + 1], base + 11, [])
            assets[guid] = text
        expander = Expander(lambda g: parse_asset(assets[g]) if g in assets else None, {})
        deep = expander.expand_asset(guids[0])
        self.assertTrue(deep.depth_truncated)
        self.assertEqual(len(deep.nodes), MAX_NESTING)
        # 深い位置で打ち切られた prefab も、直接展開すれば最後まで展開される
        tail = expander.expand_asset(guids[count - 3])
        self.assertFalse(tail.depth_truncated)
        self.assertEqual(len(tail.nodes), 3)

class LodGroupTest(unittest.TestCase):
    """LODGroup の段を Renderer に付ける（Issue #104）。"""

    @staticmethod
    def bike(levels=((103,), (113,), (123,)), enabled=1):
        """3 段の LOD を持つ prefab。``levels`` は LODGroup に並べる Renderer の fileID。"""
        return HEADER + "".join([
            game_object(10, "Bike"),
            transform(11, 10),
            lod_group(12, 10, [list(level) for level in levels], enabled=enabled),
            LodGroupTest.LOD_OBJECTS,
        ])

    LOD_OBJECTS = "".join([
        game_object(100, "Bike_LOD0"),
        transform(101, 100, father=11),
        mesh_renderer(102, 103, 100, MODEL, MAT_A, mesh_file_id=4300000),
        game_object(110, "Bike_LOD1"),
        transform(111, 110, father=11),
        mesh_renderer(112, 113, 110, MODEL, MAT_A, mesh_file_id=4300002),
        game_object(120, "Bike_LOD2"),
        transform(121, 120, father=11),
        mesh_renderer(122, 123, 120, MODEL, MAT_A, mesh_file_id=4300004),
    ])

    def levels(self, text, sources=None):
        assets = dict(sources or {})
        exp = Expander(lambda guid: parse_asset(assets[guid]) if guid in assets else None, {MODEL: "Bike"})
        h = exp.expand_raw(parse_asset(text))
        found = placements(h, [MODEL])
        return {name: r.lod_level for p in found for name, r in p.renderers.items()}

    def test_levels_follow_the_lod_group(self):
        self.assertEqual(self.levels(self.bike()), {"Bike_LOD0": 0, "Bike_LOD1": 1, "Bike_LOD2": 2})

    def test_disabled_group_leaves_every_renderer_at_zero(self):
        # 無効な LODGroup は Unity でもすべての Renderer が描かれる
        self.assertEqual(self.levels(self.bike(enabled=0)), {"Bike_LOD0": 0, "Bike_LOD1": 0, "Bike_LOD2": 0})

    def test_renderer_in_two_levels_keeps_the_finest(self):
        levels = ((103, 113), (113,), (123,))
        self.assertEqual(self.levels(self.bike(levels)), {"Bike_LOD0": 0, "Bike_LOD1": 0, "Bike_LOD2": 2})

    def test_empty_last_level_is_ignored(self):
        # 最後の段に Renderer が無い（Culled）LODGroup
        levels = ((103,), (113,), (123,), ())
        self.assertEqual(self.levels(self.bike(levels)), {"Bike_LOD0": 0, "Bike_LOD1": 1, "Bike_LOD2": 2})

    def test_levels_survive_a_prefab_instance(self):
        scene = HEADER + "".join([
            game_object(1000, "Street"),
            transform(1001, 1000),
            instance(2000, NESTED, 1001, [mod(11, NESTED, "m_LocalPosition.x", 3)]),
            stripped_transform(2001, 11, NESTED, 2000),
        ])
        self.assertEqual(self.levels(scene, {NESTED: self.bike()}), {"Bike_LOD0": 0, "Bike_LOD1": 1, "Bike_LOD2": 2})

    def test_group_in_the_scene_reaches_into_an_instance(self):
        # シーンの LODGroup が、差し込んだ prefab の中の Renderer を stripped の参照で指す
        plain = HEADER + "".join([
            game_object(10, "Rock"),
            transform(11, 10),
            game_object(100, "Rock_Near"),
            transform(101, 100, father=11),
            mesh_renderer(102, 103, 100, MODEL, MAT_A, mesh_file_id=4300000),
            game_object(110, "Rock_Far"),
            transform(111, 110, father=11),
            mesh_renderer(112, 113, 110, MODEL, MAT_A, mesh_file_id=4300002),
        ])
        scene = HEADER + "".join([
            game_object(1000, "Street"),
            transform(1001, 1000),
            lod_group(1002, 1000, [[2103], [2113]]),
            instance(2000, NESTED, 1001, []),
            stripped_transform(2001, 11, NESTED, 2000),
            stripped_transform(2103, 103, NESTED, 2000, class_id=23, kind="MeshRenderer"),
            stripped_transform(2113, 113, NESTED, 2000, class_id=23, kind="MeshRenderer"),
        ])
        self.assertEqual(self.levels(scene, {NESTED: plain}), {"Rock_Near": 0, "Rock_Far": 1})


if __name__ == "__main__":
    unittest.main()
