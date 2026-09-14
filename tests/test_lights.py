"""ライト・カメラの換算。数値は Unity 6（Built-in / URP 17.6）と Blender 5.2 EEVEE で測った画素値（Issue #49）に合わせてある。"""

import math
import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.lights import (
    BUILTIN_MATCH_RATIO,
    LIGHT_CAMERA_BASIS,
    PIPELINE_BUILTIN,
    PIPELINE_HDRP,
    PIPELINE_URP,
    convert_camera,
    convert_light,
    detect_pipeline,
)
from unitypackage_loader.core.transform import multiply, trs, unity_to_blender


def blender_pixel(light, distance=None):
    """Blender EEVEE で、白い拡散面をライトの真下から見た画素値（測定式）。"""
    if light.type == "SUN":
        return light.energy / math.pi
    return light.energy / (4 * math.pi**2 * distance**2)


def light(**values):
    body = {"m_Type": 2, "m_Intensity": 1, "m_Range": 10, "m_Color": {"r": 1, "g": 1, "b": 1, "a": 1}, "m_Shadows": {"m_Type": 2}}
    body.update(values)
    return body


class PipelineTest(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(detect_pipeline(["liltoon", "standard"]), PIPELINE_BUILTIN)
        self.assertEqual(detect_pipeline(["urp", "liltoon"]), PIPELINE_URP)
        self.assertEqual(detect_pipeline(["urp", "hdrp"]), PIPELINE_HDRP)


class BuiltinLightTest(unittest.TestCase):
    def test_directional_matches_measured_pixel(self):
        # Unity Built-in（Linear）: intensity 2 の平行光源で白い面の画素は 4.595
        sun = convert_light(light(m_Type=1, m_Intensity=2), PIPELINE_BUILTIN)
        self.assertEqual(sun.type, "SUN")
        self.assertAlmostEqual(blender_pixel(sun), 4.595, places=2)

    def test_point_matches_at_reference_distance(self):
        for intensity, light_range in ((1, 10), (3, 10), (1, 20)):
            point = convert_light(light(m_Intensity=intensity, m_Range=light_range), PIPELINE_BUILTIN)
            d = BUILTIN_MATCH_RATIO * light_range
            unity = intensity**2.2 / (1 + 25 * (d / light_range) ** 2)
            self.assertAlmostEqual(blender_pixel(point, d), unity, places=6)
            self.assertEqual((point.use_custom_distance, point.cutoff_distance), (True, light_range))

    def test_point_stays_within_factor_over_useful_distances(self):
        point = convert_light(light(m_Range=10), PIPELINE_BUILTIN)
        # 実測値（Legacy Diffuse、Range 10）: d = 2, 3, 4, 6, 8 m
        for d, measured in ((2, 0.50309), (3, 0.30886), (4, 0.20049), (6, 0.10013), (8, 0.05887)):
            ratio = blender_pixel(point, d) / measured
            self.assertTrue(1 / 1.45 < ratio < 1.45, f"d={d}: ratio {ratio:.3f}")

    def test_spot_angles(self):
        spot = convert_light(light(m_Type=0, m_SpotAngle=60, m_InnerSpotAngle=40), PIPELINE_BUILTIN)
        self.assertEqual(spot.type, "SPOT")
        self.assertAlmostEqual(spot.spot_size, math.radians(60))
        self.assertAlmostEqual(spot.spot_blend, 1 / 3)

    def test_color_is_linearized_and_temperature_kept(self):
        warm = convert_light(light(m_Color={"r": 0.5, "g": 1, "b": 0}, m_UseColorTemperature=1, m_ColorTemperature=3863), PIPELINE_BUILTIN)
        self.assertAlmostEqual(warm.color[0], 0.21404, places=4)
        self.assertEqual((warm.color[1], warm.color[2]), (1.0, 0.0))
        self.assertEqual((warm.use_temperature, warm.temperature), (True, 3863))

    def test_shadows_and_baked(self):
        self.assertFalse(convert_light(light(m_Shadows={"m_Type": 0}), PIPELINE_BUILTIN).use_shadow)
        baked = convert_light(light(m_Lightmapping=2, m_ShadowRadius=0.5), PIPELINE_BUILTIN)
        self.assertTrue(baked.baked_only)
        self.assertEqual(baked.shadow_soft_size, 0.5)

    def test_area_light(self):
        area = convert_light(light(m_Type=3, m_AreaSize={"x": 2, "y": 0.5}), PIPELINE_BUILTIN)
        self.assertEqual((area.type, area.shape, area.size, area.size_y, area.baked_only), ("AREA", "RECTANGLE", 2, 0.5, True))
        self.assertTrue(area.notes)

    def test_broken_values(self):
        broken = convert_light({"m_Type": "x", "m_Intensity": float("nan"), "m_Color": 3, "m_Shadows": None}, PIPELINE_BUILTIN)
        self.assertEqual(broken.type, "POINT")
        self.assertTrue(math.isfinite(broken.energy))


class UrpLightTest(unittest.TestCase):
    def test_directional_is_linear(self):
        sun = convert_light(light(m_Type=1, m_Intensity=2), PIPELINE_URP)
        self.assertAlmostEqual(blender_pixel(sun), 2.0)  # Simple Lit の実測 2.0

    def test_point_matches_inverse_square(self):
        point = convert_light(light(m_Intensity=3, m_Range=20), PIPELINE_URP)
        # 実測（Simple Lit）: d=1 → 2.99931、d=2 → 0.7476（Range 10）。Range の打ち切りから遠い所では I / d²
        self.assertAlmostEqual(blender_pixel(point, 1), 3.0, places=6)
        self.assertAlmostEqual(blender_pixel(point, 2), 0.75, places=6)

    def test_hdrp_is_marked_unmeasured(self):
        self.assertTrue(convert_light(light(), PIPELINE_HDRP).notes)


class CameraTest(unittest.TestCase):
    def test_vertical_fov(self):
        camera = convert_camera({"field of view": 60, "near clip plane": 0.3, "far clip plane": 200, "m_projectionMatrixMode": 1})
        self.assertEqual((camera.type, camera.sensor_fit, camera.clip_end), ("PERSP", "VERTICAL", 200))
        self.assertAlmostEqual(2 * math.degrees(math.atan(camera.sensor_height / 2 / camera.lens)), 60)

    def test_physical_camera(self):
        camera = convert_camera({"m_projectionMatrixMode": 2, "m_FocalLength": 15.638705, "m_SensorSize": {"x": 36, "y": 24},
                                 "m_GateFitMode": 1, "m_LensShift": {"x": 0.1, "y": 0}})
        self.assertEqual((camera.lens, camera.sensor_width, camera.sensor_fit, camera.shift_x), (15.638705, 36, "HORIZONTAL", 0.1))

    def test_orthographic(self):
        camera = convert_camera({"orthographic": 1, "orthographic size": 2.5})
        self.assertEqual((camera.type, camera.ortho_scale, camera.sensor_fit), ("ORTHO", 5.0, "VERTICAL"))


class OrientationTest(unittest.TestCase):
    def test_light_pointing_down_in_unity_points_down_in_blender(self):
        # Unity で Euler (90, 0, 0) のライトは真下（-Y）を向く。Blender のライトは自分の -Z を向く
        down = trs((0, 3, 0), (math.sqrt(0.5), 0, 0, math.sqrt(0.5)), (1, 1, 1))
        m = multiply(unity_to_blender(down), LIGHT_CAMERA_BASIS)
        direction = [-m[i][2] for i in range(3)]  # 自分の -Z をワールドで
        for a, e in zip(direction, (0, 0, -1)):
            self.assertAlmostEqual(a, e)
        self.assertAlmostEqual(m[2][3], 3.0)  # Unity の高さ 3 は Blender の Z

    def test_camera_up_stays_up(self):
        forward = trs((0, 0, 0), (0, 0, 0, 1), (1, 1, 1))  # Unity で +Z を向き +Y が上
        m = multiply(unity_to_blender(forward), LIGHT_CAMERA_BASIS)
        self.assertEqual([round(-m[i][2], 6) for i in range(3)], [0, -1, 0])  # Unity の +Z は Blender の -Y
        self.assertEqual([round(m[i][1], 6) for i in range(3)], [0, 0, 1])  # 上は Blender の +Z


if __name__ == "__main__":
    unittest.main()
