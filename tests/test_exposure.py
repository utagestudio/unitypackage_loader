"""HDRP の Volume の露出から Blender の露出を決める（#130）。"""

import math
import unittest

from tests import _paths  # noqa: F401
from unitypackage_loader.core.exposure import (
    BLENDER_EXPOSURE_AT_EV0,
    exposure_settings,
    global_volumes,
    scene_exposure,
)
from unitypackage_loader.core.unity_yaml import parse_documents

HEADER = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
VOLUME = "172515602e62fb746b5d573b38a5fe58"
EXPOSURE = "2d08ce26990eb1a4a9177b860541e702"


def volume(file_id, profile, is_global=1, priority=0, weight=1, enabled=1):
    return (
        f"--- !u!114 &{file_id}\nMonoBehaviour:\n  m_Enabled: {enabled}\n"
        f"  m_Script: {{fileID: 11500000, guid: {VOLUME}, type: 3}}\n"
        f"  m_IsGlobal: {is_global}\n  priority: {priority}\n  weight: {weight}\n"
        f"  sharedProfile: {{fileID: 11400000, guid: {profile}, type: 2}}\n"
    )


def param(name, value, override=1):
    return f"  {name}:\n    m_OverrideState: {override}\n    m_Value: {value}\n"


def exposure_profile(active=1, **params):
    body = "".join(param(k, v) if not isinstance(v, tuple) else param(k, *v) for k, v in params.items())
    return (
        HEADER + "--- !u!114 &1\nMonoBehaviour:\n  m_Name: Exposure\n"
        f"  m_Script: {{fileID: 11500000, guid: {EXPOSURE}, type: 3}}\n  active: {active}\n" + body
    )


class VolumeTests(unittest.TestCase):
    def test_global_volumes_by_priority(self):
        docs = parse_documents(HEADER + volume(1, "a" * 32, priority=0) + volume(2, "b" * 32, priority=5)
                               + volume(3, "c" * 32, is_global=0, priority=9) + volume(4, "d" * 32, weight=0)
                               + volume(5, "e" * 32, enabled=0))
        self.assertEqual([v.profile_guid for v in global_volumes(docs)], ["b" * 32, "a" * 32])

    def test_exposure_settings_only_overridden(self):
        settings = exposure_settings(parse_documents(exposure_profile(mode=1, limitMax=7, limitMin=(2, 0))))
        self.assertEqual(settings, {"mode": 1.0, "limitMax": 7.0})
        self.assertIsNone(exposure_settings(parse_documents(exposure_profile(active=0, mode=0))))
        self.assertIsNone(exposure_settings(parse_documents(HEADER + "--- !u!114 &1\nMonoBehaviour:\n  m_Name: Bloom\n")))


class SceneExposureTests(unittest.TestCase):
    def test_blender_exposure_matches_hdrp(self):
        # HDRP: 画素 = L / (1.2 · 2^EV)。Blender（光を ÷ 683 した場合）: 画素 = L / 683 · 2^x
        result = scene_exposure({"mode": 0, "fixedExposure": 9})
        self.assertEqual((result.mode, result.approximate), ("fixed", False))
        self.assertAlmostEqual(2 ** result.exposure / 683, 1 / (1.2 * 2 ** 9))
        self.assertAlmostEqual(BLENDER_EXPOSURE_AT_EV0, math.log2(683 / 1.2))

    def test_compensation_is_subtracted(self):
        self.assertAlmostEqual(scene_exposure({"mode": 0, "fixedExposure": 9, "compensation": 1}).ev100, 8)

    def test_automatic_uses_upper_limit(self):
        # UnityJapanOffice の NoonA: 自動露出 EV 2〜7
        result = scene_exposure({"mode": 1, "limitMin": 2, "limitMax": 7, "compensation": 0})
        self.assertEqual((result.ev100, result.approximate, result.mode), (7, True, "automatic"))
        self.assertAlmostEqual(result.exposure, math.log2(683 / 1.2) - 7)
        self.assertEqual(scene_exposure({"mode": 4}).ev100, 14)  # 上限が上書きされていなければ HDRP の既定の 14

    def test_physical_camera(self):
        camera = {"m_Aperture": 8, "m_ShutterSpeed": 1 / 125, "m_Iso": 100}
        result = scene_exposure({"mode": 3}, camera)
        self.assertAlmostEqual(result.ev100, math.log2(64 * 125))
        self.assertIsNone(scene_exposure({"mode": 3}, None))
        self.assertIsNone(scene_exposure({"mode": 3}, {"m_Aperture": 0, "m_ShutterSpeed": 1, "m_Iso": 100}))

    def test_mode_not_overridden(self):
        self.assertIsNone(scene_exposure({"fixedExposure": 5}))

    def test_exposure_is_clamped_to_blender_range(self):
        self.assertEqual(scene_exposure({"mode": 0, "fixedExposure": -100}).exposure, 32)


if __name__ == "__main__":
    unittest.main()
