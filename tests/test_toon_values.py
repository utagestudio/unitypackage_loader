"""Toon ノードグループに渡す影・MatCap・リムの値（#73）。

Toon の組み立て（blender/materials.py）は NormalizedMaterial の shadow / matcap / rim だけを読む。
各トゥーン系のプロファイルがそれを埋めていること（契約）、lilToon / MToon の値が 1.7.4 までの換算と同じであること、
1.7.4 までに保存した unity_normalized からも同じ値を補えることを確かめる。
"""

import json
import unittest

from tests import _paths  # noqa: F401
from tests.test_material import LILTOON_OPAQUE, TEX_E, TEX_N
from tests.test_profiles_toon import MTOON10_UNKNOWN_GUID, MTOON_LEGACY, POIYOMI
from tests.test_profiles_vrchat_mobile import TOON_STANDARD_FEATURES_OFF, TOON_STANDARD_FULL
from unitypackage_loader.core.material import (
    MATCAP_ADD,
    MATCAP_NORMAL,
    NormalizedMaterial,
    ToonMatCap,
    ToonRim,
    ToonShadow,
    parse_material,
    toon_values_from_extras,
)
from unitypackage_loader.core.profiles import normalize_material

TOON_FIXTURES = {
    "liltoon": LILTOON_OPAQUE,
    "mtoon": MTOON_LEGACY,
    "mtoon10": MTOON10_UNKNOWN_GUID,
    "poiyomi": POIYOMI,
    "toon_standard": TOON_STANDARD_FULL,
    "toon_standard_off": TOON_STANDARD_FEATURES_OFF,
}


def normalized(text: str) -> NormalizedMaterial:
    return normalize_material(parse_material(text, guid="f" * 32, pathname="Assets/X.mat"))


def without_typed_values(n: NormalizedMaterial) -> dict:
    """1.7.4 までに保存した unity_normalized の形（shadow / matcap / rim のキーが無い）。"""
    data = json.loads(json.dumps(n.to_dict()))
    for key in ("shadow", "matcap", "rim"):
        del data[key]
    return data


class ContractTests(unittest.TestCase):
    """トゥーンとして組み立てるマテリアルは、Toon の組み立てが読む値を型付きのフィールドに持つ。"""

    def test_every_toon_profile_provides_a_shadow(self):
        for label, text in TOON_FIXTURES.items():
            with self.subTest(label):
                n = normalized(text)
                self.assertEqual(n.lighting, "toon")
                self.assertIsInstance(n.shadow, ToonShadow)

    def test_matcap_in_extras_becomes_typed(self):
        # extras に MatCap の画像があるなら、組み立てが読む matcap にも入っている（キーの名前がずれて黙って消えない）
        for label, text in TOON_FIXTURES.items():
            with self.subTest(label):
                n = normalized(text)
                source = n.extras.get("matcap") or {}
                if source.get("tex"):
                    self.assertIsInstance(n.matcap, ToonMatCap)
                    self.assertEqual(n.matcap.tex, source["tex"])
                else:
                    self.assertIsNone(n.matcap)

    def test_rim_in_extras_becomes_typed(self):
        for label, text in TOON_FIXTURES.items():
            with self.subTest(label):
                n = normalized(text)
                self.assertEqual(n.rim is not None, bool(n.extras.get("rim")))


class LegacyConversionTests(unittest.TestCase):
    """lilToon と MToon は 1.7.4 までと同じ見た目のまま。"""

    def test_liltoon_and_mtoon_keep_the_previous_conversion(self):
        for label in ("liltoon", "mtoon", "mtoon10"):
            with self.subTest(label):
                n = normalized(TOON_FIXTURES[label])
                self.assertEqual((n.shadow, n.matcap, n.rim), toon_values_from_extras(n.extras))

    def test_liltoon_values(self):
        n = normalized(LILTOON_OPAQUE)
        self.assertEqual(n.shadow, ToonShadow(color=(0.8, 0.7, 0.7, 1.0), strength=0.6, border=0.5, blur=0.1))
        self.assertEqual(n.matcap, ToonMatCap(tex=TEX_E, color=(1.0, 1.0, 1.0, 1.0), strength=1.0, mode=MATCAP_ADD))
        self.assertIsNone(n.rim)

    def test_mtoon_values(self):
        n = normalized(MTOON_LEGACY)
        self.assertEqual(n.shadow.color, (0.6, 0.5, 0.5, 1.0))
        self.assertAlmostEqual(n.shadow.border, 0.55)  # _ShadeShift -0.1 で明るい側に寄る
        self.assertAlmostEqual(n.shadow.blur, 0.1)  # 1 - _ShadeToony
        self.assertEqual((n.matcap.tex, n.matcap.mode), (TEX_E, MATCAP_ADD))
        self.assertEqual(n.rim.strength, 0.0)  # _RimColor が黒ならリムは付かない


class NewConversionTests(unittest.TestCase):
    """VRChat Mobile の Toon Standard と Poiyomi は、意味のはっきりした値だけを換算する。"""

    def test_toon_standard(self):
        n = normalized(TOON_STANDARD_FULL)
        self.assertEqual(n.shadow, ToonShadow())  # ramp は再現できないので既定の影
        self.assertEqual(n.rim.color, (1.0, 1.0, 1.0, 1.0))
        self.assertAlmostEqual(n.rim.strength, 0.5)  # _RimIntensity
        self.assertAlmostEqual(n.rim.border, 0.7)  # 1 - _RimRange（既定 0.3）
        self.assertAlmostEqual(n.rim.blur, 0.9)  # 1 - _RimSharpness（既定 0.1）
        self.assertEqual((n.matcap.tex, n.matcap.mode), (TEX_N, MATCAP_NORMAL))  # _MatcapType 1 は加算ではない
        self.assertAlmostEqual(n.matcap.strength, 0.7)  # _MatcapStrength（1.7.4 までは無視して 1.0）
        self.assertTrue(any(w.startswith("shader approximation:") and "ramp" in w for w in n.warnings))

    def test_toon_standard_features_off(self):
        n = normalized(TOON_STANDARD_FEATURES_OFF)
        self.assertIsNone(n.matcap)  # USE_MATCAP キーワードが無い
        self.assertIsNone(n.rim)  # _RimIntensity 0

    def test_poiyomi_uses_only_the_shadow_strength(self):
        n = normalized(POIYOMI)
        self.assertEqual(n.shadow, ToonShadow(strength=0.7))  # 1.7.4 までは影が付かなかった
        self.assertTrue(any(w.startswith("shader approximation:") and "Poiyomi" in w for w in n.warnings))


class SerializationTests(unittest.TestCase):
    def test_round_trip_keeps_typed_values(self):
        n = normalized(TOON_STANDARD_FULL)
        restored = NormalizedMaterial.from_dict(json.loads(json.dumps(n.to_dict())))
        self.assertEqual((restored.shadow, restored.matcap, restored.rim), (n.shadow, n.matcap, n.rim))

    def test_data_saved_before_typed_values_is_filled_from_extras(self):
        n = normalized(LILTOON_OPAQUE)
        restored = NormalizedMaterial.from_dict(without_typed_values(n))
        self.assertEqual((restored.shadow, restored.matcap, restored.rim), (n.shadow, n.matcap, n.rim))

    def test_old_toon_standard_data_keeps_its_old_look(self):
        # 1.7.4 までは Toon Standard の extras も lilToon と同じキーで読み、既定の灰色の影と、色のアルファを強さにしたリムだった
        restored = NormalizedMaterial.from_dict(without_typed_values(normalized(TOON_STANDARD_FULL)))
        self.assertEqual(restored.shadow, ToonShadow())
        self.assertEqual(restored.rim.strength, 1.0)
        self.assertEqual(restored.matcap.strength, 1.0)

    def test_broken_typed_value_is_ignored(self):
        data = json.loads(json.dumps(normalized(LILTOON_OPAQUE).to_dict()))
        data["matcap"] = {"color": [1, 1, 1]}  # tex が無い
        data["shadow"] = "not a dict"
        restored = NormalizedMaterial.from_dict(data)
        self.assertIsNone(restored.matcap)
        self.assertIsNone(restored.shadow)

    def test_matcap_texture_is_referenced(self):
        n = NormalizedMaterial(name="x", matcap=ToonMatCap(tex="a" * 32))
        self.assertIn("a" * 32, n.extra_texture_guids())


if __name__ == "__main__":
    unittest.main()
