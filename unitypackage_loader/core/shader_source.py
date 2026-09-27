"""パッケージに入っているシェーダーの定義（.shader / .shadergraph）から、宣言されたプロパティ名を読む。

シェーダーを切り替えたマテリアルには、前のシェーダーのプロパティが残る（Unity は切り替えてもプロパティを消さない）。
Standard / URP / HDRP の名前を決まった順に読むと、いまのシェーダーが使わない残りを拾ってしまうので、
定義があるシェーダーでは宣言されたものだけを読む（#118）。シェーダーの処理そのものは解釈しない。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

from .material import UnityMaterial

__all__ = ["SHADER_SOURCE_EXTS", "ShaderSourceError", "declared_properties", "restrict_to_declared"]

SHADER_SOURCE_EXTS = frozenset({".shader", ".shadergraph"})

# URP / HDRP のターゲットが、グラフに無くても Shader Graph のマテリアルへ足す描画設定。
# 値はマテリアルごとの設定（Allow Material Override）なので読んでよい。色やテクスチャ、_EmissionColor（HDRP では
# ベイク用に白で固定）は入れない
GRAPH_TARGET_PROPERTIES = frozenset({
    # URP
    "_Surface", "_Blend", "_AlphaClip", "_Cull", "_SrcBlend", "_DstBlend", "_SrcBlendAlpha", "_DstBlendAlpha",
    "_ZWrite", "_ZTest", "_AlphaToMask", "_QueueOffset", "_QueueControl", "_WorkflowMode", "_CastShadows",
    "_ReceiveShadows",
    # HDRP
    "_SurfaceType", "_BlendMode", "_AlphaCutoffEnable", "_DoubleSidedEnable", "_CullMode", "_CullModeForward",
    "_OpaqueCullMode", "_TransparentCullMode", "_AlphaSrcBlend", "_AlphaDstBlend", "_TransparentZWrite",
    "_RenderQueueType",
})

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_PROPERTIES = re.compile(r"\bProperties\s*\{")
# [Attribute] [Attribute(args)] _Name ("Display", Type) = default
_PROPERTY = re.compile(r"(?:\[[^\]\n]*\]\s*)*\b([A-Za-z_]\w*)\s*\(\s*\"")


class ShaderSourceError(ValueError):
    """シェーダーの定義として読めない。"""


def declared_properties(data: bytes | str, ext: str) -> frozenset[str]:
    """宣言されたプロパティ名。``.shadergraph`` にはターゲットが足す描画設定も加える。

    宣言が 1 つも読めなければ空（呼ぶ側は絞り込みをしない）。壊れた入力には ``ShaderSourceError`` だけを送出する。
    """
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = text.lstrip("﻿")
    ext = ext.lower()
    if ext == ".shader":
        return _shaderlab_properties(text)
    if ext == ".shadergraph":
        names = _graph_properties(text)
        return names | GRAPH_TARGET_PROPERTIES if names else frozenset()
    raise ShaderSourceError(f"not a shader source: {ext!r}")


def _shaderlab_properties(text: str) -> frozenset[str]:
    """ShaderLab の ``Properties { ... }`` に並んだ名前。"""
    text = _COMMENT.sub("", text)
    match = _PROPERTIES.search(text)
    if match is None:
        return frozenset()
    depth, i, in_string = 1, match.end(), False
    while i < len(text) and depth:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    if depth:
        raise ShaderSourceError("unterminated Properties block")
    return frozenset(_PROPERTY.findall(text[match.end():i - 1]))


def _graph_properties(text: str) -> frozenset[str]:
    """Shader Graph のプロパティとキーワードの参照名（上書きがあればそれ、無ければ既定の名前）。

    古い形式は 1 つの JSON（``m_SerializedProperties`` の各要素の ``JSONnodeData`` が JSON の文字列）、
    新しい形式は JSON のオブジェクトを並べたもの。
    """
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    i = 0
    try:
        while True:
            while i < len(text) and text[i].isspace():
                i += 1
            if i >= len(text):
                break
            obj, i = decoder.raw_decode(text, i)
            if isinstance(obj, dict):
                objects.append(obj)
        for obj in list(objects):
            for key in ("m_SerializedProperties", "m_SerializedKeywords"):
                items = obj.get(key)
                for item in items if isinstance(items, list) else ():
                    node = item.get("JSONnodeData") if isinstance(item, dict) else None
                    if isinstance(node, str):
                        parsed = json.loads(node)
                        if isinstance(parsed, dict):
                            objects.append(parsed)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ShaderSourceError(f"not a shader graph: {exc}") from exc
    names = set()
    for obj in objects:
        name = obj.get("m_OverrideReferenceName") or obj.get("m_DefaultReferenceName")
        if isinstance(name, str) and name:
            names.add(name)
    return frozenset(names)


def restrict_to_declared(mat: UnityMaterial, declared: frozenset[str]) -> tuple[UnityMaterial, list[str]]:
    """宣言されたプロパティだけを残した写しと、外したプロパティ名。"""
    def keep(values: dict) -> dict:
        return {k: v for k, v in values.items() if k in declared}

    dropped = sorted(
        {*mat.textures, *mat.floats, *mat.ints, *mat.colors, *mat.texture_slots} - declared
    )
    if not dropped:
        return mat, []
    restricted = replace(
        mat,
        textures=keep(mat.textures),
        floats=keep(mat.floats),
        ints=keep(mat.ints),
        colors=keep(mat.colors),
        texture_slots={k for k in mat.texture_slots if k in declared},
    )
    return restricted, dropped
