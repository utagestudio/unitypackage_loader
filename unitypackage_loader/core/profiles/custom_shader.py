"""表に無いシェーダー（自作の .shader / Shader Graph / Amplify Shader Editor など）を、シェーダーの定義と名前から補う。

``base.normalize_material`` がプロファイルで読んだ後に、次の順で当てる（#262 でここにまとめた。以前は standard.py と
base.py に分かれていた）。

1. ``apply_shader_defaults``: シェーダーの定義に書かれた描画の既定（タグ・固定の Cull・常に切り抜くパス・隠した
   ``_EmissionColor``・Shader Graph のターゲットの設定。``shader_source.ShaderDefaults``。#202 / #235 / #257）
2. ``fill_from_custom_names``: 一般名で見つからなかったテクスチャ・色・発光を、自作シェーダーでよく使われる名前で探す
   （別名 #118 / #163、表示名 #236、1 枚だけの画像 #239、上下 2 色 #257、別画像のアルファ #266、発光 #167 / #241）

generic プロファイル（``standard.GenericProfile``）も、別名のテクスチャと発光は自分で探す。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..material import NormalizedMaterial, UnityMaterial
from .base import cull_backface, is_black, texture_transform


def apply_shader_defaults(mat: UnityMaterial, norm: NormalizedMaterial, defaults) -> None:
    """シェーダーの定義に書かれた描画の既定を当てる（表に無いシェーダーだけ。``defaults`` は ``ShaderDefaults``）。

    マテリアルからは不透明としか読めず、m_CustomRenderQueue も -1（シェーダーの既定のまま）なら、シェーダーの
    RenderType / Queue タグ（Shader Graph はターゲットの設定）から切り抜き・半透明を決める。マテリアルにカリングの
    プロパティが無く、シェーダーが固定の ``Cull Off``（Shader Graph は Render Face が Both）なら両面にする（#202 / #257）。
    """
    if norm.alpha_mode == "opaque" and mat.render_queue < 0 and defaults.alpha_mode is not None:
        norm.alpha_mode = defaults.alpha_mode
        norm.alpha_from_texture = True
        if "_Cutoff" not in mat.floats and "_AlphaCutoff" in mat.floats:
            # Amplify Shader Editor のテンプレートは _AlphaCutoff をしきい値にする（#235）
            norm.alpha_cutoff = mat.f("_AlphaCutoff", norm.alpha_cutoff)
    if defaults.cull_off and cull_backface(mat, default=None) is None:
        norm.cull_backface = False
    if "_EmissionColor" in defaults.hidden and norm.emission_tex is None:
        # Amplify の URP テンプレートは、使わなくても白い _EmissionColor を隠して宣言する。発光として読まない（#235）
        norm.emission_color = (0.0, 0.0, 0.0, 1.0)


def fill_from_custom_names(
    mat: UnityMaterial,
    norm: NormalizedMaterial,
    display_names: Mapping[str, str] | None,
    declared_known: bool,
    generic: bool,
) -> None:
    """一般名で見つからなかったものを、自作シェーダーの名前で探す（シェーダー表に無いものだけ）。

    ``declared_known`` は、宣言が分かって前のシェーダーの残りを外したか（残りがあると 1 枚・1 組に決められない）。
    ``generic`` は generic プロファイルで読んだか（別名のテクスチャと発光は済ませている）。
    """
    fill_textures_from_aliases(mat, norm, display_names)
    fill_alpha_from_aliases(mat, norm, display_names)
    if declared_known:
        fill_sole_texture(mat, norm, display_names)
        fill_gradient_color(mat, norm)
    # トゥーン系は発光の項目を各プロファイルが読む（名前の似た別の役割の項目を拾わない）。generic は自分で済ませている
    if norm.lighting != "toon" and not generic:
        if norm.has_emission:
            scale_emission_by_alias(mat, norm)
        else:
            fill_emission_from_aliases(mat, norm)


# 一般名（_MainTex など）を持たない自作シェーダー（Amplify Shader Editor など）でよく使われる名前。
# 大文字小文字と "_" を無視して完全一致で比べる（_Mask や _Color_Tilling のような別の役割の名前を拾わないよう、部分一致にはしない）。
# #118 で宣言されていない残りの _MainTex を読まなくなり、独自の名前でしか色を持たないシェーダーが白くなった
_BASE_COLOR_ALIASES = (
    "basecolor", "basecolormap", "basecolortex", "basecolortexture", "basemap", "basetex", "basetexture",
    "albedo", "albedomap", "albedotex", "albedotexture", "diffuse", "diffusemap", "diffusetex", "diffusetexture",
    "maintexture", "colormap", "colortex", "colortexture",
)
_NORMAL_ALIASES = ("normal", "normals", "normalmap", "normaltex", "normaltexture", "bump", "bumptex", "bumptexture")
_NORMAL_SCALE_ALIASES = (
    "normalscale", "scalenormal", "normalstrength", "normalintensity", "normalpower", "normalamount", "bumpstrength", "bumppower",
)

# 発光（#167）。ON / OFF・色・テクスチャ・強さを独自の名前で持つシェーダー（Synty の _Enable_Emission / _Emission_Color /
# _Emission_Map など）向け。Unity 標準の名前は各プロファイルが読み、条件付きで無視もする（#52 のキーワードの判定など）ので、
# ここでは拾わない（_EMISSION_STANDARD_NAMES）
_EMISSION_TOGGLE_ALIASES = ("enableemission", "useemission", "emissionenabled", "emissionenable", "emissionon", "emissiontoggle")
_EMISSION_COLOR_ALIASES = ("emissioncolor", "emissioncolour", "emissivecolor", "emissivecolour", "emissiontint")
_EMISSION_TEX_ALIASES = (
    "emissionmap", "emissiontex", "emissiontexture", "emissivemap", "emissivetex", "emissivetexture", "emission", "emissive",
)
_EMISSION_STRENGTH_ALIASES = (
    "emissionintensity", "emissionstrength", "emissionpower", "emissiveintensity", "emissivestrength",
    "emissionmultiplier", "emissionmultiplayer",  # Corals の Amplify シェーダーは綴り違いの _EmissionMultiplayer（#241）
)
_EMISSION_STANDARD_NAMES = frozenset({"_EmissionColor", "_EmissionMap", "_EmissiveColor", "_EmissiveColorMap", "_EmissiveColorLDR"})


def _alias_key(name: str) -> str:
    """大文字小文字と "_"、空白（Shader Graph の表示名の「Base Map」。#236）を無視する。"""
    return name.replace("_", "").replace(" ", "").lower()


def _find_texture(mat: UnityMaterial, aliases: tuple[str, ...], display_names: Mapping[str, str] | None = None):
    """``aliases`` の順に、値の入ったテクスチャを探す。名前で見つからなければ、表示名（Shader Graph。#236）で探す。"""
    for key_of in (lambda name: name, lambda name: (display_names or {}).get(name, "")):
        by_key = {_alias_key(key_of(name)): ref for name, ref in mat.textures.items() if key_of(name)}
        for alias in aliases:
            if by_key.get(alias) is not None:
                return by_key[alias]
        if not display_names:
            break
    return None


def _find_color(mat: UnityMaterial, aliases: tuple[str, ...], exclude: frozenset[str] = frozenset()):
    """``aliases`` の順に色を探す。無ければ None。"""
    by_key = {_alias_key(name): value for name, value in mat.colors.items() if name not in exclude}
    for alias in aliases:
        if alias in by_key:
            return by_key[alias]
    return None


def _find_float(mat: UnityMaterial, aliases: tuple[str, ...], default: float, display_names: Mapping[str, str] | None = None) -> float:
    names = {_alias_key(name): name for name in (*mat.floats, *mat.ints)}
    for name in (*mat.floats, *mat.ints):  # 名前で見つからなければ表示名で（#236）
        display = (display_names or {}).get(name)
        if display:
            names.setdefault(_alias_key(display), name)
    for alias in aliases:
        if alias in names:
            return mat.f(names[alias], default)
    return default


def fill_textures_from_aliases(mat: UnityMaterial, n: NormalizedMaterial, display_names: Mapping[str, str] | None = None) -> None:
    """ベースカラー・法線のテクスチャが一般名で見つからなかったとき、自作シェーダーでよく使われる名前で探す。

    Shader Graph で作ったシェーダーは URP Lit の ``_WorkflowMode`` / ``_Surface`` を持つので指紋では URP Lit と読まれるが、
    テクスチャは ``_Albedo_Map`` / ``_Base_Map`` / ``_Normal_Map`` のような独自の名前で持つことがある（Synty の POLYGON シリーズ）。
    ``display_names`` は Shader Graph の参照名 → 表示名。参照名が自動の ``Texture2D_E1B0D043`` のようなもので見つからなければ、
    表示名（「Base Map」）で探す（Terrain Asset Pack の草。#236）。
    """
    if n.base_color_tex is None:
        n.base_color_tex = _find_texture(mat, _BASE_COLOR_ALIASES, display_names)
        if n.base_color_tex is not None:
            n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)
    if n.normal_tex is None:
        n.normal_tex = _find_texture(mat, _NORMAL_ALIASES, display_names)
        if n.normal_tex is not None:
            n.normal_strength = _find_float(mat, _NORMAL_SCALE_ALIASES, 1.0, display_names)


# 役割の分かるテクスチャの名前に含まれる語（大文字小文字・"_"・空白を無視した部分一致）。fill_sole_texture でベースカラーの候補から外す
_NON_COLOR_WORDS = (
    "ramp", "normal", "bump", "mask", "matcap", "noise", "height", "parallax", "displace", "rough", "metal", "gloss", "smooth",
    "spec", "occlusion", "ao", "emiss", "emission", "detail", "cube", "reflect", "lut", "gradient", "dissolve", "flow",
    "shadow", "outline", "rim", "thickness", "trans", "light", "splat", "control", "dirt", "wind",
    # 手元の 71 パッケージで、色ではない画像に当たったもの（水の効果、色見本、Texture2DArray、室内の擬似表現、アルファ）
    "caustic", "foam", "water", "overlay", "theme", "palette", "array", "room", "alpha",
)
_BUILTIN_GUID_PREFIX = "0000000000000000"  # Unity の組み込みのリソース（既定のテクスチャなど）


def fill_sole_texture(mat: UnityMaterial, n: NormalizedMaterial, display_names: Mapping[str, str] | None = None) -> None:
    """名前でベースカラーが見つからず、役割の分からない名前のテクスチャが 1 枚だけなら、それをベースカラーにする（#239）。

    宣言が分かる自作シェーダー（前のシェーダーの残りを外したもの）だけに使う。Toon Harbor Pack の TH_CustomToon は
    色を ``_TextureSample1``（表示名も「Texture Sample」）、陰の段階を ``_TextureRamp`` に持ち、名前では色が分からない。
    2 枚以上残るときは決められないので当てない。
    """
    if n.base_color_tex is not None:
        return
    candidates = []
    for name, ref in mat.textures.items():
        if ref is None or not ref.guid or ref.guid.startswith(_BUILTIN_GUID_PREFIX):
            continue
        key = _alias_key(f"{name} {(display_names or {}).get(name, '')}")
        if not any(word in key for word in _NON_COLOR_WORDS):
            candidates.append(ref)
    if len(candidates) == 1:
        n.base_color_tex = candidates[0]
        n.uv_scale, n.uv_offset = texture_transform(n.base_color_tex)


# アルファを色の画像とは別の画像で持つ自作シェーダー（Shibuya の M_BaseAlpha は _AlphaMask の R を Alpha に、_AlphaThreshold を
# Alpha Clip Threshold につなぐ。#266）。完全一致で比べる
_ALPHA_TEX_ALIASES = (
    "alphamask", "alphamap", "alphatex", "alphatexture", "opacitymask", "opacitymap", "opacitytex", "opacitytexture", "opacity",
    "cutoutmask", "cutoutmap", "transparencymask", "transparencymap",
)
_ALPHA_THRESHOLD_ALIASES = (
    "alphathreshold", "alphaclipthreshold", "alphaclippingthreshold", "alphacutoffthreshold", "cutoffthreshold",
    "clipthreshold", "opacitythreshold", "opacitycutoff",
)


def fill_alpha_from_aliases(mat: UnityMaterial, n: NormalizedMaterial, display_names: Mapping[str, str] | None = None) -> None:
    """切り抜き・半透明のマテリアルで、アルファの画像としきい値を独自の名前で探す（#266）。

    アルファの画像は R を使う（白黒のマスク）。しきい値は、一般名の ``_Cutoff`` が無いときだけ使う。
    """
    if n.alpha_mode == "opaque":
        return
    if n.alpha_tex is None:
        tex = _find_texture(mat, _ALPHA_TEX_ALIASES, display_names)
        if tex is not None and tex.guid and not tex.guid.startswith(_BUILTIN_GUID_PREFIX):
            n.alpha_tex = tex
            n.alpha_from_texture = True
    if n.alpha_mode == "cutout" and "_Cutoff" not in mat.floats:
        n.alpha_cutoff = _find_float(mat, _ALPHA_THRESHOLD_ALIASES, n.alpha_cutoff, display_names)


_GRADIENT_TOP_WORDS = ("top", "upper")
_GRADIENT_BOTTOM_WORDS = ("bottom", "lower", "base")
_COLOR_WORDS = ("color", "colour", "tint")


def fill_gradient_color(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """色が白のままで、上下 2 色のグラデーション（``_Color_Top`` と ``_Color_Base`` など）があれば、その平均を色にする（#257）。

    様式化した植物のシェーダーは、白黒の葉の画像に高さで上下の色を混ぜて塗る（Dreamscape Nature Meadows の Foliage /
    Leaves の Shader Graph は ``_Color_Top`` / ``_Color_Base``、``_Foliage_Color_Top`` / ``_Foliage_Color_Bottom``）。
    以前は色の項目を一般名でしか探さず、葉が真っ白になっていた。高さで混ぜることは再現できないので平均で近づける。
    上と下の組が 1 つに決まるときだけ当てる。
    """
    if any(abs(c - 1.0) > 1e-6 for c in n.base_color[:3]):
        return
    tops: list[str] = []
    bottoms: list[str] = []
    for name in mat.colors:
        key = _alias_key(name)
        if not any(word in key for word in _COLOR_WORDS) or name in _EMISSION_STANDARD_NAMES:
            continue
        if any(word in key for word in _GRADIENT_TOP_WORDS):
            tops.append(name)
        elif any(word in key for word in _GRADIENT_BOTTOM_WORDS) and key != "basecolor":  # _BaseColor は URP の色
            bottoms.append(name)
    if len(tops) != 1 or len(bottoms) != 1:
        return
    top, bottom = mat.colors[tops[0]], mat.colors[bottoms[0]]
    n.base_color = tuple((a + b) / 2 for a, b in zip(top[:3], bottom[:3])) + (n.base_color[3],)
    n.extras["gradient_color"] = [tops[0], bottoms[0]]


def scale_emission_by_alias(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """一般名（``_EmissionColor``）で読んだ発光に、独自の名前の強さを掛ける（自作シェーダー向け。#241）。

    Corals の Amplify シェーダーは ``_EmissionColor × _EmissionMultiplayer × 画像`` で、強さが 0 なら光らない。
    """
    if n.has_emission:
        n.emission_strength *= max(0.0, _find_float(mat, _EMISSION_STRENGTH_ALIASES, 1.0))


def fill_emission_from_aliases(mat: UnityMaterial, n: NormalizedMaterial) -> None:
    """発光が一般名で見つからなかったとき、独自の名前（ON / OFF・色・テクスチャ・強さ）で探す（#167）。

    発光は「色 × テクスチャ」とする（Synty の Shader Graph は ON のとき Emission_Map × Emission_Color）。誤って光らせると
    目立つので、ON / OFF の項目が OFF なら光らせず、色が黒なら（テクスチャがあっても）光らせない。色の項目が無ければ、
    テクスチャがあるときだけ白とする。
    """
    if n.has_emission:
        return
    names = {_alias_key(name): name for name in (*mat.floats, *mat.ints)}
    toggle = next((names[a] for a in _EMISSION_TOGGLE_ALIASES if a in names), None)
    if toggle is not None and not mat.flag(toggle):
        return
    by_key = {_alias_key(name): ref for name, ref in mat.textures.items() if name not in _EMISSION_STANDARD_NAMES}
    tex = next((by_key[a] for a in _EMISSION_TEX_ALIASES if by_key.get(a) is not None), None)
    color = _find_color(mat, _EMISSION_COLOR_ALIASES, _EMISSION_STANDARD_NAMES)
    if color is None:
        if tex is None:
            return
        color = (1.0, 1.0, 1.0, 1.0)
    if is_black(color):
        return
    n.emission_tex = tex
    n.emission_color = color
    n.emission_strength *= max(0.0, _find_float(mat, _EMISSION_STRENGTH_ALIASES, 1.0))
