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


def stripped_transform(file_id, source_file_id, source_guid, instance_id):
    return (
        f"--- !u!4 &{file_id} stripped\nTransform:\n"
        f"  m_CorrespondingSourceObject: {{fileID: {source_file_id}, guid: {source_guid}, type: 3}}\n"
        f"  m_PrefabInstance: {{fileID: {instance_id}}}\n"
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

    def test_child_of_legacy_model_instance_uses_stripped_alias(self):
        exp = Expander(lambda g: None, {MODEL: "Probe"})
        h = exp.expand_raw(parse_asset(self.scene()))
        flower = next(n for n in h.nodes.values() if n.name == "Flower")
        self.assertEqual(h.nodes[flower.parent].name, "Pot")
        self.assertEqual(transform_point(world_matrices(h)[flower.key], (0, 0, 0)), (2.0, 1.0, 5.0))


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


if __name__ == "__main__":
    unittest.main()
