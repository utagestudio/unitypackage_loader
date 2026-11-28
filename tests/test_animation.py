import math
import unittest

from tests import _paths  # noqa: F401
from tests.anim_writer import clip_yaml
from unitypackage_loader.core.animation import (
    KIND_ACTIVE,
    KIND_ENABLED,
    KIND_EULER,
    KIND_POSITION,
    KIND_ROTATION,
    KIND_SCALE,
    KIND_SHAPE_KEY,
    AnimationParseError,
    Curve,
    Key,
    bezier_handles,
    euler_to_quaternion,
    evaluate,
    evaluate_many,
    is_state_clip,
    parse_clip,
    quaternion_to_euler,
    sample_times,
    transform_spec,
    transform_specs,
)
from unitypackage_loader.core.animation_targets import (
    Binding,
    TargetObject,
    TargetTree,
    bind_paths,
    folder_affinity,
    prefer_nearest_models,
)
from unitypackage_loader.core.model_space import local_basis, local_values, node_basis
from unitypackage_loader.core.transform import UNITY_TO_BLENDER, chain, inverse_affine, trs
from tests.test_transform import assert_mat

INF = float("inf")


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return tuple(u ** 3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t ** 3 * d for a, b, c, d in zip(p0, p1, p2, p3))


class ParseTest(unittest.TestCase):
    def test_shape_key_and_toggles(self):
        text = clip_yaml(
            "Smile",
            float_curves=[
                ("Body", "blendShape.smile", 137, [(0, 0, 0, 0), (1, 100, 0, 0)]),
                ("Body/Acc", "m_IsActive", 1, [(0, 1, INF, INF)]),
                ("Body", "m_Enabled", 137, [(0, 0, INF, INF)]),
                ("Body", "material._Color.r", 137, [(0, 1, 0, 0)]),
            ],
            loop=True,
            pptr=2,
        )
        clip = parse_clip(text)
        self.assertEqual(clip.name, "Smile")
        self.assertTrue(clip.loop)
        self.assertFalse(clip.humanoid)
        kinds = {(c.path, c.kind) for c in clip.curves}
        self.assertEqual(kinds, {("Body", KIND_SHAPE_KEY), ("Body/Acc", KIND_ACTIVE), ("Body", KIND_ENABLED)})
        smile = next(c for c in clip.curves if c.kind == KIND_SHAPE_KEY)
        self.assertEqual(smile.shape_key, "smile")
        self.assertEqual(clip.other_curves, 1)
        self.assertEqual(clip.pptr_curves, 2)
        self.assertEqual((clip.start, clip.stop), (0.0, 1.0))
        toggle = next(c for c in clip.curves if c.kind == KIND_ACTIVE)
        self.assertTrue(math.isinf(toggle.keys[0].out_slope))

    def test_vector_curves_split_by_component(self):
        text = clip_yaml(
            "Door",
            vector_curves={
                "m_PositionCurves": [("Door", [(0, (0, 1, 2), (0, 0, 0), (0, 0, 0)), (2, (1, 1, 2), (0, 0, 0), (0, 0, 0))])],
                "m_EulerCurves": [("Door/Hinge", [(0, (0, 90, 0), (0, 0, 0), (0, 0, 0))], 0)],
                "m_RotationCurves": [("Knob", [(0, (0, 0, 0, 1), (0, 0, 0, 0), (0, 0, 0, 0))])],
                "m_ScaleCurves": [("Door", [(0, (1, 2, 3), (0, 0, 0), (0, 0, 0))])],
            },
            stop_time=2,
        )
        clip = parse_clip(text)
        by = {(c.path, c.attribute): c for c in clip.curves}
        self.assertEqual(by[("Door", "m_LocalPosition.y")].kind, KIND_POSITION)
        self.assertEqual(by[("Door", "m_LocalPosition.x")].keys[1].value, 1.0)
        self.assertEqual(by[("Door/Hinge", "localEulerAnglesRaw.y")].kind, KIND_EULER)
        self.assertEqual(by[("Door/Hinge", "localEulerAnglesRaw.y")].rotation_order, 0)
        self.assertEqual(by[("Knob", "m_LocalRotation.w")].kind, KIND_ROTATION)
        self.assertEqual(by[("Knob", "m_LocalRotation.w")].component, 3)
        self.assertEqual(by[("Door", "m_LocalScale.z")].kind, KIND_SCALE)
        self.assertEqual(by[("Door", "m_LocalScale.z")].keys[0].value, 3.0)
        self.assertEqual(clip.stop, 2.0)

    def test_humanoid(self):
        text = clip_yaml(
            "Fist",
            float_curves=[
                ("", "LeftHand.Thumb.1 Stretched", 95, [(0, -1, 0, 0)]),
                ("", "Left Arm Down-Up", 95, [(0, 0.3, 0, 0)]),
                ("", "RootT.y", 95, [(0, 1, 0, 0)]),
            ],
        )
        clip = parse_clip(text)
        self.assertTrue(clip.humanoid)
        self.assertEqual(clip.curves, [])

    def test_humanoid_keeps_generic_curves(self):
        text = clip_yaml("Mixed", float_curves=[("", "Spine Front-Back", 95, [(0, 0, 0, 0)]), ("Body", "blendShape.a", 137, [(0, 50, 0, 0)])])
        clip = parse_clip(text)
        self.assertTrue(clip.humanoid)
        self.assertEqual([c.kind for c in clip.curves], [KIND_SHAPE_KEY])

    def test_animator_parameter_is_not_humanoid(self):
        clip = parse_clip(clip_yaml("Param", float_curves=[("", "GestureWeight", 95, [(0, 1, 0, 0)])]))
        self.assertFalse(clip.humanoid)
        self.assertEqual(clip.other_curves, 1)

    def test_editor_curves_fallback(self):
        text = clip_yaml("Editor").replace(
            "  m_EditorCurves: []",
            "  m_EditorCurves:\n  - serializedVersion: 2\n    curve:\n      serializedVersion: 2\n      m_Curve:\n"
            "      - serializedVersion: 3\n        time: 0\n        value: 5\n        inSlope: 0\n        outSlope: 0\n"
            "        tangentMode: 0\n        weightedMode: 0\n        inWeight: 0\n        outWeight: 0\n"
            "      m_PreInfinity: 2\n      m_PostInfinity: 2\n      m_RotationOrder: 4\n"
            "    attribute: m_LocalPosition.y\n    path: Lid\n    classID: 4\n    script: {fileID: 0}\n    flags: 0",
        )
        clip = parse_clip(text)
        self.assertEqual([(c.path, c.kind, c.component) for c in clip.curves], [("Lid", KIND_POSITION, 1)])

    def test_numeric_names_stay_text(self):
        clip = parse_clip(clip_yaml("Blink", float_curves=[("123", "blendShape.456", 137, [(0, 1, 0, 0)])]))
        self.assertEqual((clip.curves[0].path, clip.curves[0].shape_key), ("123", "456"))

    def test_not_a_clip(self):
        with self.assertRaises(AnimationParseError):
            parse_clip("%YAML 1.1\n--- !u!1 &1\nGameObject:\n  m_Name: a\n")
        with self.assertRaises(ValueError):
            parse_clip(b"\x00\x01garbage")

    def test_bad_values_are_ignored(self):
        text = clip_yaml("Bad", float_curves=[("A", "blendShape.x", 137, [(0, 1, 0, 0), (1, 2, 0, 0)])])
        text = text.replace("time: 1", "time: nan", 1).replace("value: 1", "value: abc", 1)
        clip = parse_clip(text)
        self.assertEqual([(k.time, k.value) for k in clip.curves[0].keys], [(0.0, 0.0)])


class StateClipTest(unittest.TestCase):
    """Animator のステート用の、状態を切り替えるだけのクリップ（読み込まない）。"""

    def _clip(self, keys, attribute="blendShape.a"):
        return parse_clip(clip_yaml("C", float_curves=[("Body", attribute, 137, keys)], stop_time=1))

    def test_single_key_and_constant_values(self):
        self.assertTrue(is_state_clip(self._clip([(0, 50, 0, 0)])))
        self.assertTrue(is_state_clip(self._clip([(0, 50, 0, 0), (1, 50, 0, 0)])))
        self.assertTrue(is_state_clip(self._clip([(0, 1, INF, INF), (2, 1, INF, INF)], "m_Enabled")))

    def test_short_clip_is_state_even_if_values_change(self):
        # 1/12 秒で 0 → 15°（車両のペダルの角度など）
        clip = parse_clip(clip_yaml("Pedal", vector_curves={"m_EulerCurves": [("Pedal", [(0, (0, 0, 0), (0, 0, 0), (0, 0, 0)), (1 / 12, (15, 0, 0), (0, 0, 0), (0, 0, 0))])]}))
        self.assertTrue(is_state_clip(clip))
        self.assertTrue(is_state_clip(self._clip([(0, 0, 0, 0), (0.1, 100, 0, 0)])))

    def test_motion(self):
        self.assertFalse(is_state_clip(self._clip([(0, 0, 0, 0), (1, 100, 0, 0)])))
        self.assertFalse(is_state_clip(self._clip([(0, 1, INF, INF), (0.5, 0, INF, INF)], "m_Enabled")))
        # 値は同じでも、接線があれば途中で膨らむ
        self.assertFalse(is_state_clip(self._clip([(0, 0, 0, 100), (1, 0, 0, 0)])))

    def test_no_curves(self):
        self.assertFalse(is_state_clip(parse_clip(clip_yaml("Empty"))))


class EvaluateTest(unittest.TestCase):
    def test_hermite(self):
        keys = [Key(0, 0, 0, 0), Key(1, 1, 0, 0)]
        self.assertAlmostEqual(evaluate(keys, 0.5), 0.5)  # smoothstep の中点
        self.assertAlmostEqual(evaluate(keys, 0.25), 3 * 0.0625 - 2 * 0.015625)
        linear = [Key(0, 0, 1, 1), Key(2, 2, 1, 1)]
        self.assertAlmostEqual(evaluate(linear, 0.7), 0.7)

    def test_outside_and_constant(self):
        keys = [Key(0, 1, INF, INF), Key(1, 5, INF, INF)]
        self.assertEqual(evaluate(keys, -1), 1)
        self.assertEqual(evaluate(keys, 0.99), 1)
        self.assertEqual(evaluate(keys, 1), 5)
        self.assertEqual(evaluate(keys, 3), 5)
        self.assertEqual(evaluate([], 0), 0)

    def test_weighted_matches_bezier(self):
        k0 = Key(0, 0, 2, 2, 0.1, 0.1, 2)
        k1 = Key(1, 1, 0, 0, 0.6, 0.6, 1)
        p0, p1, p2, p3 = (0, 0), (0.1, 0.2), (0.4, 1.0), (1, 1)
        for t in (0.1, 0.35, 0.8):
            x, y = _bezier(p0, p1, p2, p3, t)
            self.assertAlmostEqual(evaluate([k0, k1], x), y, places=6)

    def test_handles_reproduce_curve(self):
        for keys in (
            [Key(0, 0, 0, 3), Key(0.5, 2, -1, -1), Key(2, 0, 4, 4)],
            [Key(0, 1, 0, 0.5, 0.3, 0.2, 2), Key(1, 3, 1, 1, 0.5, 0.5, 3), Key(3, -1, 0, 0)],
        ):
            handles = bezier_handles(keys)
            for a, b in zip(handles, handles[1:]):
                for t in (0.2, 0.5, 0.9):
                    x, y = _bezier((a.time, a.value), a.right, b.left, (b.time, b.value), t)
                    self.assertAlmostEqual(evaluate(keys, x), y, places=5)

    def test_handles_constant_and_scale(self):
        handles = bezier_handles([Key(0, 0, 0, INF), Key(1, 100, 0, 0), Key(1, 50, 0, 0)], scale=0.01)
        self.assertEqual(len(handles), 2)  # 同じ時刻のキーは後のもの
        self.assertTrue(handles[0].constant)
        self.assertAlmostEqual(handles[1].value, 0.5)

    def test_evaluate_many_matches_evaluate(self):
        # エルミート・weighted・階段（同じ時刻に並んだキー）・範囲の外を混ぜる
        keys = [Key(0, 0, 0, 2), Key(0.5, 1, 1, INF), Key(0.5, 3, INF, -1, 0.2, 0.6, 2), Key(1.2, 0, 0.5, 0, 0.7, 0.1, 1), Key(2, 2, 0, 0)]
        times = [-0.5, 0, 0.1, 0.25, 0.5, 0.5, 0.75, 1.2, 1.5, 2, 3]
        self.assertEqual(evaluate_many(keys, times), [evaluate(keys, t) for t in times])
        self.assertEqual(evaluate_many([], [0, 1]), [0.0, 0.0])

    def test_transform_specs_match_transform_spec(self):
        curves = [
            Curve("A", "m_LocalPosition.y", 4, KIND_POSITION, [Key(0, 1, 0, 0), Key(1, 2, 0, 0)], component=1),
            Curve("A", "localEulerAnglesRaw.x", 4, KIND_EULER, [Key(0, 0, 0, 0), Key(1, 90, 0, 0)], component=0),
            Curve("A", "m_LocalScale.z", 4, KIND_SCALE, [Key(0, 1, 0, 0), Key(1, 3, 0, 0)], component=2),
        ]
        rest = euler_to_quaternion((10, 20, 30))
        times = [0, 0.3, 0.6, 1]
        self.assertEqual(transform_specs(curves, times, rest), [transform_spec(curves, t, rest) for t in times])

    def test_sample_times(self):
        curve = Curve("a", "m_LocalPosition.x", 4, KIND_POSITION, [Key(0, 0), Key(0.05, 1), Key(1, 0)])
        times = sample_times([curve], 0.0, 0.1, 30)
        self.assertEqual(times, sorted(times))
        self.assertIn(0.05, times)
        self.assertAlmostEqual(times[-1], 0.1)
        self.assertEqual(sample_times([], 0.0, 0.0, 30), [0.0])


class EulerTest(unittest.TestCase):
    def test_unity_default_order(self):
        # Quaternion.Euler(0, 90, 0) と Quaternion.Euler(90, 0, 0)
        for value, expected in (((0, 90, 0), (0, math.sqrt(0.5), 0, math.sqrt(0.5))), ((90, 0, 0), (math.sqrt(0.5), 0, 0, math.sqrt(0.5)))):
            for a, b in zip(euler_to_quaternion(value), expected):
                self.assertAlmostEqual(a, b)
        # Z → X → Y の順に回す: q = qy·qx·qz
        q = euler_to_quaternion((30, 40, 50))
        qx, qy, qz = euler_to_quaternion((30, 0, 0)), euler_to_quaternion((0, 40, 0)), euler_to_quaternion((0, 0, 50))
        m = chain((trs((0, 0, 0), qy, (1, 1, 1)), trs((0, 0, 0), qx, (1, 1, 1)), trs((0, 0, 0), qz, (1, 1, 1))))
        assert_mat(self, trs((0, 0, 0), q, (1, 1, 1)), m)

    def test_round_trip_all_orders(self):
        for order in range(6):
            for value in ((10, 20, 30), (-170, 80, 45), (0, -89.9, 120), (90, 90, 90)):
                q = euler_to_quaternion(value, order)
                back = euler_to_quaternion(quaternion_to_euler(q, order), order)
                sign = 1 if sum(a * b for a, b in zip(q, back)) >= 0 else -1
                for a, b in zip(q, back):
                    self.assertAlmostEqual(a, sign * b, places=6)

    def test_transform_spec(self):
        rest = euler_to_quaternion((0, 0, 30))
        euler = Curve("a", "localEulerAnglesRaw.y", 4, KIND_EULER, [Key(0, 0), Key(1, 90)], 1)
        spec = transform_spec([euler], 1.0, rest)
        expected = euler_to_quaternion((0, 90, 30))
        for a, b in zip(spec["rotation"], expected):
            self.assertAlmostEqual(a, b)
        self.assertEqual(spec["position"], [None, None, None])
        position = Curve("a", "m_LocalPosition.z", 4, KIND_POSITION, [Key(0, 2)], 2)
        self.assertEqual(transform_spec([position], 0, rest)["position"], [None, None, 2.0])
        partial = Curve("a", "m_LocalRotation.w", 4, KIND_ROTATION, [Key(0, 0.0)], 3)
        rotation = transform_spec([partial], 0, (0.0, 0.0, 0.5, 0.5))["rotation"]
        self.assertAlmostEqual(sum(v * v for v in rotation), 1.0)


class LocalBasisTest(unittest.TestCase):
    a = ((-0.01, 0, 0, 0), (0, 0.01, 0, 0), (0, 0, 0.01, 0), (0, 0, 0, 1))

    def test_node_basis_is_local_basis(self):
        basis = chain((UNITY_TO_BLENDER, trs((1, 2, 3), euler_to_quaternion((10, 20, 30)), (1, 2, 1)), self.a))
        spec = {"position": [None, 5.0, None]}
        assert_mat(self, node_basis(basis, spec, self.a), local_basis(basis, spec, UNITY_TO_BLENDER, self.a))

    def test_nested_child(self):
        # FBX の中の子: 親子とも C·W·A なので、子の行列は A⁻¹·L·A
        inverse = inverse_affine(self.a)
        local = trs((0.5, 0, 0), euler_to_quaternion((0, 45, 0)), (1, 1, 1))
        basis = chain((inverse, local, self.a))
        position, rotation, _ = local_values(basis, inverse, self.a)
        for p, q in zip(position, (0.5, 0, 0)):
            self.assertAlmostEqual(p, q)
        turned = local_basis(basis, {"rotation": list(euler_to_quaternion((0, 90, 0)))}, inverse, self.a)
        assert_mat(self, turned, chain((inverse, trs((0.5, 0, 0), euler_to_quaternion((0, 90, 0)), (1, 1, 1)), self.a)))


def _tree(objects, groups=()):
    return TargetTree([TargetObject(**o) for o in objects], groups)


class BindTest(unittest.TestCase):
    AVATAR = [
        dict(key="Root", name="Avatar"),
        dict(key="Armature", name="Armature", parent="Root", is_armature=True,
             bones=(("Hips", None), ("Spine", "Hips"), ("Head", "Spine"))),
        dict(key="Body", name="Body", parent="Armature"),
        dict(key="Hat", name="Hat", parent="Armature", bone_parent="Head"),
        dict(key="Tail", name="Tail.001", parent="Armature"),
    ]

    def test_skinned_meshes_are_siblings_of_armature(self):
        tree = _tree(self.AVATAR)
        [binding] = bind_paths(tree, ["Body", "Tail", "", "Armature/Hips/Spine"])
        self.assertEqual(binding.root, "Root")
        self.assertEqual(binding.targets["Body"].key, "Body")
        self.assertEqual(binding.targets["Tail"].key, "Tail")  # 連番を外して引く
        self.assertEqual(binding.targets[""].key, "Root")
        self.assertEqual((binding.targets["Armature/Hips/Spine"].key, binding.targets["Armature/Hips/Spine"].bone), ("Armature", "Spine"))
        self.assertEqual(tree.resolve("Root", "Armature/Hips/Spine/Head/Hat").key, "Hat")

    def test_top_group_when_no_root_object(self):
        objects = [o for o in self.AVATAR if o["key"] != "Root"]
        objects = [dict(o, parent=None) if o.get("parent") == "Root" else o for o in objects]
        tree = _tree(objects, [["Armature"]])
        [binding] = bind_paths(tree, ["Body", "Armature/Hips"])
        self.assertIsNone(binding.root)  # アーマチュアをルートとみなさない
        self.assertEqual(binding.targets["Body"].key, "Body")

    def test_root_only_or_unmatched(self):
        tree = _tree(self.AVATAR)
        self.assertEqual(bind_paths(tree, [""]), [])
        self.assertEqual(bind_paths(tree, ["Body", "Nope", "Gone", "Missing"]), [])  # 半分に満たない

    def test_instances_and_nested_duplicates(self):
        objects = [
            dict(key="A", name="Bike"), dict(key="A/h", name="Handle", parent="A"),
            dict(key="A/fbx", name="Collection", parent="A", is_empty=True), dict(key="A/fbx/h", name="Handle.001", parent="A/fbx"),
            dict(key="B", name="Bike.001"), dict(key="B/h", name="Handle.002", parent="B"),
        ]
        bindings = bind_paths(_tree(objects), ["Handle"])
        self.assertEqual(sorted(b.targets["Handle"].key for b in bindings), ["A/h", "B/h"])

    def test_empty_level_is_skipped(self):
        objects = [dict(key="P", name="Pin"), dict(key="F", name="Sheets", parent="P", is_empty=True), dict(key="M", name="Pins_1", parent="F")]
        [binding] = bind_paths(_tree(objects), ["Pins_1"])
        self.assertEqual(binding.targets["Pins_1"].key, "M")

    def test_unity_descendants(self):
        tree = _tree(self.AVATAR)
        self.assertEqual(set(tree.unity_descendants("Armature")), {"Armature", "Hat"})
        self.assertEqual(set(tree.unity_descendants("Root")), {"Root", "Armature", "Body", "Hat", "Tail"})


if __name__ == "__main__":
    unittest.main()


class NearestModelTest(unittest.TestCase):
    def test_folder_affinity(self):
        self.assertEqual(folder_affinity("Assets/Pack/Ghoul/Animations/run.anim", "Assets/Pack/Ghoul/Source/ghoul.fbx"), 3)
        self.assertEqual(folder_affinity("Assets/Pack/Boss/Animations/run.anim", "Assets/Pack/Ghoul/Source/ghoul.fbx"), 2)
        self.assertEqual(folder_affinity("run.anim", "Assets/ghoul.fbx"), 0)

    def test_prefers_models_near_the_clip(self):
        # ボーンの名前が共通のキャラクターのパック: クリップと同じフォルダのモデル（通常版と allinone 版）だけを残す
        models = {"ghoul": "Assets/Pack/Ghoul/Source/ghoul.fbx", "ghoul_all": "Assets/Pack/Ghoul/Source/ghoul_allinone.fbx",
                  "boss": "Assets/Pack/Boss/Source/boss.fbx", "unknown": None}
        bindings = [Binding(key) for key in models]
        kept = prefer_nearest_models(bindings, "Assets/Pack/Ghoul/Animations/run.anim", lambda b: models[b.root])
        self.assertEqual([b.root for b in kept], ["ghoul", "ghoul_all", "unknown"])

    def test_one_model_keeps_every_placement(self):
        # 同じモデルを複数置いたシーンは、モデルが 1 種類なのですべて残す
        bindings = [Binding("a"), Binding("b")]
        kept = prefer_nearest_models(bindings, "Assets/Elsewhere/clip.anim", lambda b: "Assets/Models/rig.fbx")
        self.assertEqual(kept, bindings)
