"""URP Lit の読み込みを確かめる合成 unitypackage を Blender で生成する（Issue #116）。

    blender -b --factory-startup --python tests/make_synthetic_urp_package.py -- <出力パス.unitypackage>

URP Lit の GUID のマテリアルがあるとパッケージ全体のライトが URP の単位になるので、Built-in のシーン用の合成パッケージとは分ける。
どの .mat にも、Standard から変換したときの値（_MainTex / _Color / _Mode / _Glossiness / _GlossMapScale）が残っている。

- UrpLit.fbx（立方体）: UrpLitMat（Metallic マップ・ノーマルマップ・両面。残った _MainTex は別の画像）と UrpClipMat（アルファクリップ）
- UrpGlass.fbx（球）: UrpGlassMat（半透明・_Blend 2 = Additive。残った _Mode は 0）と UrpUnlitMat（URP Unlit）
- UrpDerived.fbx（円錐）: UrpDerivedMat（表に無い URP Lit 派生のシェーダー。_EMISSION キーワードで光る）
- UrpSpec.fbx（トーラス）: UrpSpecMat（Lit の Specular ワークフロー。_SpecColor）と UrpSimpleMat（Simple Lit。スペキュラーマップ × _SpecColor）（#117）
- UrpBaked.fbx（アイコ球）: UrpBakedMat（Baked Lit。スペキュラー無し）（#117）

Assets/Synthetic/Scenes/Urp.unity に FBX を並べ、平行光源と点光源を 1 つずつ置く（URP の強さの換算を確かめる）。ライトには
URP の追加データ（UniversalAdditionalLightData）を付ける（#120）。

synthetic_urp_standard.unitypackage は、同じ中身でマテリアルのシェーダーだけを Built-in の Standard にしたもの。マテリアルからは
Built-in に見えるが、ライトの追加データから URP として換算し、食い違いを警告することを確かめる（#120）。
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

URP_LIT = "{fileID: 4800000, guid: 933532a4fcc9baf4fa0491de14d08ed7, type: 3}"
URP_UNLIT = "{fileID: 4800000, guid: 650dd9526735d5b46b79224bc6e94025, type: 3}"
URP_DERIVED = "{fileID: 4800000, guid: 7777777777777777bbbbbbbbbbbbbbbb, type: 3}"
URP_SIMPLE = "{fileID: 4800000, guid: 8d2bb70cbf9db8d4da26e15b26e74248, type: 3}"
URP_BAKED = "{fileID: 4800000, guid: 0ca6dca7396eb48e5849247ffd444914, type: 3}"
STANDARD = "{fileID: 46, guid: 0000000000000000f000000000000000, type: 0}"
URP_LIGHT_DATA = "474bcb49853aa07438625e644c072ee6"  # UniversalAdditionalLightData


def texture(prop: str, guid: str | None, scale=(1, 1)) -> str:
    ref = f"{{fileID: 2800000, guid: {guid}, type: 3}}" if guid else "{fileID: 0}"
    return f"    - {prop}:\n        m_Texture: {ref}\n        m_Scale: {{x: {scale[0]}, y: {scale[1]}}}\n        m_Offset: {{x: 0, y: 0}}\n"


def urp_mat_yaml(name: str, shader: str, *, base: str, leftover_main: str, textures: str = "", floats=(), base_color=(1, 1, 1, 1),
                 emission=(0, 0, 0), keywords=(), spec_color=(0.2, 0.2, 0.2, 1)) -> str:
    values = {"_WorkflowMode": 1, "_Surface": 0, "_Blend": 0, "_AlphaClip": 0, "_Cutoff": 0.5, "_Cull": 2, "_Metallic": 0,
              "_Smoothness": 0.5, "_BumpScale": 1, "_Mode": 3, "_Glossiness": 0.9, "_GlossMapScale": 1, **dict(floats)}
    floats_text = "".join(f"    - {k}: {v}\n" for k, v in sorted(values.items()))
    r, g, b, a = base_color
    return (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!21 &2100000\nMaterial:\n  serializedVersion: 8\n"
        f"  m_Name: {name}\n  m_Shader: {shader}\n"
        "  m_ValidKeywords:" + ("".join(f"\n  - {k}" for k in keywords) if keywords else " []") + "\n"
        "  m_InvalidKeywords: []\n  m_CustomRenderQueue: -1\n  m_SavedProperties:\n    serializedVersion: 3\n    m_TexEnvs:\n"
        + texture("_BaseMap", base, (2, 2)) + texture("_MainTex", leftover_main) + textures
        + "    m_Ints: []\n    m_Floats:\n" + floats_text
        + "    m_Colors:\n"
        f"    - _BaseColor: {{r: {r}, g: {g}, b: {b}, a: {a}}}\n"
        "    - _Color: {r: 0, g: 0, b: 0, a: 1}\n"
        f"    - _EmissionColor: {{r: {emission[0]}, g: {emission[1]}, b: {emission[2]}, a: 1}}\n"
        f"    - _SpecColor: {{r: {spec_color[0]}, g: {spec_color[1]}, b: {spec_color[2]}, a: {spec_color[3]}}}\n"
    )


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else REPO_ROOT / "_local" / "unitypackages" / "synthetic_urp.unitypackage"
    build(out)
    build(out.with_name(out.stem + "_standard" + out.suffix), standard=True)


def build(out: Path, standard: bool = False) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="synthetic_urp_"))
    entries: dict[str, tuple[str, bytes | None, str | None]] = {}

    def add(pathname: str, asset: bytes | None, meta: str | None) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    def add_tex(name: str, rgb, normal: bool = False) -> str:
        path = f"Assets/Synthetic/Textures/{name}"
        return add(path, png_bytes(8, rgb), texture_meta(guid_of(path), normal))

    color = add_tex("UrpColor.png", (80, 160, 200))
    leftover = add_tex("Leftover.png", (255, 0, 255))
    normal = add_tex("UrpNormal.png", (128, 128, 255), normal=True)
    mask = add_tex("UrpMask.png", (255, 0, 0))
    spec = add_tex("UrpSpec.png", (255, 128, 64))

    materials = {
        # Metallic マップ（R = Metallic、A × _Smoothness = Smoothness）、ノーマルマップ、両面
        "UrpLitMat": urp_mat_yaml(
            "UrpLitMat", URP_LIT, base=color, leftover_main=leftover,
            textures=texture("_MetallicGlossMap", mask) + texture("_BumpMap", normal),
            floats={"_Metallic": 0.2, "_Smoothness": 0.3, "_Cull": 0, "_BumpScale": 0.5},
            keywords=["_METALLICSPECGLOSSMAP", "_NORMALMAP"],
        ),
        "UrpClipMat": urp_mat_yaml("UrpClipMat", URP_LIT, base=color, leftover_main=leftover,
                                   floats={"_AlphaClip": 1, "_Cutoff": 0.3, "_Mode": 0}),
        # 半透明の Additive。残った _Mode: 0 で不透明にしない
        "UrpGlassMat": urp_mat_yaml("UrpGlassMat", URP_LIT, base=color, leftover_main=leftover,
                                    floats={"_Surface": 1, "_Blend": 2, "_Mode": 0}, base_color=(1, 1, 1, 0.4)),
        "UrpUnlitMat": urp_mat_yaml("UrpUnlitMat", URP_UNLIT, base=color, leftover_main=leftover),
        # 表に無い URP Lit 派生のシェーダー。_WorkflowMode / _Surface の指紋で URP として読む
        "UrpDerivedMat": urp_mat_yaml("UrpDerivedMat", URP_DERIVED, base=color, leftover_main=leftover,
                                      emission=(1, 0.5, 0.25), keywords=["_EMISSION"]),
        # Lit の Specular ワークフロー。F0 = _SpecColor（マップが無い）。残った _Metallic: 0.8 は使わない
        "UrpSpecMat": urp_mat_yaml("UrpSpecMat", URP_LIT, base=color, leftover_main=leftover, spec_color=(0.36, 0.18, 0.09, 1),
                                   floats={"_WorkflowMode": 0, "_Metallic": 0.8, "_Smoothness": 0.7}, keywords=["_SPECULAR_SETUP"]),
        # Simple Lit。F0 = マップの RGB × _SpecColor、Smoothness = マップの A × _Smoothness
        "UrpSimpleMat": urp_mat_yaml("UrpSimpleMat", URP_SIMPLE, base=color, leftover_main=leftover, spec_color=(0.5, 0.5, 0.25, 0.4),
                                     textures=texture("_SpecGlossMap", spec), floats={"_Smoothness": 0.4, "_SpecularHighlights": 1},
                                     keywords=["_SPECGLOSSMAP"]),
        # Baked Lit。ライトマップだけで照らされ、スペキュラーを持たない
        "UrpBakedMat": urp_mat_yaml("UrpBakedMat", URP_BAKED, base=color, leftover_main=leftover, floats={"_Metallic": 0.8}),
    }
    if standard:
        for shader in (URP_LIT, URP_UNLIT, URP_DERIVED, URP_SIMPLE, URP_BAKED):
            materials = {name: text.replace(shader, STANDARD) for name, text in materials.items()}
    mats = {}
    for name, text in materials.items():
        path = f"Assets/Synthetic/Materials/{name}.mat"
        mats[name] = add(path, text.encode(), f"fileFormatVersion: 2\nguid: {guid_of(path)}\nNativeFormatImporter:\n  mainObjectFileID: 2100000\n")

    models = {}
    for label, kind, first, second in (
        ("UrpLit", "cube", "UrpLitMat", "UrpClipMat"),
        ("UrpGlass", "sphere", "UrpGlassMat", "UrpUnlitMat"),
        ("UrpDerived", "cone", "UrpDerivedMat", None),
        ("UrpSpec", "torus", "UrpSpecMat", "UrpSimpleMat"),
        ("UrpBaked", "icosphere", "UrpBakedMat", None),
    ):
        path = tmp / f"{label.lower()}.fbx"
        export_model(kind, first, path, name=f"Synthetic{label}", first_used_mat=second)
        model_path = f"Assets/Synthetic/Models/{label}.fbx"
        used = {first: mats[first], **({second: mats[second]} if second else {})}
        models[label] = add(model_path, path.read_bytes(), model_meta(guid_of(model_path), used))

    scene_path = "Assets/Synthetic/Scenes/Urp.unity"
    docs = []
    for index, (label, x) in enumerate((("UrpLit", 0), ("UrpGlass", 3), ("UrpDerived", 6), ("UrpSpec", 9), ("UrpBaked", 12))):
        model = models[label]
        docs.append(instance(1000 + index * 10, model, 0, [
            mod(ROOT_TRANSFORM, model, "m_LocalPosition.x", x),
            mod(ROOT_GAME_OBJECT, model, "m_Name", label),
        ]))
    docs += [
        game_object(8000, "Directional Light"),
        transform(8001, 8000, pos=(0, 3, 0), rot=(0.40821788, -0.23456968, 0.10938163, 0.8754261)),
        light_doc(8002, 8000, 1, intensity=2),
        mono_behaviour(8003, 8000, URP_LIGHT_DATA),
        game_object(8100, "Point Light"),
        transform(8101, 8100, pos=(2, 1, 0)),
        light_doc(8102, 8100, 2, intensity=2, light_range=5),
        mono_behaviour(8103, 8100, URP_LIGHT_DATA),
    ]
    add(scene_path, (HEADER + "".join(docs)).encode(),
        f"fileFormatVersion: 2\nguid: {guid_of(scene_path)}\nDefaultImporter:\n  externalObjects: {{}}\n")

    write_package(out, entries)


if __name__ == "__main__":
    main()
