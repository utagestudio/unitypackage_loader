"""読み込んだクリップを NLA のトラックに置く共通の処理（.anim は ``animation.py``、モデルのテイクは ``fbx_motion.py``）。

- クリップごとに、クリップの名前のトラックを 1 本足し、ストリップを 1 つ置く。
- Animation のオプション（Muted / Active / None）が Active なら、対象ごとに最初の 1 本だけミュートを外す。残りはミュート。
- ループするクリップ（Unity の Loop Time）は、ストリップの繰り返しでシーンのフレーム範囲の終わりまで伸ばす。
"""

from __future__ import annotations

import math

import bpy

# Animation のオプションの値（``ImportOptions.animation``）
MODE_MUTED = "MUTED"
MODE_ACTIVE = "ACTIVE"
MODE_NONE = "NONE"


def add_track(owner: bpy.types.ID, name: str, action: bpy.types.Action, slot, scene: bpy.types.Scene, *,
              active: bool, frame_range: tuple[float, float] | None = None, loop: bool = False) -> bpy.types.NlaStrip:
    """``owner`` に ``name`` のトラックを足し、シーンの最初のフレームに ``action`` のストリップを置く。

    ``active`` は Animation が Active かどうか。そのときでも、ミュートを外すのは ``owner`` の最初のトラックだけ。
    ``frame_range`` は使う Action の範囲（無ければ Action 全体）。``loop`` ならシーンの終わりまで繰り返す。
    """
    animation_data = owner.animation_data or owner.animation_data_create()
    # 新しいトラックはミュートされていない状態で作られるので、足す前に数える
    unmute = active and not any(not t.mute for t in animation_data.nla_tracks)
    track = animation_data.nla_tracks.new()
    track.name = name
    strip = track.strips.new(name, int(scene.frame_start), action)
    if slot is not None and strip.action_slot != slot:
        strip.action_slot = slot
    if frame_range is not None:
        set_strip_range(strip, *frame_range)
    if loop:
        loop_strip(strip, scene)
    track.mute = not unmute
    return strip


def copy_track(animation_data, track, strip, action: bpy.types.Action) -> None:
    """``strip`` と同じ範囲・名前・繰り返しのトラックを ``animation_data`` に足す（Action は ``action`` に差し替える）。

    元のトラックがミュートでなくても、``animation_data`` にミュートでないトラックがすでにあればミュートにする。
    """
    has_active = any(not t.mute for t in animation_data.nla_tracks)
    new_track = animation_data.nla_tracks.new()
    new_track.name = track.name
    new_strip = new_track.strips.new(strip.name, int(strip.frame_start), action)
    slot = action.slots.get(strip.action_slot.identifier) if strip.action_slot is not None else None
    if slot is not None and new_strip.action_slot != slot:
        new_strip.action_slot = slot
    set_strip_range(new_strip, strip.action_frame_start, strip.action_frame_end)
    new_strip.repeat = strip.repeat
    new_track.mute = track.mute or has_active


def loop_strip(strip, scene: bpy.types.Scene) -> None:
    """ループするクリップ（Unity の Loop Time）のストリップを、シーンのフレーム範囲の終わりまで繰り返す。

    NLA はストリップの範囲の外を評価しない（Action の Cycles モディファイアーは範囲の中でしか働かない）ので、繰り返しの回数で表す。
    """
    length = strip.action_frame_end - strip.action_frame_start
    if length <= 0:
        return
    span = scene.frame_end - strip.frame_start + 1
    strip.repeat = float(max(1, math.ceil(span / length)))


def set_strip_range(strip, start: float, end: float) -> None:
    """ストリップが使う Action の範囲を変え、ストリップの長さをそれに合わせる（置く位置は変えない）。"""
    begin = strip.frame_start
    if start >= strip.action_frame_end:
        strip.action_frame_end = end
        strip.action_frame_start = start
    else:
        strip.action_frame_start = start
        strip.action_frame_end = end
    strip.frame_start = begin
    strip.frame_end = begin + (end - start) * strip.scale * strip.repeat
