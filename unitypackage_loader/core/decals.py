"""URP / HDRP の DecalProjector を、投影の箱の中に置いた板で近似する（Issue #165）。bpy 非依存。

DecalProjector は MonoBehaviour としてシーンに保存されている。スクリプトの GUID ではなく、フィールドの形
（``m_Material`` / ``m_Size`` / ``m_UVScale`` / ``m_UVBias``）で見分ける（URP と HDRP 10 以降は同じ名前で持つ）。

DecalProjector の空間（GameObject のローカル）では、投影の箱は中心が ``m_Offset``、大きさが ``m_Size`` で、ローカルの +Z へ
投影する。箱の中の面の点は、xy を箱の範囲で 0〜1 にした値に ``m_UVScale`` を掛けて ``m_UVBias`` を足した UV でテクスチャを読む。
Blender には投影の仕組みが無いので、箱の中心の高さ（z = ``m_Offset.z``）に xy の範囲の板を置き、投影元（-Z）のほうへ少しだけ
寄せて下の面と重ならないようにする。平らな面（壁・道路）に貼る落書きや染みなら、ほぼ同じ見た目になる。箱が奥行きのある
範囲を覆うもの（でこぼこした地面、角をまたぐもの）や、``_DrawOrder``・法線だけを合成する設定は近似になる。

``m_ScaleMode`` が 0（Scale Invariant）なら、箱の大きさは GameObject の拡大縮小を受けない。1（Inherit From Hierarchy）なら受ける。
"""

from __future__ import annotations

from dataclasses import dataclass

from .primitives import PrimitiveMesh, _Builder
from .unity_yaml import UnityRef, to_float

__all__ = ["Decal", "decal_from_body", "decal_plane", "is_decal_projector"]

_FIELDS = ("m_Material", "m_Size", "m_UVScale", "m_UVBias")
SCALE_INVARIANT = 0
INHERIT_FROM_HIERARCHY = 1
_NUDGE = 0.002  # 投影元へ寄せる距離（m）。箱の奥行きの 1/4 を超えない


@dataclass(frozen=True)
class Decal:
    material: str  # .mat の GUID
    size: tuple[float, float, float]
    offset: tuple[float, float, float]
    uv_scale: tuple[float, float]
    uv_bias: tuple[float, float]
    scale_mode: int


def is_decal_projector(body: dict) -> bool:
    """MonoBehaviour の中身が DecalProjector の形をしているか。"""
    return all(key in body for key in _FIELDS)


def _vector(value: object, keys: str, default: tuple) -> tuple:
    if not isinstance(value, dict):
        return default
    return tuple(to_float(value.get(k), d) for k, d in zip(keys, default))


def decal_from_body(body: dict) -> Decal | None:
    """DecalProjector の中身から ``Decal``。マテリアルが無い（参照が null）か、箱の幅か高さが 0 なら None。"""
    material = body.get("m_Material")
    if not isinstance(material, UnityRef) or material.is_null or not material.guid:
        return None
    size = _vector(body.get("m_Size"), "xyz", (1.0, 1.0, 1.0))
    if abs(size[0]) < 1e-9 or abs(size[1]) < 1e-9:
        return None
    mode = to_float(body.get("m_ScaleMode"), SCALE_INVARIANT)
    return Decal(
        material.guid,
        size,
        _vector(body.get("m_Offset"), "xyz", (0.0, 0.0, 0.0)),
        _vector(body.get("m_UVScale"), "xy", (1.0, 1.0)),
        _vector(body.get("m_UVBias"), "xy", (0.0, 0.0)),
        INHERIT_FROM_HIERARCHY if mode == INHERIT_FROM_HIERARCHY else SCALE_INVARIANT,
    )


def decal_plane(decal: Decal, lossy_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)) -> PrimitiveMesh:
    """デカールの板（GameObject のローカル、Blender の座標）。表は投影元（Unity の -Z）を向く。

    ``lossy_scale`` は GameObject のワールドでの拡大縮小（Unity のローカルの軸ごと）。Scale Invariant のデカールは、
    これで割っておくと、GameObject の行列を掛けたときに箱の大きさが ``m_Size`` のままになる。
    """
    scale = lossy_scale if decal.scale_mode == SCALE_INVARIANT else (1.0, 1.0, 1.0)
    sx, sy, sz = (s / (k if abs(k) > 1e-9 else 1.0) for s, k in zip(decal.size, scale))
    ox, oy, oz = (o / (k if abs(k) > 1e-9 else 1.0) for o, k in zip(decal.offset, scale))
    world_depth = abs(decal.size[2]) if decal.scale_mode == SCALE_INVARIANT else abs(sz * lossy_scale[2])
    nudge = min(_NUDGE, world_depth / 4)
    z = oz - nudge / (abs(lossy_scale[2]) if abs(lossy_scale[2]) > 1e-9 else 1.0)
    x0, x1, y0, y1 = ox - sx / 2, ox + sx / 2, oy - sy / 2, oy + sy / 2
    (su, sv), (bu, bv) = decal.uv_scale, decal.uv_bias
    b = _Builder("Decal")
    corners = [b.vertex(p) for p in ((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z))]
    uvs = [(bu, bv), (bu + su, bv), (bu + su, bv + sv), (bu, bv + sv)]
    b.face(corners, uvs, (0.0, 0.0, -1.0))
    return b.mesh
