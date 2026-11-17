"""パッケージに入っているシェーダーの定義（.shader / .shadergraph）から、宣言されたプロパティ名を読む。

シェーダーを切り替えたマテリアルには、前のシェーダーのプロパティが残る（Unity は切り替えてもプロパティを消さない）。
Standard / URP / HDRP の名前を決まった順に読むと、いまのシェーダーが使わない残りを拾ってしまうので、
定義があるシェーダーでは宣言されたものだけを読む（#118）。シェーダーの処理そのものは解釈しない。

.shader からは、マテリアルが上書きしていないときに Unity が使う描画の既定（SubShader の ``RenderType`` / ``Queue`` タグと、固定の ``Cull``）も読む
（``shader_defaults``。#202）。アルファで切り抜くかどうかがシェーダーの中だけで決まっている自作シェーダーのため。
タグが Opaque でも、パスが常に切り抜く（``#define _ALPHATEST_ON`` か、無効の選択肢の無い ``#pragma multi_compile… _ALPHATEST_ON``）
シェーダーは切り抜きにする。``[HideInInspector]`` で宣言されたプロパティ（Amplify Shader Editor のテンプレートが足す白い
``_EmissionColor`` など）も覚えておく（#235 / #241）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from .material import UnityMaterial

__all__ = [
    "SHADER_SOURCE_EXTS", "ShaderDefaults", "ShaderSourceError", "declared_properties", "restrict_to_declared", "shader_defaults",
]

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
_PROPERTY_WITH_ATTRIBUTES = re.compile(r"((?:\[[^\]\n]*\]\s*)*)\b([A-Za-z_]\w*)\s*\(\s*\"")
# パスが常に切り抜く。multi_compile にキーワードを 1 つだけ書くと「無効」の選択肢が無く、常に有効になる
# （shader_feature は無効の選択肢が自動で付くので、マテリアルのキーワード次第）
_ALWAYS_ALPHA_TEST = re.compile(
    r"^[ \t]*#[ \t]*(?:define[ \t]+_ALPHATEST_ON\b|pragma[ \t]+multi_compile\w*[ \t]+_ALPHATEST_ON[ \t]*$)", re.M
)


# ShaderLab の Queue の名前と値（"AlphaTest+1" のように足し引きできる）
_QUEUE_NAMES = {"background": 1000, "geometry": 2000, "alphatest": 2450, "geometrylast": 2500, "transparent": 3000, "overlay": 4000}
_SUBSHADER = re.compile(r"\bSubShader\s*\{")
_PASS = re.compile(r"\bPass\s*\{")
_TAGS = re.compile(r"\bTags\s*\{([^}]*)\}")
_RENDER_TYPE = re.compile(r"\"RenderType\"\s*=\s*\"\s*(\w+)\s*\"")
_QUEUE = re.compile(r"\"Queue\"\s*=\s*\"\s*([A-Za-z]+)\s*(?:([+-])\s*(\d+))?\s*\"")
_CULL = re.compile(r"\bCull\s+(Off|Back|Front)\b", re.I)


class ShaderSourceError(ValueError):
    """シェーダーの定義として読めない。"""


@dataclass(frozen=True)
class ShaderDefaults:
    """.shader の最初の SubShader に書かれた描画の既定（#202）。分からないものは None。"""

    queue: int | None = None  # SubShader の Tags の "Queue"。マテリアルの m_CustomRenderQueue が -1 のときに使われる
    render_type: str | None = None  # SubShader の Tags の "RenderType"（Opaque / TransparentCutout / Transparent など）
    cull_off: bool = False  # 最初の Cull が固定の Off（両面）。"Cull [_Cull]" のようにプロパティで決めるものは含めない
    alpha_test: bool = False  # パスが常に切り抜く（#235 / #241）
    hidden: frozenset[str] = frozenset()  # [HideInInspector] で宣言されたプロパティ

    @property
    def alpha_mode(self) -> str | None:
        """タグから見たアルファの扱い。RenderType を優先し（AlphaTest のキューで半透明に重ねるデカールがある）、無ければ Queue。"""
        render_type = (self.render_type or "").lower()
        if render_type == "transparent":
            return "blend"
        if render_type == "transparentcutout" or self.alpha_test:
            return "cutout"
        if render_type or self.queue is None:
            return None
        if self.queue >= 3000:
            return "blend"
        return "cutout" if self.queue >= 2450 else None


def shader_defaults(data: bytes | str, ext: str) -> ShaderDefaults:
    """.shader の描画の既定。.shadergraph（描画の設定はマテリアルのプロパティに出る）や、読めないものは空。"""
    if ext.lower() != ".shader":
        return ShaderDefaults()
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = _COMMENT.sub("", text)
    sub = _SUBSHADER.search(text)
    if sub is None:
        return ShaderDefaults()
    body = text[sub.end():]
    first_pass = _PASS.search(body)
    head = body[:first_pass.start()] if first_pass else body  # Pass の Tags の Queue は Unity が使わない
    queue = render_type = None
    for tags in _TAGS.finditer(head):
        match = _QUEUE.search(tags.group(1))
        if queue is None and match and match.group(1).lower() in _QUEUE_NAMES:
            queue = _QUEUE_NAMES[match.group(1).lower()]
            if match.group(2):
                queue += int(match.group(3)) * (1 if match.group(2) == "+" else -1)
        match = _RENDER_TYPE.search(tags.group(1))
        if render_type is None and match:
            render_type = match.group(1)
    cull = _CULL.search(body)
    end = _SUBSHADER.search(body)  # 最初の SubShader の中だけを見る（2 つ目以降は別のパイプライン・品質向け）
    return ShaderDefaults(
        queue=queue,
        render_type=render_type,
        cull_off=bool(cull and cull.group(1).lower() == "off"),
        alpha_test=bool(_ALWAYS_ALPHA_TEST.search(body[:end.start()] if end else body)),
        hidden=_hidden_properties(text),
    )


def _hidden_properties(text: str) -> frozenset[str]:
    """``[HideInInspector]`` で宣言されたプロパティ名。Properties が読めなければ空。"""
    try:
        block = _properties_block(text)
    except ShaderSourceError:
        return frozenset()
    return frozenset(
        name for attributes, name in _PROPERTY_WITH_ATTRIBUTES.findall(block or "") if "hideininspector" in attributes.lower()
    )


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
    block = _properties_block(_COMMENT.sub("", text))
    return frozenset(_PROPERTY.findall(block)) if block is not None else frozenset()


def _properties_block(text: str) -> str | None:
    """``Properties { ... }`` の中身（コメントは除いてから渡す）。無ければ None。"""
    match = _PROPERTIES.search(text)
    if match is None:
        return None
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
    return text[match.end():i - 1]


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
