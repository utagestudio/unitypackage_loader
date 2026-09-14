import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.prefab import RendererMaterials
from unitypackage_loader.core.units import (
    NO_MESH_REASON,
    UNIT_MODELS,
    UNIT_PREFABS,
    PrefabSummary,
    available_units,
    choice_count,
    default_unit,
    summarize_prefabs,
)

MAT_A = "a" * 32
MAT_B = "b" * 32
MODEL_1 = "1" * 32
MODEL_2 = "2" * 32
BLEND = "3" * 32


def table(name: str, model: str, mats: list[str | None]) -> dict[str, RendererMaterials]:
    return {name: RendererMaterials(name, mats, mesh_guid=model)}


class SummarizePrefabsTest(unittest.TestCase):
    def setUp(self):
        self.tables = {
            "Assets/P/Red.prefab": {MODEL_1: table("Body", MODEL_1, [MAT_A, None, MAT_A])},
            "Assets/P/Both.prefab": {
                MODEL_2: table("Hair", MODEL_2, [MAT_B]),
                MODEL_1: table("Body", MODEL_1, [MAT_A]),
            },
            "Assets/P/Torus.prefab": {BLEND: table("Torus", BLEND, [MAT_B])},
        }
        prefabs = [
            ("r", "Assets/P/Red.prefab"),
            ("b", "Assets/P/Both.prefab"),
            ("e", "Assets/P/Effect.prefab"),
            ("t", "Assets/P/Torus.prefab"),
        ]
        self.summaries = {
            s.pathname: s
            for s in summarize_prefabs(prefabs, self.tables, [MODEL_1, MODEL_2, BLEND], {BLEND: ".blend disabled"})
        }

    def test_sorted_by_pathname_and_nothing_dropped(self):
        prefabs = [("x", "Assets/Z.prefab"), ("y", "Assets/A.prefab")]
        self.assertEqual([s.pathname for s in summarize_prefabs(prefabs, {}, [], {})], ["Assets/A.prefab", "Assets/Z.prefab"])

    def test_models_follow_model_order(self):
        self.assertEqual(self.summaries["Assets/P/Both.prefab"].model_guids, [MODEL_1, MODEL_2])

    def test_materials_unique_in_order(self):
        self.assertEqual(self.summaries["Assets/P/Red.prefab"].material_guids, [MAT_A])
        self.assertEqual(self.summaries["Assets/P/Both.prefab"].material_guids, [MAT_A, MAT_B])

    def test_prefab_without_mesh_is_kept_with_reason(self):
        effect = self.summaries["Assets/P/Effect.prefab"]
        self.assertFalse(effect.supported)
        self.assertEqual(effect.skip_reason, NO_MESH_REASON)

    def test_prefab_using_only_unsupported_models(self):
        torus = self.summaries["Assets/P/Torus.prefab"]
        self.assertFalse(torus.supported)
        self.assertEqual(torus.skip_reason, ".blend disabled")

    def test_name_is_file_stem(self):
        self.assertEqual(self.summaries["Assets/P/Red.prefab"].name, "Red")
        self.assertEqual(PrefabSummary("g", "Assets/Plain").name, "Plain")


class UnitChoiceTest(unittest.TestCase):
    def setUp(self):
        self.ok = PrefabSummary("a", "A.prefab", [MODEL_1])
        self.ng = PrefabSummary("b", "B.prefab", supported=False, skip_reason=NO_MESH_REASON)

    def test_available_units(self):
        self.assertEqual(available_units([self.ng], 1), [UNIT_PREFABS, UNIT_MODELS])
        self.assertEqual(available_units([], 2), [UNIT_MODELS])
        self.assertEqual(available_units([self.ok], 0), [UNIT_PREFABS])

    def test_choice_count_ignores_unreadable(self):
        self.assertEqual(choice_count([self.ok, self.ng], 1), 2)
        self.assertEqual(choice_count([self.ng], 1), 1)

    def test_default_prefers_prefabs(self):
        self.assertEqual(default_unit([self.ok], 1), UNIT_PREFABS)
        self.assertEqual(default_unit([self.ng], 1), UNIT_MODELS)

    def test_default_remembers_last_unit_when_readable(self):
        self.assertEqual(default_unit([self.ok], 1, UNIT_MODELS), UNIT_MODELS)
        self.assertEqual(default_unit([self.ok], 0, UNIT_MODELS), UNIT_PREFABS)
        self.assertEqual(default_unit([self.ok], 1, "BOGUS"), UNIT_PREFABS)


if __name__ == "__main__":
    unittest.main()
