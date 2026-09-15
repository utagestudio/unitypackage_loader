import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.hierarchy import CLASS_MESH_RENDERER, Hierarchy, ModelPlacement, Node, PlacedRenderer, SceneContents
from unitypackage_loader.core.scene_import import parts_to_hide, scene_warnings
from unitypackage_loader.core.transform import IDENTITY
from unitypackage_loader.core.unity_ids import mesh_file_id

MODEL = "a" * 32


def node(key: int, model_guid: str | None = None) -> Node:
    return Node(key, "Root", [0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0], [1.0, 1.0, 1.0], model_guid=model_guid)


def renderer(name: str, visible: bool = True, mesh_id: int = 0) -> PlacedRenderer:
    return PlacedRenderer(name, [], CLASS_MESH_RENDERER, visible, mesh_id)


class PartsToHideTests(unittest.TestCase):
    def placement(self, *renderers: PlacedRenderer, active: bool = True, model_root: bool = False):
        h = Hierarchy({1: node(1, MODEL if model_root else None)})
        placement = ModelPlacement(MODEL, 1, IDENTITY, active, {r.name: r for r in renderers})
        return h, placement

    def test_unused_meshes_are_hidden_for_placements_from_renderers(self):
        h, placement = self.placement(renderer("Body"), renderer("Door"))
        objects = [("Body.001", True), ("Door", True), ("Body_LOD1", True), ("Armature", False)]
        self.assertEqual(parts_to_hide(h, placement, objects), ({"Body_LOD1"}, 1))

    def test_renamed_parts_match_by_mesh_file_id(self):
        h, placement = self.placement(renderer("RenamedDoor", mesh_id=mesh_file_id("Door")))
        self.assertEqual(parts_to_hide(h, placement, [("Door.002", True), ("Window", True)]), ({"Window"}, 1))

    def test_model_instances_keep_all_parts(self):
        h, placement = self.placement(renderer("Body"), model_root=True)
        self.assertEqual(parts_to_hide(h, placement, [("Body", True), ("Body_LOD1", True)]), (set(), 0))

    def test_disabled_renderers_and_inactive_placements(self):
        h, placement = self.placement(renderer("Body"), renderer("Glass", visible=False))
        self.assertEqual(parts_to_hide(h, placement, [("Body", True), ("Glass.001", True)]), ({"Glass.001"}, 0))
        h, inactive = self.placement(renderer("Body"), active=False)
        # 非アクティブな配置はすべて隠すが、「使われていない部品」としては数えない
        self.assertEqual(parts_to_hide(h, inactive, [("Body", True), ("Extra", True)]), ({"Body", "Extra"}, 0))


class SceneWarningsTests(unittest.TestCase):
    def test_messages_in_order(self):
        contents = SceneContents([], unresolved_overrides=2, missing_sources=1, other_renderers=3)
        messages = scene_warnings(
            "Assets/S.unity", contents, baked_lights=1, light_notes={"b note", "a note"},
            hidden_unused=4, skipped_nodes=5, skipped_offsets=6,
        )
        self.assertTrue(all(m.startswith("scene Assets/S.unity: ") for m in messages))
        self.assertEqual([m.split(": ", 1)[1].split(" ", 1)[0] for m in messages], ["1", "a", "b", "2", "1", "3", "4", "5", "6"])

    def test_unreadable_scene_reports_only_lights(self):
        self.assertEqual(scene_warnings("S", None, hidden_unused=3), [])
        self.assertEqual(len(scene_warnings("S", None, baked_lights=1)), 1)
        self.assertEqual(scene_warnings("S", SceneContents([])), [])


if __name__ == "__main__":
    unittest.main()
