"""テスト用に、TerrainData の .asset（バイナリ）と .terrainlayer（YAML）を組み立てる（Issue #205）。

実在アセットのデータは使わない手書きの生成器。単体テストと ``tests/make_synthetic_scene_package.py`` から使う。
"""

from __future__ import annotations

from tests.unity_binary_writer import Node, build_serialized_file, cls, pptr, prim, string, vector

CLASS_TEXTURE_2D = 28
CLASS_TERRAIN_DATA = 156
TERRAIN_DATA_ID = 15600000
_ALPHA_IDS = (7000000001, 7000000002, 7000000003)


def _vec(name: str, keys: str) -> Node:
    return cls("Vector3f" if len(keys) == 3 else "Vector2f", name, *(prim("float", k) for k in keys))


def _typeless(name: str) -> Node:
    return Node("TypelessData", name, [Node("Array", "Array", [prim("int", "size"), prim("UInt8", "data")], is_array=True)])


_TEXTURE = cls(
    "Texture2D", "Base",
    string("m_Name"), prim("int", "m_Width"), prim("int", "m_Height"), prim("int", "m_TextureFormat"),
    cls("StreamingInfo", "m_StreamData", prim("unsigned int", "offset"), prim("unsigned int", "size"), string("path")),
    _typeless("image data"),
)


def _splat_prototype() -> Node:
    return cls(
        "SplatPrototype", "data",
        pptr("Texture2D", "texture"), pptr("Texture2D", "normalMap"), _vec("tileSize", "xy"), _vec("tileOffset", "xy"),
        prim("float", "metallic"), prim("float", "smoothness"),
    )


def terrain_data_asset(
    columns: int,
    rows: int,
    scale: tuple[float, float, float],
    heights: list[int],
    *,
    holes: list[int] | None = None,
    layers: list[str] = (),
    alphamaps: list[tuple[int, int, bytes]] = (),
    argb: bool = False,
    legacy: list[dict] | None = None,
    old_layout: bool = False,
    trees: int = 0,
    name: str = "Terrain",
) -> bytes:
    """TerrainData の .asset。``layers`` は .terrainlayer の GUID、``alphamaps`` は (幅, 高さ, RGBA の並び)。

    ``argb`` ならアルファマップを ARGB32 で書く。``old_layout`` なら 2019.3 より前の ``m_Width`` / ``m_Height`` の形。
    ``legacy`` は SplatPrototype（``texture`` / ``normalMap`` は externals の GUID、``tileSize`` など）。
    """
    externals: list[tuple[str | None, int, str]] = []

    def external(guid: str | None) -> tuple[int, int]:
        if not guid:
            return (0, 0)
        externals.append((guid, 2, ""))
        return (len(externals), 8574412962073106934)

    def texture_external(guid: str | None) -> tuple[int, int]:
        if not guid:
            return (0, 0)
        externals.append((guid, 3, ""))
        return (len(externals), 2800000)

    if old_layout:
        heightmap_node = cls(
            "Heightmap", "m_Heightmap", vector("m_Heights", prim("SInt16", "data"), align=True),
            _vec("m_Scale", "xyz"), prim("int", "m_Width"), prim("int", "m_Height"),
        )
        heightmap = {"m_Heights": heights, "m_Scale": dict(zip("xyz", scale)), "m_Width": columns, "m_Height": rows}
    else:
        heightmap_node = cls(
            "Heightmap", "m_Heightmap", vector("m_Heights", prim("SInt16", "data"), align=True),
            vector("m_Holes", prim("UInt8", "data"), align=True), _vec("m_Scale", "xyz"), prim("int", "m_Resolution"),
        )
        heightmap = {
            "m_Heights": heights, "m_Holes": holes if holes is not None else [], "m_Scale": dict(zip("xyz", scale)),
            "m_Resolution": columns,
        }
    splat_node = cls(
        "SplatDatabase", "m_SplatDatabase",
        vector("m_TerrainLayers", pptr("TerrainLayer", "data")),
        vector("m_Splats", _splat_prototype()),
        vector("m_AlphaTextures", pptr("Texture2D", "data")),
    )
    splat = {
        "m_TerrainLayers": [external(g) for g in layers],
        "m_Splats": [
            {
                "texture": texture_external(p.get("texture")), "normalMap": texture_external(p.get("normalMap")),
                "tileSize": dict(zip("xy", p.get("tileSize", (15.0, 15.0)))),
                "tileOffset": dict(zip("xy", p.get("tileOffset", (0.0, 0.0)))),
                "metallic": p.get("metallic", 0.0), "smoothness": p.get("smoothness", 0.0),
            }
            for p in legacy or []
        ],
        "m_AlphaTextures": [(0, _ALPHA_IDS[i]) for i in range(len(alphamaps))],
    }
    tree = cls("TreeInstance", "data", _vec("position", "xyz"))
    detail_node = cls(
        "DetailDatabase", "m_DetailDatabase", vector("m_TreeInstances", tree), vector("m_DetailPrototypes", prim("int", "data")),
    )
    details = {"m_TreeInstances": [{"position": {"x": 0.5, "y": 0.0, "z": 0.5}}] * trees, "m_DetailPrototypes": []}
    root = cls("TerrainData", "Base", string("m_Name"), splat_node, detail_node, heightmap_node)
    objects = [(TERRAIN_DATA_ID, CLASS_TERRAIN_DATA, root, {
        "m_Name": name, "m_SplatDatabase": splat, "m_DetailDatabase": details, "m_Heightmap": heightmap,
    })]
    for i, (width, height, rgba) in enumerate(alphamaps):
        pixels = rgba
        if argb:
            pixels = bytes(b for p in range(0, len(rgba), 4) for b in (rgba[p + 3], rgba[p], rgba[p + 1], rgba[p + 2]))
        objects.append((_ALPHA_IDS[i], CLASS_TEXTURE_2D, _TEXTURE, {
            "m_Name": f"SplatAlpha {i}", "m_Width": width, "m_Height": height, "m_TextureFormat": 5 if argb else 4,
            "m_StreamData": {"offset": 0, "size": 0, "path": ""}, "image data": list(pixels),
        }))
    return build_serialized_file(objects, externals, version=22, unity_version="2021.3.0f1")


def terrain_layer_yaml(
    name: str,
    diffuse: str | None,
    normal: str | None = None,
    mask: str | None = None,
    tile_size: tuple[float, float] = (4.0, 4.0),
    tile_offset: tuple[float, float] = (0.0, 0.0),
    metallic: float = 0.0,
    smoothness: float = 0.0,
) -> str:
    def ref(guid: str | None) -> str:
        return f"{{fileID: 2800000, guid: {guid}, type: 3}}" if guid else "{fileID: 0}"

    return (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!1953259897 &8574412962073106934\nTerrainLayer:\n"
        f"  m_Name: {name}\n  m_DiffuseTexture: {ref(diffuse)}\n  m_NormalMapTexture: {ref(normal)}\n"
        f"  m_MaskMapTexture: {ref(mask)}\n  m_TileSize: {{x: {tile_size[0]}, y: {tile_size[1]}}}\n"
        f"  m_TileOffset: {{x: {tile_offset[0]}, y: {tile_offset[1]}}}\n  m_Specular: {{r: 0, g: 0, b: 0, a: 0}}\n"
        f"  m_Metallic: {metallic}\n  m_Smoothness: {smoothness}\n  m_NormalScale: 1\n"
        "  m_DiffuseRemapMin: {x: 0, y: 0, z: 0, w: 0}\n  m_DiffuseRemapMax: {x: 1, y: 1, z: 1, w: 1}\n"
        "  m_MaskMapRemapMin: {x: 0, y: 0, z: 0, w: 0}\n  m_MaskMapRemapMax: {x: 1, y: 1, z: 1, w: 1}\n"
    )
