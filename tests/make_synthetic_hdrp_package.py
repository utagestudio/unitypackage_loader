"""HDRP の読み込みを確かめる合成 unitypackage を Blender で生成する（Issue #119）。

    blender -b --factory-startup --python tests/make_synthetic_hdrp_package.py -- <出力パス.unitypackage>

HDRP の GUID のマテリアルがあるとパッケージ全体のライトが HDRP の単位（lux / lumen）になるので、Built-in / URP の合成パッケージとは分ける。
どの .mat にも、互換用の _Color（黒）/ _MainTex（別の画像）と Standard の _Mode / _Glossiness が残っている。

- HdrpLit.fbx（立方体）: HdrpMaskMat（マスクマップと Remap・ノーマルマップ・両面）と HdrpSpecMat（Material Type が Specular Color）
- HdrpGlass.fbx（球）: HdrpGlassMat（半透明。残った _Mode は 0）と HdrpUnlitMat（HDRP/Unlit）
- HdrpLayered.fbx（円錐）: HdrpLayeredMat（HDRP/LayeredLit。レイヤー 0 を読む）

Assets/Synthetic/Scenes/Hdrp.unity に 3 つの FBX を並べ、平行光源・点光源・面光源を 1 つずつ置く（HDRP の強さの換算を確かめる。面光源は #129）。
ライトには HDRP の追加データ（HDAdditionalLightData）を付ける（#120）。
露出の Volume を 2 つ置く（#130）。グローバルな Volume（Exposure が固定露出 EV 9・補正 1 のプロファイル）と、優先度の高い
ローカルな Volume（固定露出 EV 3）。使うのはグローバルな方で、Blender の露出は log2(683 / 1.2) − 8 になる。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import bpy  # noqa: F401 - export_model が使う

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.make_synthetic_package import export_model, guid_of, model_meta, png_bytes, texture_meta, write_package  # noqa: E402
from tests.make_synthetic_scene_package import (  # noqa: E402
    HEADER,
    ROOT_GAME_OBJECT,
    ROOT_TRANSFORM,
    game_object,
    instance,
    light_doc,
    mod,
    mono_behaviour,
    transform,
)
from tests.make_synthetic_urp_package import texture  # noqa: E402

HDRP_LIT = "{fileID: 4800000, guid: 6e4ae4064600d784cac1e41a9e6f2e59, type: 3}"
HDRP_LAYERED = "{fileID: 4800000, guid: 81d02e8644315b742b154842a3a2f98c, type: 3}"
HDRP_UNLIT = "{fileID: 4800000, guid: c4edd00ff2db5b24391a4fcb1762e459, type: 3}"
HDRP_LIGHT_DATA = "7a68c43fe1f2a47cfa234b5eeaa98012"  # HDAdditionalLightData
VOLUME_SCRIPT = "172515602e62fb746b5d573b38a5fe58"  # Volume
EXPOSURE_SCRIPT = "2d08ce26990eb1a4a9177b860541e702"  # Exposure


def exposure_profile_yaml(name: str, fixed: float, compensation: float) -> str:
    """Exposure だけを持つ Volume のプロファイル（.asset）。"""
    def param(key, value):
        return f"  {key}:\n    m_OverrideState: 1\n    m_Value: {value}\n"
    return (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        f"--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: {name}\n  components:\n  - {{fileID: 1}}\n"
        "--- !u!114 &1\nMonoBehaviour:\n  m_Name: Exposure\n"
        f"  m_Script: {{fileID: 11500000, guid: {EXPOSURE_SCRIPT}, type: 3}}\n  active: 1\n"
        + param("mode", 0) + param("fixedExposure", fixed) + param("compensation", compensation)
    )


def volume_doc(file_id: int, go: int, profile: str, is_global: int, priority: int) -> str:
    return (
        f"--- !u!114 &{file_id}\nMonoBehaviour:\n  m_GameObject: {{fileID: {go}}}\n  m_Enabled: 1\n"
        f"  m_Script: {{fileID: 11500000, guid: {VOLUME_SCRIPT}, type: 3}}\n"
        f"  m_IsGlobal: {is_global}\n  priority: {priority}\n  weight: 1\n"
        f"  sharedProfile: {{fileID: 11400000, guid: {profile}, type: 2}}\n"
    )


def hdrp_mat_yaml(name: str, shader: str, *, textures: str, floats: dict, colors: dict) -> str:
    values = {"_SurfaceType": 0, "_BlendMode": 0, "_AlphaCutoffEnable": 0, "_DoubleSidedEnable": 0, "_CullMode": 2,
              "_Mode": 3, "_Glossiness": 0.9, **floats}
    all_colors = {"_Color": (0, 0, 0, 1), "_EmissionColor": (1, 1, 1, 1), **colors}
    return (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!21 &2100000\nMaterial:\n  serializedVersion: 8\n"
        f"  m_Name: {name}\n  m_Shader: {shader}\n"
        "  m_ValidKeywords: []\n  m_InvalidKeywords: []\n  m_CustomRenderQueue: -1\n  m_SavedProperties:\n    serializedVersion: 3\n"
        "    m_TexEnvs:\n" + textures + "    m_Ints: []\n    m_Floats:\n"
        + "".join(f"    - {k}: {v}\n" for k, v in sorted(values.items()))
        + "    m_Colors:\n"
        + "".join(f"    - {k}: {{r: {c[0]}, g: {c[1]}, b: {c[2]}, a: {c[3]}}}\n" for k, c in sorted(all_colors.items()))
    )


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else REPO_ROOT / "_local" / "unitypackages" / "synthetic_hdrp.unitypackage"
    tmp = Path(tempfile.mkdtemp(prefix="synthetic_hdrp_"))
    entries: dict[str, tuple[str, bytes | None, str | None]] = {}

    def add(pathname: str, asset: bytes | None, meta: str | None) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    def add_tex(name: str, rgb, normal: bool = False) -> str:
        path = f"Assets/Synthetic/Textures/{name}"
        return add(path, png_bytes(8, rgb), texture_meta(guid_of(path), normal))

    color = add_tex("HdrpColor.png", (200, 160, 80))
    leftover = add_tex("Leftover.png", (255, 0, 255))
    normal = add_tex("HdrpNormal.png", (128, 128, 255), normal=True)
    mask = add_tex("HdrpMask.png", (255, 255, 0))
    spec = add_tex("HdrpSpec.png", (255, 200, 100))
    main_tex = texture("_MainTex", leftover)

    materials = {
        # マスクマップ（R = Metallic、A = Smoothness を Remap）、ノーマルマップ、両面。残った _Metallic / _Smoothness は使われない
        "HdrpMaskMat": hdrp_mat_yaml(
            "HdrpMaskMat", HDRP_LIT,
            textures=texture("_BaseColorMap", color, (2, 2)) + main_tex + texture("_MaskMap", mask) + texture("_NormalMap", normal),
            floats={"_Metallic": 0.9, "_Smoothness": 0.9, "_MetallicRemapMin": 0.1, "_MetallicRemapMax": 0.8,
                    "_SmoothnessRemapMin": 0.2, "_SmoothnessRemapMax": 0.7, "_NormalScale": 0.4, "_DoubleSidedEnable": 1, "_CullMode": 0},
            colors={"_BaseColor": (1, 1, 1, 1)},
        ),
        # Material Type が Specular Color。F0 = マップの RGB × _SpecularColor の非金属
        "HdrpSpecMat": hdrp_mat_yaml(
            "HdrpSpecMat", HDRP_LIT, textures=texture("_BaseColorMap", color) + main_tex + texture("_SpecularColorMap", spec),
            floats={"_MaterialID": 4, "_Metallic": 0.9, "_Smoothness": 0.6}, colors={"_BaseColor": (1, 1, 1, 1), "_SpecularColor": (0.4, 0.2, 0.1, 1)},
        ),
        # 半透明。残った _Mode: 0 で不透明にしない
        "HdrpGlassMat": hdrp_mat_yaml(
            "HdrpGlassMat", HDRP_LIT, textures=texture("_BaseColorMap", color) + main_tex,
            floats={"_SurfaceType": 1, "_Mode": 0}, colors={"_BaseColor": (1, 1, 1, 0.3)},
        ),
        "HdrpUnlitMat": hdrp_mat_yaml(
            "HdrpUnlitMat", HDRP_UNLIT, textures=texture("_UnlitColorMap", color) + main_tex,
            floats={}, colors={"_UnlitColor": (0.5, 1, 0.5, 1)},
        ),
        # LayeredLit。レイヤー 0（_BaseColor0 など）を読み、残った _Color（黒）/ _BaseColor は使わない
        "HdrpLayeredMat": hdrp_mat_yaml(
            "HdrpLayeredMat", HDRP_LAYERED,
            textures=texture("_BaseColorMap0", color, (5, 10)) + texture("_BaseColorMap1", leftover) + main_tex,
            floats={"_LayerCount": 2, "_Metallic0": 0.6, "_Smoothness0": 0.25},
            colors={"_BaseColor0": (0.9, 0.9, 0.9, 1), "_BaseColor": (0.2, 0.2, 0.2, 1)},
        ),
    }
    mats = {}
    for name, text in materials.items():
        path = f"Assets/Synthetic/Materials/{name}.mat"
        mats[name] = add(path, text.encode(), f"fileFormatVersion: 2\nguid: {guid_of(path)}\nNativeFormatImporter:\n  mainObjectFileID: 2100000\n")

    models = {}
    for label, kind, first, second in (
        ("HdrpLit", "cube", "HdrpMaskMat", "HdrpSpecMat"),
        ("HdrpGlass", "sphere", "HdrpGlassMat", "HdrpUnlitMat"),
        ("HdrpLayered", "cone", "HdrpLayeredMat", None),
    ):
        path = tmp / f"{label.lower()}.fbx"
        export_model(kind, first, path, name=f"Synthetic{label}", first_used_mat=second)
        model_path = f"Assets/Synthetic/Models/{label}.fbx"
        used = {first: mats[first], **({second: mats[second]} if second else {})}
        models[label] = add(model_path, path.read_bytes(), model_meta(guid_of(model_path), used))

    profiles = {}
    for name, fixed, compensation in (("GlobalExposure", 9, 1), ("LocalExposure", 3, 0)):
        path = f"Assets/Synthetic/Settings/{name}.asset"
        profiles[name] = add(path, exposure_profile_yaml(name, fixed, compensation).encode(),
                             f"fileFormatVersion: 2\nguid: {guid_of(path)}\nNativeFormatImporter:\n  mainObjectFileID: 11400000\n")

    scene_path = "Assets/Synthetic/Scenes/Hdrp.unity"
    docs = []
    for index, (label, x) in enumerate((("HdrpLit", 0), ("HdrpGlass", 3), ("HdrpLayered", 6))):
        model = models[label]
        docs.append(instance(1000 + index * 10, model, 0, [
            mod(ROOT_TRANSFORM, model, "m_LocalPosition.x", x),
            mod(ROOT_GAME_OBJECT, model, "m_Name", label),
        ]))
    docs += [
        game_object(8000, "Directional Light"),
        transform(8001, 8000, pos=(0, 3, 0), rot=(0.40821788, -0.23456968, 0.10938163, 0.8754261)),
        light_doc(8002, 8000, 1, intensity=100000),  # lux
        mono_behaviour(8003, 8000, HDRP_LIGHT_DATA),
        game_object(8100, "Point Light"),
        transform(8101, 8100, pos=(2, 1, 0)),
        light_doc(8102, 8100, 2, intensity=600, light_range=5),  # candela
        mono_behaviour(8103, 8100, HDRP_LIGHT_DATA),
        # 面光源（#129）。HDRP の m_Intensity は nits
        game_object(8200, "Area Light"),
        transform(8201, 8200, pos=(0, 2, 0), rot=(0.7071068, 0, 0, 0.7071068)),
        light_doc(8202, 8200, 3, intensity=1366, area=(2, 0.5), lightmapping=2, shadow=0),
        mono_behaviour(8203, 8200, HDRP_LIGHT_DATA),
        game_object(8300, "Global Volume"),
        transform(8301, 8300),
        volume_doc(8302, 8300, profiles["GlobalExposure"], is_global=1, priority=0),
        game_object(8400, "Local Volume"),
        transform(8401, 8400),
        volume_doc(8402, 8400, profiles["LocalExposure"], is_global=0, priority=10),
    ]
    add(scene_path, (HEADER + "".join(docs)).encode(),
        f"fileFormatVersion: 2\nguid: {guid_of(scene_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")

    write_package(out, entries)


if __name__ == "__main__":
    main()
