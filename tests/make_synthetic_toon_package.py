"""トゥーン系のマテリアルと、マテリアルの組み立ての設定を確かめる合成 unitypackage を Blender で生成する（#199）。

    blender -b --factory-startup --python tests/make_synthetic_toon_package.py -- <出力パス.unitypackage>

読み込みの設定（マテリアルモード、アウトライン、Force Opaque、法線・発光を使わない、画像の取り込み、同名のマテリアルの再利用）と、
読み込んだ後の再構築・アウトライン・Backface Culling のオペレーターを確かめるためのもの。ほかの合成パッケージは
Auto のモードで読むものだけで、Toon のノードグループとアウトラインを通っていなかった。

- ToonBody.fbx（モンキー）: LilBodyMat（lilToon の lts_o。アウトライン・法線・発光・MatCap・影・リム）と
  LilHairMat（lilToon の lts_trans。半透明・両面）。ポリゴンで最初に使われるのは LilHairMat
- ToonFace.fbx（球）: MToonFaceMat（VRM/MToon。アウトラインの幅のモードが 1）
- ToonProp.fbx（立方体）: ToonStdMat（Standard）と OrphanMat（パッケージに .mat が無く、externalObjects にも無い）
- 画像: ToonColor.png、ToonNormal.png（ノーマルマップ）、ToonEmit.png、ToonMatCap.png と、どの .mat も使わない ToonSpare.png
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
from tests.test_material import mat_yaml  # noqa: E402

LTS_O = "{fileID: 4800000, guid: efa77a80ca0344749b4f19fdd5891cbe, type: 3}"  # lilToon lts_o（不透明・アウトライン付き）
LTS_TRANS = "{fileID: 4800000, guid: 165365ab7100a044ca85fc8c33548a62, type: 3}"  # lilToon lts_trans（半透明）
MTOON = "{fileID: 4800000, guid: 1a97144e4ad27a04aafd70f7b915cedb, type: 3}"  # VRM/MToon
STANDARD = "{fileID: 46, guid: 0000000000000000f000000000000000, type: 0}"


def tex(prop: str, guid: str | None, scale=(1, 1)):
    return (prop, guid, scale, (0, 0))


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out = Path(argv[0]) if argv else REPO_ROOT / "_local" / "unitypackages" / "synthetic_toon.unitypackage"
    tmp = Path(tempfile.mkdtemp(prefix="synthetic_toon_"))
    entries: dict[str, tuple[str, bytes | None, str | None]] = {}

    def add(pathname: str, asset: bytes | None, meta: str | None) -> str:
        guid = guid_of(pathname)
        entries[guid] = (pathname, asset, meta)
        return guid

    def add_tex(name: str, rgb, normal: bool = False) -> str:
        path = f"Assets/Synthetic/Toon/Textures/{name}"
        return add(path, png_bytes(8, rgb), texture_meta(guid_of(path), normal))

    color = add_tex("ToonColor.png", (240, 200, 180))
    normal = add_tex("ToonNormal.png", (128, 128, 255), normal=True)
    emit = add_tex("ToonEmit.png", (255, 64, 32))
    matcap = add_tex("ToonMatCap.png", (200, 200, 255))
    add_tex("ToonSpare.png", (10, 200, 10))  # どの .mat も使わない（Import Unreferenced Images で読む）

    materials = {
        "LilBodyMat": mat_yaml(
            "LilBodyMat", LTS_O,
            tex=[tex("_MainTex", color), tex("_BumpMap", normal), tex("_EmissionMap", emit), tex("_MatCapTex", matcap)],
            floats=[("_lilToonVersion", 45), ("_Cull", 2), ("_Cutoff", 0.5), ("_UseBumpMap", 1), ("_BumpScale", 0.8),
                    ("_UseEmission", 1), ("_EmissionBlend", 1), ("_UseReflection", 0), ("_UseMatCap", 1), ("_MatCapBlend", 0.5),
                    ("_UseShadow", 1), ("_ShadowStrength", 0.7), ("_ShadowBorder", 0.4), ("_ShadowBlur", 0.1),
                    ("_UseRim", 1), ("_RimBorder", 0.6), ("_UseOutline", 1), ("_OutlineWidth", 0.1)],
            colors=[("_Color", (1, 1, 1, 1)), ("_ShadowColor", (0.8, 0.6, 0.6, 1)), ("_EmissionColor", (1, 0.5, 0.25, 1)),
                    ("_MatCapColor", (1, 1, 1, 1)), ("_RimColor", (1, 1, 0.9, 1)), ("_OutlineColor", (0.3, 0.2, 0.2, 1))],
        ),
        "LilHairMat": mat_yaml(
            "LilHairMat", LTS_TRANS,
            tex=[tex("_MainTex", color)],
            floats=[("_lilToonVersion", 45), ("_Cull", 0), ("_Cutoff", 0.001), ("_SrcBlend", 1), ("_DstBlend", 10), ("_UseReflection", 0)],
            colors=[("_Color", (0.9, 0.8, 0.7, 1))],
            queue=2460,
        ),
        "MToonFaceMat": mat_yaml(
            "MToonFaceMat", MTOON,
            tex=[tex("_MainTex", color), tex("_ShadeTexture", color)],
            floats=[("_MToonVersion", 38), ("_BlendMode", 0), ("_CullMode", 2), ("_Cutoff", 0.5), ("_ShadeShift", 0), ("_ShadeToony", 0.9),
                    ("_OutlineWidthMode", 1), ("_OutlineWidth", 0.2)],
            colors=[("_Color", (1, 1, 1, 1)), ("_ShadeColor", (0.7, 0.6, 0.6, 1)), ("_OutlineColor", (0.1, 0.1, 0.2, 1)),
                    ("_RimColor", (0, 0, 0, 1)), ("_EmissionColor", (0, 0, 0, 1))],
        ),
        "ToonStdMat": mat_yaml(
            "ToonStdMat", STANDARD,
            tex=[tex("_MainTex", color), tex("_BumpMap", normal), tex("_EmissionMap", emit)],
            floats=[("_Mode", 0), ("_Metallic", 0.1), ("_Glossiness", 0.5), ("_BumpScale", 1)],
            colors=[("_Color", (1, 1, 1, 1)), ("_EmissionColor", (1, 0.5, 0.25, 1))],
            keywords=["_EMISSION", "_NORMALMAP"],
        ),
    }
    mats = {}
    for name, text in materials.items():
        path = f"Assets/Synthetic/Toon/Materials/{name}.mat"
        mats[name] = add(path, text.encode(), f"fileFormatVersion: 2\nguid: {guid_of(path)}\nNativeFormatImporter:\n  mainObjectFileID: 2100000\n")

    for label, kind, first, second in (
        ("ToonBody", "monkey", "LilBodyMat", "LilHairMat"),
        ("ToonFace", "sphere", "MToonFaceMat", None),
        ("ToonProp", "cube", "ToonStdMat", "OrphanMat"),
    ):
        path = tmp / f"{label.lower()}.fbx"
        export_model(kind, first, path, name=f"Synthetic{label}", first_used_mat=second)
        model_path = f"Assets/Synthetic/Toon/Models/{label}.fbx"
        used = {name: mats[name] for name in (first, second) if name in mats}  # OrphanMat は .mat が無いので載せない
        add(model_path, path.read_bytes(), model_meta(guid_of(model_path), used))

    write_package(out, entries)


if __name__ == "__main__":
    main()
