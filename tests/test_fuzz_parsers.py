"""解析器のファズテスト。

シーン・prefab・.mat・.meta は補助的な情報なので、壊れていても「そのアセットだけを外して続ける」。
そのために解析器は ValueError 系（UnityYamlError / UnityBinaryError / HierarchyError / MaterialParseError）
以外を送出しない約束にしている（#69）。既存テストのフィクスチャをシードを固定した乱数で壊し、
解析から展開・配置の計算までを通して、その約束を確かめる。
"""

import random
import traceback
import unittest

from tests import _paths  # noqa: F401
from tests import test_hierarchy as th
from tests import test_material as tm
from tests import test_prefab as tp
from tests import test_profiles_toon as tpt
from tests import test_profiles_vrchat_mobile as tpv
from tests.test_unity_binary import legacy_material
from unitypackage_loader.core.hierarchy import (
    Expander,
    components,
    effective_active,
    parse_asset,
    placements,
    summarize,
    world_matrices,
)
from unitypackage_loader.core.material import parse_material
from unitypackage_loader.core.meta import ModelImporterInfo, TextureImporterInfo
from unitypackage_loader.core.prefab import parse_prefab, resolve_renderers, tables_by_model, unresolved_material_overrides
from unitypackage_loader.core.profiles.base import normalize_material
from unitypackage_loader.core.unity_binary import load_documents

SEED = 20260915
ROUNDS = 80  # フィクスチャ 1 つあたりの壊し方の数

# 差し込む断片。YAML の構文を崩すもの、参照や数値として変なもの、文書の区切り
_TOKENS = [
    "-", "- ", ":", ": ", "{", "}", "[", "]", "'", '"', "\\", "&", "*", "!u!", "|", ">",
    "--- !u!1 &1", "--- !u!1001 &5", "---", "fileID: ", "guid: ", "{fileID: 1}", "{fileID: 1, guid: , type: 3}",
    "{fileID: x, guid: 1}", "[]", "{}", "- - -", "  ", "\t", "\n", "\n- a\n", "\n  - {fileID: 1}\n",
    "-1e999", "1e999", "nan", "inf", "99999999999999999999999", "-0", "﻿", "%YAML 1.1", "null", "~",
]

_MODELS = {th.MODEL: "Model"}

META = f"""\
fileFormatVersion: 2
guid: {"9" * 32}
ModelImporter:
  serializedVersion: 22200
  internalIDToNameTable:
  - first:
      43: 4300000
    second: Body
  externalObjects:
  - first:
      type: UnityEngine:Material
      assembly: UnityEngine.CoreModule
      name: Body
    second: {{fileID: 2100000, guid: {"1" * 32}, type: 2}}
  fileIDToRecycleName:
    4300000: Body
  materials:
    materialImportMode: 2
  meshes:
    globalScale: 0.01
TextureImporter:
  mipmaps:
    sRGBTexture: 0
  textureType: 1
"""


def mutate_text(text: str, rng: random.Random) -> str:
    lines = text.split("\n")
    for _ in range(rng.randint(1, 3)):
        if not lines:
            lines = [""]
        i = rng.randrange(len(lines))
        op = rng.randrange(7)
        if op == 0:
            del lines[i]
        elif op == 1:
            lines.insert(i, lines[rng.randrange(len(lines))])
        elif op == 2:
            j = rng.randrange(len(lines))
            lines[i], lines[j] = lines[j], lines[i]
        elif op == 3:
            lines[i] = " " * rng.choice((1, 2, 4)) + lines[i] if rng.random() < 0.5 else lines[i][rng.randint(1, 3):]
        elif op == 4:
            pos = rng.randint(0, len(lines[i]))
            lines[i] = lines[i][:pos] + rng.choice(_TOKENS) + lines[i][pos:]
        elif op == 5:
            key, sep, _ = lines[i].partition(": ")
            lines[i] = f"{key}: {rng.choice(_TOKENS)}" if sep else lines[i] + ":"
        else:
            lines = lines[:i]  # 途中で切れたファイル
    return "\n".join(lines)


def mutate_bytes(data: bytes, rng: random.Random) -> bytes:
    buf = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        if not buf:
            break
        i = rng.randrange(len(buf))
        op = rng.randrange(3)
        if op == 0:
            buf[i] = rng.randrange(256)
        elif op == 1:
            buf[i : i + 4] = rng.randrange(1 << 32).to_bytes(4, "little")
        else:
            del buf[i:]
    return bytes(buf)


# 読み込み側の処理の流れ（blender/importer.py の prepare_package と同じ順に呼ぶ）


def _read_from(assets: dict[str, str]):
    def read(guid: str):
        data = assets.get(guid)
        if data is None:
            return None
        try:
            return parse_asset(data)
        except ValueError:
            return None

    return read


def run_scene(data, assets: dict[str, str]) -> None:
    hierarchy = Expander(_read_from(assets), _MODELS).expand_raw(parse_asset(data))
    summarize(hierarchy, _MODELS)
    placements(hierarchy, _MODELS)
    components(hierarchy)
    effective_active(hierarchy)
    world_matrices(hierarchy)


def run_prefab(data) -> None:
    guid = "7" * 32
    documents = {guid: parse_prefab(data)}
    resolved: dict = {}
    unresolved_material_overrides(guid, documents, resolved)
    tables_by_model(resolve_renderers(guid, documents, resolved).values(), [tp.MODEL_A, th.MODEL])


def run_material(data) -> None:
    load_documents(data)
    normalize_material(parse_material(data, "f" * 32, "Assets/Fuzz.mat"))


def run_meta(text) -> None:
    ModelImporterInfo.from_meta(text)
    TextureImporterInfo.from_meta(text)


class ParserFuzzTest(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(SEED)
        self.failures: dict[tuple[str, str], str] = {}  # (処理, 例外と発生箇所) → 最初の入力

    def fuzz(self, label: str, run, seed_data, mutate) -> None:
        for _ in range(ROUNDS):
            data = mutate(seed_data, self.rng)
            try:
                run(data)
            except ValueError:
                pass
            except Exception as exc:  # noqa: BLE001 - 約束に反する例外を集めて最後にまとめて報告する
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                key = (label, f"{type(exc).__name__} at {frame.name}:{frame.lineno}")
                self.failures.setdefault(key, repr(data)[:600])

    def tearDown(self):
        if self.failures:
            lines = [f"{label}: {where}\n    input: {data}" for (label, where), data in sorted(self.failures.items())]
            self.fail("parsers raised non-ValueError exceptions:\n" + "\n".join(lines))

    def test_scene_and_prefab_hierarchy(self):
        assets = {th.NESTED: th.NESTED_PREFAB, th.UNPACKED: th.UNPACKED_PREFAB, th.LOOP: th.LOOP_PREFAB, "d" * 32: th.ComponentTest.LAMP}
        # GameObject を消した prefab のインスタンスに、そのライトへの上書きが残っているシーン（#68）
        removed = th.HEADER + th.instance(
            10, "d" * 32, 0, [th.mod(3, "d" * 32, "m_Intensity", 2.5), th.mod(2, "d" * 32, "m_LocalPosition.y", 1)],
            removed_game_objects=f"    - {{fileID: 1, guid: {'d' * 32}, type: 3}}\n",
        )
        for scene in (th.SCENE, th.ComponentTest.SCENE, removed):
            self.fuzz("scene", lambda data: run_scene(data, assets), scene, mutate_text)
        for guid, prefab in assets.items():
            # 壊れた prefab を、シーンから展開する
            self.fuzz("nested prefab", lambda data, guid=guid: run_scene(th.SCENE, {**assets, guid: data}), prefab, mutate_text)

    def test_prefab_tables(self):
        for prefab in (tp.PREFAB, th.NESTED_PREFAB, th.UNPACKED_PREFAB):
            self.fuzz("prefab", run_prefab, prefab, mutate_text)

    def test_materials(self):
        for mat in (
            tm.LILTOON_OPAQUE, tm.LILTOON_TRANS, tm.STANDARD_CUTOUT, tm.URP_TRANSPARENT, tm.LEGACY_EMISSION_ON,
            tm.hdrp_mat_yaml(tm.HDRP_LIT), tpt.MTOON_LEGACY, tpt.POIYOMI, tpv.TOON_STANDARD_FULL, tpv.STANDARD_LITE_EMISSION_ON,
        ):
            self.fuzz("material", run_material, mat, mutate_text)

    def test_binary_material(self):
        self.fuzz("binary material", run_material, legacy_material(), mutate_bytes)

    def test_meta(self):
        self.fuzz("meta", run_meta, META, mutate_text)


if __name__ == "__main__":
    unittest.main()
