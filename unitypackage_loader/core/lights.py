"""シーンのライト・カメラを、Blender のライト・カメラの値に換算する。bpy 非依存。

ライトの強さは推測で決めず、Unity 6 と Blender 5.2 で「白い拡散面をライトの真下から見た画素値（線形）」を
測って合わせた（Issue #49。詳細は DESIGN.md）。

- Unity Built-in（Linear）: 強さはガンマ空間で扱われる。平行光源の画素 = I^2.2、点光源・スポットの画素 =
  I^2.2 / (1 + 25·d²/R²)
- Unity URP: 強さは線形。平行光源の画素 = I、点光源・スポットの画素 = I / d² × (1 − (d/R)⁴)²
- Blender EEVEE: Sun の画素 = strength / π、Point・Spot の画素 = P / (4π²·d²)

Built-in の点光源は逆二乗ではないので、全距離では合わない。d/R が 0.2〜1 の範囲で比の対数の最大を最小にする
「d = 0.3R で一致」を使う（その範囲で 1.4 倍以内）。URP はどの距離でも一致し、Range は Blender の cutoff に入れる。
面光源（Unity ではベイク専用）と HDRP（物理単位）は実測していない近似。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

PIPELINE_BUILTIN = "BUILTIN"
PIPELINE_URP = "URP"
PIPELINE_HDRP = "HDRP"

# Unity の m_Type
LIGHT_SPOT, LIGHT_DIRECTIONAL, LIGHT_POINT, LIGHT_RECTANGLE, LIGHT_DISC = 0, 1, 2, 3, 4
LIGHTMAP_BAKED = 2  # m_Lightmapping: 4 Realtime / 1 Mixed / 2 Baked

BUILTIN_MATCH_RATIO = 0.3  # Built-in の点光源を、Range のこの割合の距離で Unity と一致させる
_GAMMA = 2.2

# Unity のライト・カメラは自分の +Z を向き +Y が上。Blender のライト・カメラは -Z を向き +Y が上。
# Unity の Transform を C·M·C⁻¹ で変換した行列に、右からこの行列を掛けるとオブジェクトの行列になる
LIGHT_CAMERA_BASIS = ((-1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def detect_pipeline(families: Iterable[str]) -> str:
    """パッケージのマテリアルのシェーダーの系統から、ライトの強さの扱いを決める。"""
    found = set(families)
    if "hdrp" in found:
        return PIPELINE_HDRP
    if "urp" in found:
        return PIPELINE_URP
    return PIPELINE_BUILTIN


def _number(value: object, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _srgb_to_linear(c: float) -> float:
    c = min(max(c, 0.0), 1e6)
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _color(value: object) -> tuple[float, float, float]:
    if not isinstance(value, dict):
        return (1.0, 1.0, 1.0)
    return tuple(_srgb_to_linear(_number(value.get(k), 1.0)) for k in "rgb")  # type: ignore[return-value]


@dataclass
class BlenderLight:
    type: str  # SUN / POINT / SPOT / AREA
    color: tuple[float, float, float]  # 線形
    energy: float  # Sun は W/m²、それ以外は W
    use_shadow: bool = True
    shadow_soft_size: float = 0.0
    angle: float = 0.0  # Sun の見かけの大きさ（ラジアン）
    spot_size: float = math.radians(30.0)
    spot_blend: float = 0.15
    use_custom_distance: bool = False
    cutoff_distance: float = 0.0
    shape: str = "RECTANGLE"
    size: float = 1.0
    size_y: float = 1.0
    use_temperature: bool = False
    temperature: float = 6500.0
    baked_only: bool = False  # Unity ではライトマップにだけ効く（リアルタイムでは光らない）
    notes: list[str] = field(default_factory=list)


def convert_light(body: dict, pipeline: str) -> BlenderLight:
    """Light コンポーネントの中身（上書き済み）を Blender のライトの値にする。"""
    light_type = int(_number(body.get("m_Type"), LIGHT_POINT))
    intensity = max(_number(body.get("m_Intensity"), 1.0), 0.0)
    light_range = max(_number(body.get("m_Range"), 10.0), 0.0)
    shadows = body.get("m_Shadows") if isinstance(body.get("m_Shadows"), dict) else {}
    shadow_type = int(_number(shadows.get("m_Type"), 0))
    lightmapping = int(_number(body.get("m_Lightmapping"), 4))
    linear = intensity if pipeline != PIPELINE_BUILTIN else intensity ** _GAMMA

    result = BlenderLight("POINT", _color(body.get("m_Color")), 0.0, use_shadow=shadow_type != 0)
    if _number(body.get("m_UseColorTemperature"), 0) != 0:
        result.use_temperature = True
        result.temperature = min(max(_number(body.get("m_ColorTemperature"), 6570.0), 800.0), 20000.0)
    result.baked_only = lightmapping == LIGHTMAP_BAKED or light_type in (LIGHT_RECTANGLE, LIGHT_DISC)
    # ベイクのソフトシャドウの大きさ（リアルタイムでは Unity のライトは点・平行光源なので、ハードシャドウに近づける）
    result.shadow_soft_size = _number(body.get("m_ShadowRadius"), 0.0) if lightmapping == LIGHTMAP_BAKED else 0.0

    if light_type == LIGHT_DIRECTIONAL:
        result.type = "SUN"
        result.energy = math.pi * linear
        if pipeline == PIPELINE_HDRP:
            result.energy = intensity / 683.0  # lux → W/m²（未計測の近似）
            result.notes.append("HDRP light intensity converted from lux without measurement")
        if lightmapping == LIGHTMAP_BAKED:
            result.angle = math.radians(min(max(_number(body.get("m_ShadowAngle"), 0.0), 0.0), 180.0))
        return result

    if light_type in (LIGHT_RECTANGLE, LIGHT_DISC):
        area = body.get("m_AreaSize") if isinstance(body.get("m_AreaSize"), dict) else {}
        result.type = "AREA"
        result.shape = "RECTANGLE" if light_type == LIGHT_RECTANGLE else "DISK"
        result.size = max(_number(area.get("x"), 1.0), 1e-4)
        result.size_y = max(_number(area.get("y"), 1.0), 1e-4)
        surface = result.size * result.size_y if light_type == LIGHT_RECTANGLE else math.pi * (result.size / 2) ** 2
        # 面のすぐ近くで、白い拡散面の画素が強さ（線形）になるように合わせる（Blender の面光源は近くで 画素 ≈ P / (面積 · π)。実測）
        result.energy = linear * surface * math.pi
        result.notes.append("area light intensity is an unmeasured approximation (Unity bakes area lights)")
        return result

    result.type = "SPOT" if light_type == LIGHT_SPOT else "POINT"
    if pipeline == PIPELINE_BUILTIN:
        d = BUILTIN_MATCH_RATIO * light_range
        result.energy = 4 * math.pi ** 2 * linear * d * d / (1 + 25 * BUILTIN_MATCH_RATIO**2)
    elif pipeline == PIPELINE_URP:
        result.energy = 4 * math.pi ** 2 * linear
    else:
        result.energy = intensity / 683.0 * 4 * math.pi  # lumen → W（未計測の近似）
        result.notes.append("HDRP light intensity converted from lumen without measurement")
    if light_range > 0:
        result.use_custom_distance = True
        result.cutoff_distance = light_range
    if light_type == LIGHT_SPOT:
        outer = min(max(_number(body.get("m_SpotAngle"), 30.0), 1.0), 179.0)
        inner = min(max(_number(body.get("m_InnerSpotAngle"), outer * 0.727), 0.0), outer)
        result.spot_size = math.radians(outer)
        result.spot_blend = min(max(1.0 - inner / outer, 0.0), 1.0)
    return result


@dataclass
class BlenderCamera:
    type: str  # PERSP / ORTHO
    lens: float = 50.0
    sensor_width: float = 36.0
    sensor_height: float = 24.0
    sensor_fit: str = "VERTICAL"
    ortho_scale: float = 10.0
    clip_start: float = 0.3
    clip_end: float = 1000.0
    shift_x: float = 0.0
    shift_y: float = 0.0


# Unity の Gate Fit（m_GateFitMode）→ Blender の sensor_fit。Fill / Overscan / None は近い AUTO にする
_GATE_FIT = {0: "VERTICAL", 1: "HORIZONTAL"}


def convert_camera(body: dict) -> BlenderCamera:
    """Camera コンポーネントの中身（上書き済み）を Blender のカメラの値にする。"""
    near = max(_number(body.get("near clip plane"), 0.3), 1e-4)
    far = max(_number(body.get("far clip plane"), 1000.0), near * 1.001)
    if _number(body.get("orthographic"), 0) != 0:
        size = max(_number(body.get("orthographic size"), 5.0), 1e-4)
        return BlenderCamera("ORTHO", sensor_fit="VERTICAL", ortho_scale=2 * size, clip_start=near, clip_end=far)
    camera = BlenderCamera("PERSP", clip_start=near, clip_end=far)
    if int(_number(body.get("m_projectionMatrixMode"), 1)) == 2:  # Physical Camera
        sensor = body.get("m_SensorSize") if isinstance(body.get("m_SensorSize"), dict) else {}
        shift = body.get("m_LensShift") if isinstance(body.get("m_LensShift"), dict) else {}
        camera.lens = max(_number(body.get("m_FocalLength"), 50.0), 0.1)
        camera.sensor_width = max(_number(sensor.get("x"), 36.0), 0.1)
        camera.sensor_height = max(_number(sensor.get("y"), 24.0), 0.1)
        camera.sensor_fit = _GATE_FIT.get(int(_number(body.get("m_GateFitMode"), 2)), "AUTO")
        camera.shift_x = _number(shift.get("x"), 0.0)
        camera.shift_y = _number(shift.get("y"), 0.0)
        return camera
    # Unity の field of view は縦の画角
    fov = math.radians(min(max(_number(body.get("field of view"), 60.0), 0.1), 179.0))
    camera.sensor_fit = "VERTICAL"
    camera.lens = (camera.sensor_height / 2) / math.tan(fov / 2)
    return camera
