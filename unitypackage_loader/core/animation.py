"""AnimationClip（.anim）を読む。bpy 非依存（#209）。

Generic なクリップのうち、次のカーブを ``Curve`` にそろえて返す。

- シェイプキー: ``SkinnedMeshRenderer`` の ``blendShape.<名前>``（値は 0〜100）
- 表示の切り替え: GameObject の ``m_IsActive``、Renderer の ``m_Enabled``
- Transform: ``m_PositionCurves`` / ``m_RotationCurves``（クォータニオン）/ ``m_EulerCurves``（度）/ ``m_ScaleCurves``。
  3〜4 成分のカーブは成分ごとの ``Curve`` に分ける（``attribute`` は ``m_LocalPosition.x`` などエディターのカーブと同じ名前）

Humanoid のクリップ（Animator のマッスル値のカーブ）は ``humanoid`` を立て、マッスル値のカーブは返さない
（ほかのカーブは返すが、読み込む側はクリップごと外す）。
それ以外のカーブ（マテリアルのプロパティ、オブジェクト参照の差し替えなど）は数だけ数える。

カーブの評価は Unity の AnimationCurve と同じく、キーの間をエルミート（weighted のキーは 3 次ベジェ）でつなぎ、
接線が無限大の区間は前のキーの値のまま（階段）にする。

Animator のステートで使う前提の「状態を切り替えるだけ」のクリップ（値が一定か、0.1 秒以下で終わるもの。
VRChat のアバターの衣装の切り替えや表情、車両のハンドルの角度など）は、Blender で再生しても意味が無いので
``is_state_clip`` で見分けて読み込まない。

解析は Unity YAML でもバイナリ（Force Binary）でも ``unity_binary.load_documents`` で ``UnityDocument`` にしてから行う。
壊れた入力には ``AnimationParseError``（``ValueError``）だけを送出する。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .unity_binary import load_documents

__all__ = [
    "AnimationParseError",
    "Key",
    "Curve",
    "AnimationClip",
    "KIND_SHAPE_KEY",
    "KIND_ACTIVE",
    "KIND_ENABLED",
    "KIND_POSITION",
    "KIND_ROTATION",
    "KIND_EULER",
    "KIND_SCALE",
    "TRANSFORM_KINDS",
    "parse_clip",
    "is_state_clip",
    "evaluate",
    "bezier_handles",
    "euler_to_quaternion",
    "quaternion_to_euler",
    "sample_times",
    "transform_spec",
]

CLASS_ANIMATION_CLIP = 74
CLASS_GAME_OBJECT = 1
CLASS_TRANSFORM = 4
CLASS_ANIMATOR = 95
CLASS_SKINNED_MESH_RENDERER = 137
# m_Enabled を表示の切り替えとして扱う Renderer（MeshRenderer / SkinnedMeshRenderer）
RENDERER_CLASSES = frozenset({23, 137})

KIND_SHAPE_KEY = "shape_key"
KIND_ACTIVE = "active"
KIND_ENABLED = "enabled"
KIND_POSITION = "position"
KIND_ROTATION = "rotation"
KIND_EULER = "euler"
KIND_SCALE = "scale"
TRANSFORM_KINDS = frozenset({KIND_POSITION, KIND_ROTATION, KIND_EULER, KIND_SCALE})

# 1 つのクリップで読むカーブ・キーの数の上限（壊れた・細工したファイルで止まらないように）
MAX_CURVES = 20000
MAX_KEYS = 200000
# クリップの長さの上限（秒）。これを超えるキーは無いものとして扱う
MAX_TIME = 86400.0

# Animator（classID 95）のカーブのうち、Humanoid のマッスル値・ルートの動きとみなす属性
_HUMANOID_RE = re.compile(
    r"^(Root|Motion|LeftFoot|RightFoot|LeftHand|RightHand)[TQ]\.[xyzw]$"
    r"|^(Spine|Chest|UpperChest|Neck|Head|Jaw|Left|Right) "
    r"|^(LeftHand|RightHand)\.(Thumb|Index|Middle|Ring|Little)\."
)

_VECTOR_FIELDS = {
    "m_PositionCurves": (KIND_POSITION, "m_LocalPosition", "xyz"),
    "m_EulerCurves": (KIND_EULER, "localEulerAnglesRaw", "xyz"),
    "m_ScaleCurves": (KIND_SCALE, "m_LocalScale", "xyz"),
    "m_RotationCurves": (KIND_ROTATION, "m_LocalRotation", "xyzw"),
}

# エディターのカーブ（m_EditorCurves）の Transform の属性 → 種類
_EDITOR_TRANSFORM = {
    "m_LocalPosition": KIND_POSITION,
    "m_LocalScale": KIND_SCALE,
    "m_LocalRotation": KIND_ROTATION,
    "localEulerAnglesRaw": KIND_EULER,
    "localEulerAngles": KIND_EULER,
    "localEulerAnglesBaked": KIND_EULER,
}

# Unity の RotationOrder（m_RotationOrder）。文字は「先に回す軸」から並べる（4 = ZXY が既定）
ROTATION_ORDERS = {0: "XYZ", 1: "XZY", 2: "YZX", 3: "YXZ", 4: "ZXY", 5: "ZYX"}
DEFAULT_ROTATION_ORDER = 4


class AnimationParseError(ValueError):
    """.anim として読めない（ValueError の派生）。"""


@dataclass(frozen=True)
class Key:
    """AnimationCurve の 1 キー。接線は値/秒（無限大は階段）。重みは weighted のときだけ使う。"""

    time: float
    value: float
    in_slope: float = 0.0
    out_slope: float = 0.0
    in_weight: float = 1.0 / 3.0
    out_weight: float = 1.0 / 3.0
    weighted_mode: int = 0  # 0 = None, 1 = In, 2 = Out, 3 = Both

    @property
    def weighted_in(self) -> bool:
        return bool(self.weighted_mode & 1)

    @property
    def weighted_out(self) -> bool:
        return bool(self.weighted_mode & 2)


@dataclass
class Curve:
    """1 つの値のカーブ。``path`` はアニメーションのルートからの階層パス（ルート自身は空文字）。"""

    path: str
    attribute: str
    class_id: int
    kind: str
    keys: list[Key]
    component: int = 0  # Transform のカーブの成分（x, y, z, w の順の添字）
    rotation_order: int = DEFAULT_ROTATION_ORDER  # オイラー回転のカーブだけが使う

    @property
    def shape_key(self) -> str | None:
        """シェイプキーのカーブなら、その名前。"""
        return self.attribute[len("blendShape."):] if self.kind == KIND_SHAPE_KEY else None


@dataclass
class AnimationClip:
    name: str
    sample_rate: float = 60.0
    loop: bool = False
    legacy: bool = False
    humanoid: bool = False
    curves: list[Curve] = field(default_factory=list)
    other_curves: int = 0  # 読まない種類のカーブ（マテリアルのプロパティ、Animator のパラメーターなど）
    pptr_curves: int = 0  # オブジェクト参照を差し替えるカーブ（マテリアル・スプライトの差し替え）
    start: float = 0.0
    stop: float = 0.0

    @property
    def paths(self) -> set[str]:
        return {c.path for c in self.curves}


# ---------------------------------------------------------------------------
# 値の取り出し
# ---------------------------------------------------------------------------


def _number(value: Any, default: float = 0.0, allow_infinite: bool = False) -> float:
    """数値にする。Unity YAML の ``Infinity`` / ``-Infinity`` は ``allow_infinite`` のときだけ無限大にする。"""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return default
    else:
        return default
    if math.isnan(number):
        return default
    if math.isinf(number) and not allow_infinite:
        return default
    return number


def _int(value: Any, default: int = 0) -> int:
    number = _number(value, float(default))
    try:
        return int(number)
    except (OverflowError, ValueError):
        return default


def _component(value: Any, index: int, default: float, allow_infinite: bool = False) -> float:
    """``{x: .., y: .., z: .., w: ..}`` の index 番目の成分。"""
    if isinstance(value, Mapping):
        return _number(value.get("xyzw"[index]), default, allow_infinite)
    return default


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _keys(raw_curve: Any, component: int | None) -> list[Key]:
    """``curve`` の ``m_Curve`` をキーの列にする（``component`` はベクトルのカーブの成分。None ならスカラー）。"""
    if not isinstance(raw_curve, Mapping):
        return []
    keys: list[Key] = []
    for raw in _list(raw_curve.get("m_Curve")):
        if not isinstance(raw, Mapping):
            continue
        time = _number(raw.get("time"), math.nan)
        if not -MAX_TIME <= time <= MAX_TIME:
            continue
        if component is None:
            value = _number(raw.get("value"), 0.0)
            in_slope = _number(raw.get("inSlope"), 0.0, True)
            out_slope = _number(raw.get("outSlope"), 0.0, True)
            in_weight = _number(raw.get("inWeight"), 1.0 / 3.0)
            out_weight = _number(raw.get("outWeight"), 1.0 / 3.0)
        else:
            value = _component(raw.get("value"), component, 0.0)
            in_slope = _component(raw.get("inSlope"), component, 0.0, True)
            out_slope = _component(raw.get("outSlope"), component, 0.0, True)
            in_weight = _component(raw.get("inWeight"), component, 1.0 / 3.0)
            out_weight = _component(raw.get("outWeight"), component, 1.0 / 3.0)
        keys.append(Key(time, value, in_slope, out_slope, in_weight, out_weight, _int(raw.get("weightedMode")) & 3))
        if len(keys) > MAX_KEYS:
            raise AnimationParseError(f"too many keys (> {MAX_KEYS})")
    keys.sort(key=lambda k: k.time)
    # 同じ時刻のキーが並ぶことがある（階段の表現）。評価では後のものを使うので、そのまま残す
    return keys


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 数字だけの名前は YAML のパーサーが数値にする
        return str(int(value)) if float(value).is_integer() else str(value)
    return ""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _is_humanoid(class_id: int, path: str, attribute: str) -> bool:
    return class_id == CLASS_ANIMATOR and not path and bool(_HUMANOID_RE.search(attribute))


def _float_curve(raw: Mapping, clip: AnimationClip) -> Curve | None:
    """``m_FloatCurves`` / ``m_EditorCurves`` の 1 本を ``Curve`` にする。読まない種類は None（数は ``clip`` に足す）。"""
    path = _text(raw.get("path"))
    attribute = _text(raw.get("attribute"))
    class_id = _int(raw.get("classID"))
    if _is_humanoid(class_id, path, attribute):
        clip.humanoid = True
        return None
    kind = None
    component = 0
    if attribute.startswith("blendShape.") and len(attribute) > len("blendShape.") and class_id == CLASS_SKINNED_MESH_RENDERER:
        kind = KIND_SHAPE_KEY
    elif attribute == "m_IsActive" and class_id == CLASS_GAME_OBJECT:
        kind = KIND_ACTIVE
    elif attribute == "m_Enabled" and class_id in RENDERER_CLASSES:
        kind = KIND_ENABLED
    elif class_id == CLASS_TRANSFORM and "." in attribute:
        base, _, axis = attribute.rpartition(".")
        if base in _EDITOR_TRANSFORM and axis in "xyzw" and len(axis) == 1:
            kind = _EDITOR_TRANSFORM[base]
            component = "xyzw".index(axis)
            if kind != KIND_ROTATION and component == 3:
                kind = None
    if kind is None:
        clip.other_curves += 1
        return None
    curve = raw.get("curve")
    keys = _keys(curve, None)
    if not keys:
        return None
    order = _int(curve.get("m_RotationOrder"), DEFAULT_ROTATION_ORDER) if isinstance(curve, Mapping) else DEFAULT_ROTATION_ORDER
    return Curve(path, attribute, class_id, kind, keys, component, order if order in ROTATION_ORDERS else DEFAULT_ROTATION_ORDER)


def _vector_curves(raw: Mapping, field_name: str) -> list[Curve]:
    kind, base, axes = _VECTOR_FIELDS[field_name]
    path = _text(raw.get("path"))
    curve = raw.get("curve")
    order = DEFAULT_ROTATION_ORDER
    if isinstance(curve, Mapping):
        order = _int(curve.get("m_RotationOrder"), DEFAULT_ROTATION_ORDER)
        if order not in ROTATION_ORDERS:
            order = DEFAULT_ROTATION_ORDER
    result = []
    for index, axis in enumerate(axes):
        keys = _keys(curve, index)
        if keys:
            result.append(Curve(path, f"{base}.{axis}", CLASS_TRANSFORM, kind, keys, index, order))
    return result


def _check_count(clip: AnimationClip) -> None:
    if len(clip.curves) + clip.other_curves > MAX_CURVES:
        raise AnimationParseError(f"too many curves (> {MAX_CURVES})")


def clip_from_body(body: Mapping[str, Any]) -> AnimationClip:
    """AnimationClip のドキュメントの本体から ``AnimationClip`` を作る。"""
    clip = AnimationClip(_text(body.get("m_Name")))
    rate = _number(body.get("m_SampleRate"), 60.0)
    clip.sample_rate = rate if 0.0 < rate <= 1000.0 else 60.0
    clip.legacy = bool(_int(body.get("m_Legacy")))
    settings = body.get("m_AnimationClipSettings")
    if isinstance(settings, Mapping):
        clip.loop = bool(_int(settings.get("m_LoopTime")))
        start = _number(settings.get("m_StartTime"), 0.0)
        stop = _number(settings.get("m_StopTime"), 0.0)
    else:
        start = stop = 0.0
    if clip.legacy:
        clip.loop = _int(body.get("m_WrapMode")) == 2  # Legacy のクリップは WrapMode.Loop で繰り返す

    for field_name in _VECTOR_FIELDS:
        for raw in _list(body.get(field_name)):
            if isinstance(raw, Mapping):
                clip.curves.extend(_vector_curves(raw, field_name))
                _check_count(clip)
    runtime_transforms = any(c.kind in TRANSFORM_KINDS for c in clip.curves)
    float_curves = _list(body.get("m_FloatCurves"))
    # 実行時のカーブが無いもの（エディターのカーブだけを残したクリップ）は、エディターのカーブを読む
    if not float_curves and not clip.curves:
        float_curves = _list(body.get("m_EditorCurves"))
    for raw in float_curves:
        if not isinstance(raw, Mapping):
            continue
        curve = _float_curve(raw, clip)
        if curve is not None and not (runtime_transforms and curve.kind in TRANSFORM_KINDS):
            clip.curves.append(curve)
        _check_count(clip)
    clip.pptr_curves = len([raw for raw in _list(body.get("m_PPtrCurves")) if isinstance(raw, Mapping)])

    times = [k.time for c in clip.curves for k in c.keys]
    first = min(times) if times else 0.0
    last = max(times) if times else 0.0
    if stop > start >= 0.0 and stop <= MAX_TIME:
        clip.start, clip.stop = start, stop
    else:
        clip.start, clip.stop = min(first, 0.0), max(last, 0.0)
    return clip


def parse_clip(data: bytes | str) -> AnimationClip:
    """.anim の中身（テキストでもバイナリでも）から最初の AnimationClip を読む。"""
    try:
        docs = load_documents(data)
    except ValueError as exc:
        raise AnimationParseError(str(exc)) from exc
    for doc in docs:
        if doc.class_id == CLASS_ANIMATION_CLIP or doc.type_name == "AnimationClip":
            return clip_from_body(doc.body)
    raise AnimationParseError("no AnimationClip in the asset")


# ---------------------------------------------------------------------------
# 状態を切り替えるだけのクリップ
# ---------------------------------------------------------------------------

# これ以下の長さで終わるクリップは、動きではなく状態（Unity の 60fps で 6 フレーム。よくある 1/12 秒・0.1 秒を含む）
STATE_CLIP_MAX_LENGTH = 0.1 + 1e-6


def _flat_slope(slope: float) -> bool:
    return slope == 0.0 or math.isinf(slope)


def _curve_constant(keys: Sequence[Key]) -> bool:
    """値が時間で変わらないカーブ。キーの値がすべて同じで、キーの間の接線が 0 か無限大（階段）のもの。"""
    if len(keys) <= 1:
        return True
    first = keys[0].value
    if any(k.value != first for k in keys):
        return False
    return all(_flat_slope(k0.out_slope) and _flat_slope(k1.in_slope) for k0, k1 in zip(keys, keys[1:]))


def is_state_clip(clip: AnimationClip) -> bool:
    """Animator のステートで使う前提の、状態を切り替えるだけのクリップか。

    どのカーブも値が変わらないか、キーのある範囲が ``STATE_CLIP_MAX_LENGTH`` 以下のもの。カーブが無いクリップは False。
    """
    if not clip.curves:
        return False
    if all(_curve_constant(c.keys) for c in clip.curves):
        return True
    times = [k.time for c in clip.curves for k in c.keys]
    return max(times) - min(times) <= STATE_CLIP_MAX_LENGTH


# ---------------------------------------------------------------------------
# 評価と、ベジェのハンドル
# ---------------------------------------------------------------------------


def _segment_constant(k0: Key, k1: Key) -> bool:
    return math.isinf(k0.out_slope) or math.isinf(k1.in_slope)


def _bezier_x(t: float, x0: float, x1: float, x2: float, x3: float) -> float:
    u = 1.0 - t
    return u * u * u * x0 + 3.0 * u * u * t * x1 + 3.0 * u * t * t * x2 + t * t * t * x3


def _solve_bezier_t(x: float, x0: float, x1: float, x2: float, x3: float) -> float:
    """x 方向が単調な 3 次ベジェで、x になるパラメーター t を二分法で求める。"""
    lo, hi = 0.0, 1.0
    for _ in range(48):
        mid = (lo + hi) * 0.5
        if _bezier_x(mid, x0, x1, x2, x3) < x:
            lo = mid
        else:
            hi = mid
    return (lo + hi) * 0.5


def _segment_handles(k0: Key, k1: Key) -> tuple[tuple[float, float], tuple[float, float]]:
    """k0 → k1 の区間の 3 次ベジェの制御点 2 つ（時刻, 値）。重みの無いキーは 1/3 の長さにする。"""
    dt = k1.time - k0.time
    w0 = min(max(k0.out_weight, 0.0), 1.0) if k0.weighted_out else 1.0 / 3.0
    w1 = min(max(k1.in_weight, 0.0), 1.0) if k1.weighted_in else 1.0 / 3.0
    return (
        (k0.time + w0 * dt, k0.value + k0.out_slope * w0 * dt),
        (k1.time - w1 * dt, k1.value - k1.in_slope * w1 * dt),
    )


def evaluate(keys: Sequence[Key], time: float) -> float:
    """カーブの値（キーの範囲の外は端の値）。"""
    if not keys:
        return 0.0
    if time <= keys[0].time:
        return keys[0].value
    if time >= keys[-1].time:
        return keys[-1].value
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if keys[mid].time <= time:
            lo = mid
        else:
            hi = mid
    k0, k1 = keys[lo], keys[hi]
    dt = k1.time - k0.time
    if dt <= 0.0:
        return k1.value
    if _segment_constant(k0, k1):
        return k0.value
    if k0.weighted_out or k1.weighted_in:
        (x1, y1), (x2, y2) = _segment_handles(k0, k1)
        t = _solve_bezier_t(time, k0.time, x1, x2, k1.time)
        return _bezier_x(t, k0.value, y1, y2, k1.value)
    # エルミート
    t = (time - k0.time) / dt
    t2, t3 = t * t, t * t * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    return h00 * k0.value + h10 * dt * k0.out_slope + h01 * k1.value + h11 * dt * k1.in_slope


@dataclass(frozen=True)
class BezierKey:
    """Blender のキーフレームにする値（時刻は秒）。``constant`` はこのキーから次のキーまで値を保つ。"""

    time: float
    value: float
    left: tuple[float, float]
    right: tuple[float, float]
    constant: bool


def bezier_handles(keys: Sequence[Key], scale: float = 1.0) -> list[BezierKey]:
    """キーの列を、Blender のベジェのキーフレーム（ハンドルは絶対位置）にする。値は ``scale`` 倍する。

    エルミートの区間は、接線を 1/3 の長さのハンドルにすると同じ曲線になる。weighted の区間は重みの長さにする。
    同じ時刻に並んだキーは後のものだけを残す（Blender の F カーブは同じフレームに 2 つのキーを持てない）。
    """
    unique: list[Key] = []
    for key in keys:
        if unique and key.time <= unique[-1].time:
            unique[-1] = key
        else:
            unique.append(key)
    result: list[BezierKey] = []
    for i, key in enumerate(unique):
        value = key.value * scale
        left = (key.time, value)
        right = (key.time, value)
        constant = False
        if i > 0:
            prev = unique[i - 1]
            if not _segment_constant(prev, key):
                _, (x, y) = _segment_handles(prev, key)
                left = (x, y * scale)
        if i + 1 < len(unique):
            nxt = unique[i + 1]
            if _segment_constant(key, nxt):
                constant = True
            else:
                (x, y), _ = _segment_handles(key, nxt)
                right = (x, y * scale)
        result.append(BezierKey(key.time, value, left, right, constant))
    return result


# Transform をフレームごとに焼くときの時刻の数の上限（長すぎるクリップは間隔を広げる）
MAX_SAMPLES = 20000


def sample_times(curves: Iterable[Curve], start: float, stop: float, rate: float) -> list[float]:
    """Transform をフレームごとに焼くときの時刻（``rate`` 毎秒。キーの時刻も含める）。

    数が ``MAX_SAMPLES`` を超えるときは、間隔を広げて ``MAX_SAMPLES`` に収める（キーの時刻は数に入れない）。
    """
    rate = rate if rate > 0 else 60.0
    times = {start, stop}
    span = max(stop - start, 0.0)
    count = int(math.floor(span * rate + 1e-6))
    step = 1.0 / rate
    if count > MAX_SAMPLES:
        count = MAX_SAMPLES
        step = span / count
    for i in range(count + 1):
        times.add(start + i * step)
    keys: set[float] = set()
    for curve in curves:
        for key in curve.keys:
            if start <= key.time <= stop:
                keys.add(key.time)
                if len(keys) > MAX_SAMPLES:
                    break
    times |= keys
    return sorted(t for t in times if start <= t <= stop)


def euler_to_quaternion(degrees: Sequence[float], order: int = DEFAULT_ROTATION_ORDER) -> tuple[float, float, float, float]:
    """Unity のオイラー角（度）を Unity のクォータニオン (x, y, z, w) にする。

    ``order`` の文字の順に軸を回す（既定の ZXY は Z → X → Y。``Quaternion.Euler`` と同じ）。
    """
    axes = ROTATION_ORDERS.get(order, "ZXY")
    q = (0.0, 0.0, 0.0, 1.0)
    for axis in axes:
        angle = math.radians(degrees["XYZ".index(axis)]) * 0.5
        s, c = math.sin(angle), math.cos(angle)
        r = (s if axis == "X" else 0.0, s if axis == "Y" else 0.0, s if axis == "Z" else 0.0, c)
        q = _quat_multiply(r, q)  # 後に回す軸を左から掛ける
    return q


def _quat_multiply(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float, float]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quaternion_to_euler(q: Sequence[float], order: int = DEFAULT_ROTATION_ORDER) -> tuple[float, float, float]:
    """Unity のクォータニオン (x, y, z, w) を、``order`` のオイラー角（度、x, y, z の順）にする（``euler_to_quaternion`` の逆）。

    R = R_k·R_j·R_i（i を先に回す）のとき、s を (i, j, k) が巡回なら 1、そうでなければ -1 として
    θj = asin(-s·R[k][i])、θi = atan2(s·R[k][j], R[k][k])、θk = atan2(s·R[j][i], R[i][i])。
    """
    x, y, z, w = q
    norm = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    m = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    axes = ROTATION_ORDERS.get(order, "ZXY")
    i, j, k = ("XYZ".index(a) for a in axes)
    s = 1.0 if (j - i) % 3 == 1 else -1.0
    angles = [0.0, 0.0, 0.0]
    sin_j = max(-1.0, min(1.0, -s * m[k][i]))
    angles[j] = math.asin(sin_j)
    if abs(sin_j) < 1.0 - 1e-9:
        angles[i] = math.atan2(s * m[k][j], m[k][k])
        angles[k] = math.atan2(s * m[j][i], m[i][i])
    else:
        # ジンバルロック: θk を 0 にして θi に寄せる
        angles[i] = math.atan2(-s * m[j][k], m[j][j])
    return (math.degrees(angles[0]), math.degrees(angles[1]), math.degrees(angles[2]))


def transform_spec(curves: Iterable[Curve], time: float, rest_rotation: Sequence[float]) -> dict[str, list[float | None]]:
    """1 つの Transform のカーブの、``time`` での値を ``model_space.local_basis`` の ``spec`` の形にする。

    オイラー回転のカーブはクォータニオンにする（アニメーションしていない成分は、元の回転をオイラー角にした値）。
    クォータニオンの一部の成分だけのカーブは、残りを元の回転で埋めてから正規化する。
    """
    position: list[float | None] = [None, None, None]
    scale: list[float | None] = [None, None, None]
    rotation: list[float | None] = [None, None, None, None]
    euler: list[float | None] = [None, None, None]
    order = DEFAULT_ROTATION_ORDER
    for curve in curves:
        value = evaluate(curve.keys, time)
        if curve.kind == KIND_POSITION and curve.component < 3:
            position[curve.component] = value
        elif curve.kind == KIND_SCALE and curve.component < 3:
            scale[curve.component] = value
        elif curve.kind == KIND_ROTATION and curve.component < 4:
            rotation[curve.component] = value
        elif curve.kind == KIND_EULER and curve.component < 3:
            euler[curve.component] = value
            order = curve.rotation_order
    if any(v is not None for v in euler):
        rest = quaternion_to_euler(rest_rotation, order)
        filled = [rest[i] if v is None else v for i, v in enumerate(euler)]
        rotation = list(euler_to_quaternion(filled, order))
    elif any(v is not None for v in rotation):
        filled = [float(rest_rotation[i]) if v is None else v for i, v in enumerate(rotation)]
        norm = math.sqrt(sum(v * v for v in filled))
        rotation = [v / norm for v in filled] if norm > 1e-12 else [float(v) for v in rest_rotation]
    return {"position": position, "rotation": rotation, "scale": scale}
