"""テスト用の AnimationClip（.anim）の Unity YAML を書く（#209）。単体テストと合成パッケージの生成で使う。

キーは (time, value, in_slope, out_slope) か、weighted のときは (time, value, in_slope, out_slope, weighted_mode, in_weight, out_weight)。
ベクトルのカーブ（位置・オイラー回転・スケール・クォータニオン）の value と接線は成分のタプル。接線に ``float("inf")`` を
渡すと ``Infinity``（階段）になる。
"""

from __future__ import annotations

import math

HEADER = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!74 &7400000\n"


def _num(value: float) -> str:
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return repr(float(value)) if not float(value).is_integer() else str(int(value))


def _vec(values, axes: str = "xyz") -> str:
    return "{" + ", ".join(f"{a}: {_num(v)}" for a, v in zip(axes, values)) + "}"


def _key_lines(key, axes: str | None) -> list[str]:
    time, value, in_slope, out_slope, *rest = key
    weighted, in_weight, out_weight = (rest + [0, None, None])[:3]
    if axes is None:
        fmt = _num
        default_weight = 1 / 3
    else:
        def fmt(v):
            return _vec(v, axes)

        default_weight = (1 / 3,) * len(axes)
    in_weight = default_weight if in_weight is None else in_weight
    out_weight = default_weight if out_weight is None else out_weight
    return [
        "      - serializedVersion: 3",
        f"        time: {_num(time)}",
        f"        value: {fmt(value)}",
        f"        inSlope: {fmt(in_slope)}",
        f"        outSlope: {fmt(out_slope)}",
        "        tangentMode: 0",
        f"        weightedMode: {weighted}",
        f"        inWeight: {fmt(in_weight)}",
        f"        outWeight: {fmt(out_weight)}",
    ]


def _curve_block(keys, axes: str | None, rotation_order: int = 4) -> list[str]:
    lines = ["    curve:", "      serializedVersion: 2", "      m_Curve:"]
    for key in keys:
        lines += _key_lines(key, axes)
    lines += ["      m_PreInfinity: 2", "      m_PostInfinity: 2", f"      m_RotationOrder: {rotation_order}"]
    return lines


_VECTOR_AXES = {"m_PositionCurves": "xyz", "m_EulerCurves": "xyz", "m_ScaleCurves": "xyz", "m_RotationCurves": "xyzw"}


def clip_yaml(
    name: str,
    float_curves=(),
    vector_curves: dict | None = None,
    loop: bool = False,
    sample_rate: float = 60,
    stop_time: float = 0,
    pptr: int = 0,
) -> str:
    """AnimationClip の YAML。``float_curves`` は (path, attribute, classID, keys)、``vector_curves`` は
    {"m_PositionCurves": [(path, keys[, rotation_order])], ...}。"""
    vector_curves = vector_curves or {}
    lines = [
        "AnimationClip:",
        "  m_ObjectHideFlags: 0",
        f"  m_Name: {name}",
        "  serializedVersion: 7",
        "  m_Legacy: 0",
        "  m_Compressed: 0",
        "  m_UseHighQualityCurve: 1",
    ]
    for field_name in ("m_RotationCurves", "m_CompressedRotationCurves", "m_EulerCurves", "m_PositionCurves", "m_ScaleCurves"):
        entries = vector_curves.get(field_name, [])
        if not entries:
            lines.append(f"  {field_name}: []")
            continue
        lines.append(f"  {field_name}:")
        for entry in entries:
            path, keys = entry[0], entry[1]
            order = entry[2] if len(entry) > 2 else 4
            block = _curve_block(keys, _VECTOR_AXES[field_name], order)
            block[0] = "  - curve:"
            lines += block
            lines.append(f"    path: {path}")
    if float_curves:
        lines.append("  m_FloatCurves:")
        for path, attribute, class_id, keys in float_curves:
            lines.append("  - serializedVersion: 2")
            lines += _curve_block(keys, None)
            lines += [f"    attribute: {attribute}", f"    path: {path}", f"    classID: {class_id}", "    script: {fileID: 0}", "    flags: 0"]
    else:
        lines.append("  m_FloatCurves: []")
    if pptr:
        lines.append("  m_PPtrCurves:")
        for i in range(pptr):
            lines += [
                "  - serializedVersion: 2",
                "    curve:",
                "    - time: 0",
                "      value: {fileID: 2100000, guid: " + "e" * 32 + ", type: 2}",
                "    attribute: m_Materials.Array.data[0]",
                f"    path: part{i}",
                "    classID: 23",
                "    script: {fileID: 0}",
            ]
    else:
        lines.append("  m_PPtrCurves: []")
    lines += [
        f"  m_SampleRate: {_num(sample_rate)}",
        "  m_WrapMode: 0",
        "  m_AnimationClipSettings:",
        "    serializedVersion: 2",
        "    m_StartTime: 0",
        f"    m_StopTime: {_num(stop_time)}",
        f"    m_LoopTime: {1 if loop else 0}",
        "  m_EditorCurves: []",
        "  m_EulerEditorCurves: []",
        "  m_HasGenericRootTransform: 0",
        "  m_HasMotionFloatCurves: 0",
        "  m_Events: []",
    ]
    return HEADER + "\n".join(lines) + "\n"
