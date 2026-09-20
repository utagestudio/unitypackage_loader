import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.hierarchy import CLASS_MESH_RENDERER, Hierarchy, ModelPlacement, Node, PlacedRenderer, SceneContents
from unitypackage_loader.core.scene_import import parts_to_hide, scene_warnings
from unitypackage_loader.core.transform import IDENTITY
from unitypackage_loader.core.unity_ids import mesh_file_id

MODEL = "a" * 32


def node(key: int, model_guid: str | None = None) -> Node:
    return Node(key, "Root", [0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0], [1.0, 1.0, 1.0], model_guid=model_guid)


def renderer(name: str, visible: bool = True, mesh_id: int = 0, lod: int = 0) -> PlacedRenderer:
    return PlacedRenderer(name, [], CLASS_MESH_RENDERER, visible, mesh_id, lod)


class PartsToHideTests(unittest.TestCase):
    def placement(self, *renderers: PlacedRenderer, active: bool = True, model_root: bool = False):
        h = Hierarchy({1: node(1, MODEL if model_root else None)})
        placement = ModelPlacement(MODEL, 1, IDENTITY, active, {r.name: r for r in renderers})
        return h, placement

    def hidden(self, *args, **kwargs):
        result = parts_to_hide(*args, **kwargs)
        return result.names, result.unused

    def test_unused_meshes_are_hidden_for_placements_from_renderers(self):
        h, placement = self.placement(renderer("Body"), renderer("Door"))
        objects = [("Body.001", True), ("Door", True), ("Body_LOD1", True), ("Armature", False)]
        self.assertEqual(self.hidden(h, placement, objects), ({"Body_LOD1"}, 1))

    def test_renamed_parts_match_by_mesh_file_id(self):
        h, placement = self.placement(renderer("RenamedDoor", mesh_id=mesh_file_id("Door")))
        self.assertEqual(self.hidden(h, placement, [("Door.002", True), ("Window", True)]), ({"Window"}, 1))

    def test_model_instances_keep_all_parts(self):
        h, placement = self.placement(renderer("Body"), model_root=True)
        self.assertEqual(self.hidden(h, placement, [("Body", True), ("Body_LOD1", True)]), (set(), 0))

    def test_disabled_renderers_and_inactive_placements(self):
        h, placement = self.placement(renderer("Body"), renderer("Glass", visible=False))
        self.assertEqual(self.hidden(h, placement, [("Body", True), ("Glass.001", True)]), ({"Glass.001"}, 0))
        h, inactive = self.placement(renderer("Body"), active=False)
        # 非アクティブな配置はすべて隠すが、「使われていない部品」としては数えない
        self.assertEqual(self.hidden(h, inactive, [("Body", True), ("Extra", True)]), ({"Body", "Extra"}, 0))


class LodPartsTests(unittest.TestCase):
    """LODGroup の遠景用の段を隠す（#104）。"""

    def placement(self, *renderers: PlacedRenderer):
        h = Hierarchy({1: node(1)})
        return h, ModelPlacement(MODEL, 1, IDENTITY, True, {r.name: r for r in renderers})

    def test_lower_levels_are_hidden_and_counted(self):
        h, placement = self.placement(renderer("Bike_LOD0"), renderer("Bike_LOD1", lod=1), renderer("Bike_LOD2", lod=2))
        objects = [("Bike_LOD0", True), ("Bike_LOD1", True), ("Bike_LOD2", True)]
        result = parts_to_hide(h, placement, objects)
        self.assertEqual(result.names, {"Bike_LOD1", "Bike_LOD2"})
        self.assertEqual((result.unused, result.hidden_lods), (0, 2))
        self.assertEqual(result.lods, {"Bike_LOD0": 0, "Bike_LOD1": 1, "Bike_LOD2": 2})

    def test_levels_are_reported_but_not_hidden_when_switched_off(self):
        h, placement = self.placement(renderer("Bike_LOD0"), renderer("Bike_LOD1", lod=1))
        result = parts_to_hide(h, placement, [("Bike_LOD0", True), ("Bike_LOD1", True)], hide_lods=False)
        self.assertEqual((result.names, result.hidden_lods), (set(), 0))
        self.assertEqual(result.lods, {"Bike_LOD0": 0, "Bike_LOD1": 1})

    def test_numeric_suffix_and_renamed_parts_are_matched(self):
        h, placement = self.placement(
            renderer("Bike_LOD0"), renderer("Far", mesh_id=mesh_file_id("Bike_LOD1"), lod=1)
        )
        result = parts_to_hide(h, placement, [("Bike_LOD0.003", True), ("Bike_LOD1", True)])
        self.assertEqual(result.names, {"Bike_LOD1"})
        self.assertEqual(result.lods, {"Bike_LOD0.003": 0, "Bike_LOD1": 1})

    def test_inactive_placement_is_not_counted_as_lod(self):
        # 非アクティブな配置ではすべてが隠れるので、LOD を理由に隠したものはない
        h = Hierarchy({1: node(1)})
        renderers = [renderer("Bike_LOD0"), renderer("Bike_LOD1", lod=1)]
        inactive = ModelPlacement(MODEL, 1, IDENTITY, False, {r.name: r for r in renderers})
        result = parts_to_hide(h, inactive, [("Bike_LOD0", True), ("Bike_LOD1", True)])
        self.assertEqual((result.names, result.hidden_lods), ({"Bike_LOD0", "Bike_LOD1"}, 0))
        self.assertEqual(result.lods, {"Bike_LOD0": 0, "Bike_LOD1": 1})


class SceneWarningsTests(unittest.TestCase):
    def test_messages_in_order(self):
        contents = SceneContents([], unresolved_overrides=2, missing_sources=1, other_renderers=3)
        messages = scene_warnings(
            "Assets/S.unity", contents, baked_lights=1, light_notes={"b note", "a note"},
            hidden_unused=4, hidden_lods=7, skipped_nodes=5, skipped_offsets=6,
        )
        self.assertTrue(all(m.startswith("scene Assets/S.unity: ") for m in messages))
        self.assertEqual(
            [m.split(": ", 1)[1].split(" ", 1)[0] for m in messages],
            ["1", "a", "b", "2", "1", "3", "4", "7", "5", "6"],
        )

    def test_unreadable_scene_reports_only_lights(self):
        self.assertEqual(scene_warnings("S", None, hidden_unused=3), [])
        self.assertEqual(len(scene_warnings("S", None, baked_lights=1)), 1)
        self.assertEqual(scene_warnings("S", SceneContents([])), [])


if __name__ == "__main__":
    unittest.main()
