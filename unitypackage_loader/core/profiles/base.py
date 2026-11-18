from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..material import AlphaMode, Lighting, NormalizedMaterial, TexRef, UnityMaterial
from ..shader_source import ShaderDefaults, restrict_to_declared

__all__ = ["ShaderInfo", "ShaderTable", "ShaderProfile", "select_profile", "normalize_material"]

_TABLE_PATH = Path(__file__).with_name("shader_guids.json")

# Unity の UnityEngine.Rendering.BlendMode
BLEND_ZERO = 0
BLEND_ONE = 1
BLEND_SRC_ALPHA = 5
BLEND_ONE_MINUS_SRC_ALPHA = 10

# Unity の UnityEngine.Rendering.CullMode
CULL_OFF = 0
CULL_FRONT = 1
CULL_BACK = 2


@dataclass(frozen=True)
class ShaderInfo:
    family: str
    name: str
    alpha: AlphaMode | None = None
    outline: bool = False
    lighting: Lighting | None = None
    verified: bool = True
    extra: dict[str, Any] | None = None


class ShaderTable:
    """shader_guids.json を読み、マテリアルのシェーダー参照から ShaderInfo を返す。"""

    def __init__(self, path: Path | None = None):
        self.warnings: list[str] = []  # 表を組み立てたときの注意（追加の表が読めなかったなど。インポートの警告に出す）
        data = json.loads((path or _TABLE_PATH).read_text("utf-8"))
        self.builtin_guid: str = data.get("builtin_guid", "0000000000000000f000000000000000")
        self.by_guid: dict[str, ShaderInfo] = {
            g.lower(): self._info(v) for g, v in (data.get("shaders") or {}).items()
        }
        self.by_builtin_id: dict[int, ShaderInfo] = {
            int(k): self._info(v) for k, v in (data.get("builtin_file_ids") or {}).items()
        }

    @staticmethod
    def _info(v: dict[str, Any]) -> ShaderInfo:
        known = {"family", "name", "alpha", "outline", "lighting", "verified"}
        return ShaderInfo(
            family=str(v.get("family", "unknown")),
            name=str(v.get("name", "")),
            alpha=v.get("alpha"),
            outline=bool(v.get("outline", False)),
            lighting=v.get("lighting"),
            verified=bool(v.get("verified", True)),
            extra={k: val for k, val in v.items() if k not in known} or None,
        )

    def add(self, guid: str, info: ShaderInfo) -> None:
        self.by_guid[guid.lower()] = info

    def lookup(self, mat: UnityMaterial) -> ShaderInfo | None:
        if mat.shader is None:
            return None
        guid = mat.shader.guid
        if guid and guid.lower() != self.builtin_guid:
            return self.by_guid.get(guid.lower())
        return self.by_builtin_id.get(mat.shader.file_id)


_default_table: ShaderTable | None = None


def default_table() -> ShaderTable:
    global _default_table
    if _default_table is None:
        _default_table = ShaderTable()
    return _default_table


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------


def alpha_mode_from_blend_state(mat: UnityMaterial, default: AlphaMode = "opaque") -> AlphaMode:
    """ブレンド係数・レンダーキュー・キーワードからアルファモードを推定する。"""
    src = mat.f("_SrcBlend", -1)
    dst = mat.f("_DstBlend", -1)
    if dst == BLEND_ONE_MINUS_SRC_ALPHA or (src == BLEND_SRC_ALPHA and dst >= 0 and dst != BLEND_ZERO):
        return "blend"
    if src == BLEND_ONE and dst == BLEND_ONE:
        return "blend"  # additive
    if mat.render_queue >= 3000:
        return "blend"
    # _BUILTIN_* は Shader Graph の Built-in ターゲットのキーワード（#257）
    if any(k in mat.keywords for k in ("_ALPHABLEND_ON", "_ALPHAPREMULTIPLY_ON", "_SURFACE_TYPE_TRANSPARENT", "_BUILTIN_SURFACE_TYPE_TRANSPARENT")):
        return "blend"
    if 2450 <= mat.render_queue < 3000 or any(k in mat.keywords for k in ("_ALPHATEST_ON", "_BUILTIN_ALPHATEST_ON")) or mat.flag("_AlphaToMask"):
        return "cutout"
    return default


def cull_backface(mat: UnityMaterial, default: bool | None = True) -> bool | None:
    """_Cull / _CullMode / _Culling（VRChat Toon Standard）のいずれかから背面カリングを決める。"""
    for name in ("_Cull", "_CullMode", "_Culling"):
        cull = mat.f(name, -1)
        if cull >= 0:
            return int(cull) == CULL_BACK
    return default


def texture_transform(ref: TexRef | None) -> tuple[tuple[float, float], tuple[float, float]]:
    if ref is None:
        return (1.0, 1.0), (0.0, 0.0)
    return ref.scale, ref.offset


def is_black(color: tuple[float, float, float, float]) -> bool:
    return all(c <= 0.0 for c in color[:3])


# ---------------------------------------------------------------------------
# プロファイル基底
# ---------------------------------------------------------------------------


class ShaderProfile:
    family: str = "unknown"
    lighting: Lighting = "pbr"

    def matches(self, mat: UnityMaterial) -> bool:  # プロパティ指紋による判定
        return False

    def normalize(self, mat: UnityMaterial, info: ShaderInfo | None = None) -> NormalizedMaterial:
        raise NotImplementedError

    # -- サブクラス共通の下ごしらえ --
    def _base(self, mat: UnityMaterial, info: ShaderInfo | None) -> NormalizedMaterial:
        norm = NormalizedMaterial(
            name=mat.name,
            family=info.family if info else self.family,
            shader_name=info.name if info else None,
            lighting=(info.lighting if info and info.lighting else self.lighting),
            source_guid=mat.guid,
            source_path=mat.pathname,
            shader_guid=mat.shader_guid,
        )
        if info is not None and not info.verified:
            norm.warnings.append(f"shader table entry for {info.name!r} is unverified")
        return norm


_profiles: tuple[tuple[ShaderProfile, ...], ShaderProfile] | None = None


def _profile_list() -> tuple[tuple[ShaderProfile, ...], ShaderProfile]:
    """(指紋判定の順に並べたプロファイル, generic)。プロファイルは状態を持たないので 1 回だけ作る。"""
    global _profiles
    if _profiles is None:
        # 各プロファイルのモジュールがこのモジュールを import するので、ここで初めて import する
        from . import hdrp, liltoon, mtoon, poiyomi, standard, urp, vrchat_mobile

        # 指紋判定の順序: VRChat Mobile は Standard の残骸プロパティを持つことが多いので Standard より先に置く。
        # URP Lit / HDRP/Lit も、Standard から変換したときの _MainTex / _Glossiness などを持つので Standard より先に置く（#116 / #119）
        ordered = (
            liltoon.LilToonProfile(),
            mtoon.MToonProfile(),
            poiyomi.PoiyomiProfile(),
            vrchat_mobile.VRChatMobileProfile(),
            urp.UrpLitProfile(),
            hdrp.HdrpLitProfile(),
            standard.StandardProfile(),
        )
        _profiles = (ordered, standard.GenericProfile())
    return _profiles


def select_profile(mat: UnityMaterial, table: ShaderTable | None = None) -> tuple[ShaderProfile, ShaderInfo | None]:
    """GUID 表 → プロパティ指紋 → generic の順にプロファイルを決める。"""
    table = table or default_table()
    info = table.lookup(mat)
    profiles, generic = _profile_list()
    if info is not None:
        for profile in profiles:
            if profile.family == info.family or info.family in getattr(profile, "aliases", ()):
                return profile, info
    for profile in profiles:
        if profile.matches(mat):
            return profile, info
    return generic, info


def normalize_material(
    mat: UnityMaterial,
    table: ShaderTable | None = None,
    declared: frozenset[str] | None = None,
    defaults: ShaderDefaults | None = None,
) -> NormalizedMaterial:
    """``declared`` はパッケージに入っているシェーダーの定義が宣言するプロパティ名、``defaults`` はその描画の既定（``core/shader_source.py``）。

    シェーダー表に無いシェーダーで宣言が分かるときは、宣言されていない（前のシェーダーの残りの）プロパティを読まない（#118）。
    外した名前は ``extras["undeclared_properties"]`` に残す。表で分かるシェーダーは各プロファイルの読み方に任せる。
    同じく表に無いシェーダーで、マテリアルからは不透明としか読めず、m_CustomRenderQueue も -1（シェーダーの既定のまま）なら、
    シェーダーの RenderType / Queue タグから切り抜き・半透明を決める。マテリアルにカリングのプロパティが無く、
    シェーダーが固定の ``Cull Off`` なら両面にする（#202）。表に無いシェーダーを定義と名前から補う処理と、その順番は
    ``custom_shader.py`` にまとめてある。
    """
    table = table or default_table()
    dropped: list[str] = []
    custom = table.lookup(mat) is None
    if declared and custom:
        mat, dropped = restrict_to_declared(mat, declared)
    profile, info = select_profile(mat, table)
    norm = profile.normalize(mat, info)
    # 表に無いシェーダー（自作の .shader / Shader Graph / Amplify など）を、シェーダーの定義と名前から補う（custom_shader.py）
    from .custom_shader import apply_shader_defaults, fill_from_custom_names
    from .standard import GenericProfile

    if defaults is not None and custom:
        apply_shader_defaults(mat, norm, defaults)
    if info is None:
        fill_from_custom_names(
            mat, norm,
            display_names=defaults.display_names if defaults is not None else None,
            declared_known=bool(declared and custom),
            generic=isinstance(profile, GenericProfile),
        )
    if dropped:
        norm.extras["undeclared_properties"] = dropped
    return norm
