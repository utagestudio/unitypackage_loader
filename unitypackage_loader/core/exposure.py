"""HDRP の Volume の露出（Exposure）から、Blender のシーンの露出（Color Management の Exposure）を決める。bpy 非依存（#130）。

HDRP の表示は 画素 = 輝度（nits）× 1 / (1.2 × 2^EV100)（core の ``PhysicalCamera.hlsl`` の ``ConvertEV100ToExposure``）。
アドオンは HDRP のライトを 683 lm/W でワットにしている（``core/lights.py``）ので、Blender の画素 = 輝度 ÷ 683。
同じ見た目にするには、Blender の露出（段）= log2(683 / 1.2) − EV100。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from .unity_yaml import UnityDocument, to_float

__all__ = ["VOLUME_SCRIPT_GUID", "EXPOSURE_SCRIPT_GUID", "GlobalVolume", "SceneExposure", "global_volumes",
           "exposure_settings", "scene_exposure", "blender_exposure"]

# Unity 6000.6 に同梱の core / HDRP 17.6 の .cs.meta
VOLUME_SCRIPT_GUID = "172515602e62fb746b5d573b38a5fe58"  # UnityEngine.Rendering.Volume
EXPOSURE_SCRIPT_GUID = "2d08ce26990eb1a4a9177b860541e702"  # UnityEngine.Rendering.HighDefinition.Exposure

# Exposure.mode（ExposureMode）
MODE_FIXED, MODE_AUTOMATIC, MODE_CURVE_MAPPING, MODE_PHYSICAL_CAMERA, MODE_AUTOMATIC_HISTOGRAM = 0, 1, 2, 3, 4
_MODE_NAMES = {0: "fixed", 1: "automatic", 2: "curve mapping", 3: "physical camera", 4: "automatic histogram"}

BLENDER_EXPOSURE_AT_EV0 = math.log2(683.0 / 1.2)  # ≈ 9.15
_EXPOSURE_LIMIT = 32.0  # Blender の view_settings.exposure の範囲（-32〜32）


@dataclass
class GlobalVolume:
    priority: float
    weight: float
    profile_guid: str


@dataclass
class SceneExposure:
    ev100: float
    exposure: float  # Blender の view_settings.exposure（段）
    mode: str
    approximate: bool  # 自動露出を上限で近似したなど


def _script_guid(body: dict) -> str | None:
    script = body.get("m_Script")
    guid = getattr(script, "guid", None)
    return guid.lower() if isinstance(guid, str) else None


def global_volumes(documents: Iterable[UnityDocument]) -> list[GlobalVolume]:
    """シーンのグローバルな Volume（有効・重み > 0・プロファイルあり）を、``priority`` の高い順に。

    ローカルな Volume（コライダーの中だけで効く）は、カメラの位置が決まらないので使わない。
    """
    result = []
    for doc in documents:
        if doc.class_id != 114 or _script_guid(doc.body) != VOLUME_SCRIPT_GUID:
            continue
        body = doc.body
        profile = getattr(body.get("sharedProfile"), "guid", None)
        if not profile or to_float(body.get("m_Enabled"), 1.0) == 0 or to_float(body.get("m_IsGlobal"), 1.0) == 0:
            continue
        weight = to_float(body.get("weight"), 1.0)
        if weight <= 0:
            continue
        result.append(GlobalVolume(to_float(body.get("priority"), 0.0), weight, str(profile).lower()))
    return sorted(result, key=lambda v: -v.priority)


def exposure_settings(documents: Iterable[UnityDocument]) -> dict[str, float] | None:
    """Volume のプロファイルにある Exposure の、上書きされている値（``m_OverrideState: 1``）。無効・無しなら None。"""
    for doc in documents:
        if doc.class_id != 114 or _script_guid(doc.body) != EXPOSURE_SCRIPT_GUID:
            continue
        if to_float(doc.body.get("active"), 1.0) == 0:
            return None
        values = {}
        for key in ("mode", "fixedExposure", "compensation", "limitMin", "limitMax"):
            param = doc.body.get(key)
            if isinstance(param, dict) and to_float(param.get("m_OverrideState"), 0.0) != 0:
                value = to_float(param.get("m_Value"), math.nan)
                if math.isfinite(value):
                    values[key] = value
        return values
    return None


def scene_exposure(settings: dict[str, float], camera: dict | None = None) -> SceneExposure | None:
    """Exposure の値から EV100 と Blender の露出を決める。モードが上書きされていなければ None（HDRP の既定のプロファイル次第で分からない）。

    - Fixed: ``fixedExposure``
    - Use Physical Camera: カメラの絞り・シャッター・ISO から EV100 = log2(N² / t) − log2(ISO / 100)
    - Automatic / Automatic Histogram / Curve Mapping: 画面の明るさで決まるので、``limitMax`` で近似する
      （室内の照明や日中の外光では、自動露出はふつう上限で止まる）
    - どれも ``compensation`` を引く（HDRP は補正を EV から引く）
    """
    if "mode" not in settings:
        return None
    mode = int(settings["mode"])
    approximate = False
    if mode == MODE_FIXED:
        ev = settings.get("fixedExposure", 0.0)
    elif mode == MODE_PHYSICAL_CAMERA:
        ev = _camera_ev100(camera)
        if ev is None:
            return None
    else:
        ev = settings.get("limitMax", 14.0)  # HDRP の Exposure の既定の上限
        approximate = True
    ev -= settings.get("compensation", 0.0)
    exposure = max(-_EXPOSURE_LIMIT, min(_EXPOSURE_LIMIT, BLENDER_EXPOSURE_AT_EV0 - ev))
    return SceneExposure(ev, exposure, _MODE_NAMES.get(mode, str(mode)), approximate)


def blender_exposure(ev100: float) -> float:
    return BLENDER_EXPOSURE_AT_EV0 - ev100


def _camera_ev100(camera: dict | None) -> float | None:
    """Camera の物理カメラの値（m_Aperture / m_ShutterSpeed / m_Iso）からの EV100。"""
    if not isinstance(camera, dict):
        return None
    aperture = to_float(camera.get("m_Aperture"), math.nan)
    shutter = to_float(camera.get("m_ShutterSpeed"), math.nan)
    iso = to_float(camera.get("m_Iso"), math.nan)
    if not all(math.isfinite(v) and v > 0 for v in (aperture, shutter, iso)):
        return None
    return math.log2(aperture * aperture / shutter) - math.log2(iso / 100.0)
