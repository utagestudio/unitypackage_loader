"""Unity の Terrain を読み込むシーンの合成 unitypackage を生成する（Issue #205）。

    blender -b --factory-startup --python tests/make_synthetic_terrain_package.py -- <出力パス.unitypackage>

モデルを使わない Assets/Synthetic/Scenes/Terrain.unity に、2 つの Terrain を置く。実在アセットのデータは使わない。

- Ground（シーンの最上位）: 2021.3 の形の TerrainData（Ground Data.asset）。高さマップ 257 × 257、点の間隔 X 0.5 m・Z 0.25 m、
  高さの最大 20 m で、X の番号 i の点の高さの値が i × 127（X に沿った坂）。マス (10〜11, 20〜21) の 4 つが穴。
  TerrainLayer 2 つ（Grass: ディフューズと法線、Road: ディフューズとマスク）、アルファマップ 8 × 8（左半分が Grass、右半分が
  Road）、木 3 本。GameObject は位置 (10, 1, 20)・Y 軸 45° の回転・2 倍の拡大で、Terrain は回転と拡大を受けない。
- Level/Old Ground: 2018.3 より前の形の TerrainData（Old Data.asset）。高さマップ 17 × 9（すべて 0）、点の間隔 1 m、
  SplatPrototype 1 つ、ARGB32 のアルファマップ。親の Level は位置 (100, 0, 0)・Y 軸 90° の回転、Old Ground は親の中の (5, 0, 0)。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.make_synthetic_package import guid_of, png_bytes, texture_meta, write_package  # noqa: E402
from tests.terrain_writer import terrain_data_asset, terrain_layer_yaml  # noqa: E402
from tests.unity_yaml_writer import HEADER, game_object, transform  # noqa: E402

GROUND_POINTS = 257
OLD_COLUMNS, OLD_ROWS = 17, 9


def _terrain(file_id: int, go: int, data_guid: str) -> str:
    return (
        f"--- !u!218 &{file_id}\nTerrain:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n  serializedVersion: 6\n"
        f"  m_TerrainData: {{fileID: 15600000, guid: {data_guid}, type: 2}}\n  m_DrawHeightmap: 1\n"
        "  m_MaterialTemplate: {fileID: 10652, guid: 0000000000000000f000000000000000, type: 0}\n"
    )


def _alphamap(size: int, left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> bytes:
    """左半分（Terrain の X の小さい側）と右半分で重みの違う RGBA。"""
    return bytes(b for _ in range(size) for x in range(size) for b in (left if x < size // 2 else right))


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else REPO_ROOT / "_local" / "unitypackages" / "synthetic_terrain.unitypackage"
    entries: dict[str, tuple[str, bytes | None, str | None]] = {}

    def add(pathname: str, asset: bytes | None, meta: str | None) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    def texture(name: str, rgb: tuple[int, int, int], normal: bool = False) -> str:
        path = f"Assets/Synthetic/Terrain/{name}.png"
        return add(path, png_bytes(8, rgb), texture_meta(guid_of(path), normal))

    def native_meta(path: str, main_id: int) -> str:
        return f"fileFormatVersion: 2\nguid: {guid_of(path)}\nNativeFormatImporter:\n  mainObjectFileID: {main_id}\n"

    grass_tex = texture("Grass", (60, 160, 60))
    grass_normal = texture("GrassNormal", (128, 128, 255), normal=True)
    road_tex = texture("Road", (90, 90, 90))
    road_mask = texture("RoadMask", (0, 255, 0))
    old_tex = texture("Dirt", (150, 110, 70))

    layers = []
    for name, text in (
        ("Grass", terrain_layer_yaml("Grass", grass_tex, grass_normal, tile_size=(8.0, 8.0))),
        ("Road", terrain_layer_yaml("Road", road_tex, mask=road_mask, tile_size=(4.0, 2.0), tile_offset=(1.0, 0.0), smoothness=0.4)),
    ):
        path = f"Assets/Synthetic/Terrain/{name}.terrainlayer"
        layers.append(add(path, text.encode(), native_meta(path, 8574412962073106934)))

    heights = [i * 127 for _ in range(GROUND_POINTS) for i in range(GROUND_POINTS)]
    cells = GROUND_POINTS - 1
    holes = [255] * (cells * cells)
    for j in (20, 21):
        for i in (10, 11):
            holes[j * cells + i] = 0
    ground_path = "Assets/Synthetic/Terrain/Ground Data.asset"
    ground = add(
        ground_path,
        terrain_data_asset(
            GROUND_POINTS, GROUND_POINTS, (0.5, 20.0, 0.25), heights, holes=holes, layers=layers,
            alphamaps=[(8, 8, _alphamap(8, (255, 0, 0, 0), (0, 255, 0, 0)))], trees=3, name="Ground Data",
        ),
        native_meta(ground_path, 15600000),
    )
    old_path = "Assets/Synthetic/Terrain/Old Data.asset"
    old = add(
        old_path,
        terrain_data_asset(
            OLD_COLUMNS, OLD_ROWS, (1.0, 5.0, 1.0), [0] * (OLD_COLUMNS * OLD_ROWS), old_layout=True,
            legacy=[{"texture": old_tex, "tileSize": (4.0, 4.0)}],
            alphamaps=[(4, 4, _alphamap(4, (255, 0, 0, 0), (255, 0, 0, 0)))], argb=True, name="Old Data",
        ),
        native_meta(old_path, 15600000),
    )

    scene_path = "Assets/Synthetic/Scenes/Terrain.unity"
    yaw45 = (0.0, 0.3826834, 0.0, 0.9238795)
    yaw90 = (0.0, 0.7071068, 0.0, 0.7071068)
    scene = HEADER + "".join([
        game_object(10, "Ground"), transform(11, 10, pos=(10, 1, 20), rot=yaw45, scale=(2, 2, 2)), _terrain(12, 10, ground),
        game_object(20, "Level"), transform(21, 20, pos=(100, 0, 0), rot=yaw90),
        game_object(30, "Old Ground"), transform(31, 30, father=21, pos=(5, 0, 0)), _terrain(32, 30, old),
    ])
    add(scene_path, scene.encode(), f"fileFormatVersion: 2\nguid: {guid_of(scene_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")
    write_package(out, entries)


if __name__ == "__main__":
    main()
