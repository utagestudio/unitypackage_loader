"""モデル（FBX など）の中のアニメーションのテイクを、Unity のクリップの設定どおりに NLA に並べる（#276）。

Blender の FBX インポーターは、テイクごとにテイクの名前の Action を作り、最初のテイクをアクティブな Action にする。
Unity はモデルの .meta の ``clipAnimations`` で、テイクから名前・範囲を決めてクリップを切り出す（無ければテイクごとに全体）。
ここでは読み込んだ直後に、クリップごとに範囲を絞った NLA ストリップを、クリップの名前のトラックに置く。

- 同じテイクの Action を範囲違いのストリップで共有する（クリップごとに Action を複製しない）。
- 状態を切り替えるだけの短いクリップ（T ポーズなど。``core.animation.STATE_CLIP_MAX_LENGTH`` 以下）は置かない。
- どのクリップにも使われないテイクの Action は消す。アクティブな Action は外す（読み込んだ直後の見た目を、アニメーションを
  読まないときと同じにする）。
- 同じモデルを何度も読む（シーンの配置ごと）ときは、最初に読んだときの Action を使い回し、新しく作られた分は消す。
- トラックの置き方（Active のときのミュート、ループ）は ``nla.py``。モーションだけのモデルのアーマチュアを 1 体にまとめるのは
  ``rig_merge.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import bpy

from ..core.animation import STATE_CLIP_MAX_LENGTH
from ..core.meta import ModelClip, ModelImporterInfo, strip_numeric_suffix
from .model_import import FBX_NODE_PROP, is_removed
from .nla import MODE_ACTIVE, add_track

# Blender の FBX インポーターの anim_offset の既定値（FBX のフレーム 0 を Blender のフレーム 1 に置く）
ANIM_OFFSET = 1.0
# Action に残す、元のテイクの名前とモデルの GUID
TAKE_PROP = "unity_take"
TAKE_MODEL_PROP = "unity_take_model"



@dataclass
class MotionStats:
    """モデルのテイクから作ったクリップの集計（レポートに書く）。"""

    clips: list[str] = field(default_factory=list)  # 置いたクリップ（"モデルのパス: クリップ名"）
    state: int = 0  # 状態を切り替えるだけで置かなかったクリップ
    missing_takes: list[str] = field(default_factory=list)  # .meta のクリップのテイクが見つからなかったもの
    merged_rigs: int = 0  # 1 体にまとめて消したアーマチュア

    def report_lines(self):
        if self.state:
            yield f"skipped {self.state} model animation clip(s) that only switch a state (0.1 s or shorter, e.g. a T-pose)"
        if self.missing_takes:
            yield f"{len(self.missing_takes)} model animation clip(s) refer to a take that was not found in the model"
        if self.merged_rigs:
            yield f"merged {self.merged_rigs} motion-only armature(s) with the same skeleton into one"


@dataclass(frozen=True)
class _Clip:
    name: str
    action: bpy.types.Action
    start: float  # Blender のフレーム（Action の中の範囲）
    end: float
    loop: bool = False


def _slot_owner(slot, objects: list[bpy.types.Object]):
    """スロットの対象（オブジェクトかシェイプキー）を、読み込んだオブジェクトから探す。"""
    name = slot.identifier[2:]
    if slot.target_id_type == "OBJECT":
        for obj in objects:
            if obj.get(FBX_NODE_PROP, obj.name) == name or obj.name == name:
                return obj
        return None
    if slot.target_id_type == "KEY":
        for obj in objects:
            key = getattr(obj.data, "shape_keys", None) if obj.data is not None else None
            if key is not None and key.name == name:
                return key
    return None


def _users(action: bpy.types.Action, objects: list[bpy.types.Object]) -> list[tuple[bpy.types.ID, bpy.types.ActionSlot]]:
    users = []
    for slot in action.slots:
        owner = _slot_owner(slot, objects)
        if owner is None and len(action.slots) == 1:
            # スロットの名前が付け替わっていても、アニメーションを持つオブジェクトが 1 つなら、それとみなす
            animated = [o for o in objects if o.animation_data is not None and o.animation_data.action == action]
            owner = animated[0] if len(animated) == 1 else None
        if owner is not None:
            users.append((owner, slot))
    return users


def _take_of(action: bpy.types.Action) -> str:
    return str(action.get(TAKE_PROP) or strip_numeric_suffix(action.name))


def _take_name(action_name: str, known: set[str]) -> str:
    """Action の名前から元のテイクの名前を求める。.meta のテイク名と完全一致すればそのまま、無ければ Blender の連番を外す。"""
    return action_name if action_name in known else strip_numeric_suffix(action_name)


class MotionBuilder:
    """読み込みの間、モデルのテイクから NLA を作る（モデルを読み込むたびに ``arrange`` を呼ぶ）。"""

    def __init__(self, scene: bpy.types.Scene, mode: str):
        self.scene = scene
        self.mode = mode
        self.stats = MotionStats()
        # モデルの GUID → テイクの名前 → 最初に読んだときの Action（2 回目以降に使い回す）
        self._actions: dict[str, dict[str, bpy.types.Action]] = {}

    def arrange(self, model_guid: str, pathname: str, info: ModelImporterInfo | None,
                objects: list[bpy.types.Object], actions: list[bpy.types.Action]) -> None:
        if not actions:
            return
        if info is not None and not info.import_animation:
            # Unity でアニメーションを取り込まない設定のモデル（Unity にクリップが無い）
            for obj in objects:
                if obj.animation_data is not None:
                    obj.animation_data.action = None
            bpy.data.batch_remove(actions)
            return
        known = {c.take for c in info.clips} if info is not None else set()
        takes = {a.as_pointer(): _take_name(a.name, known) for a in actions}
        cached = self._actions.get(model_guid)
        if cached is not None and any(is_removed(a) for a in cached.values()):
            cached = None  # 前の読み込みが失敗して片付けで消えた
        first_time = cached is None
        if first_time:
            cached = self._actions[model_guid] = {}
            for action in actions:
                action[TAKE_PROP] = takes[action.as_pointer()]
                action[TAKE_MODEL_PROP] = model_guid
                cached.setdefault(takes[action.as_pointer()], action)
        # 新しい Action の (対象, スロットの識別子) を、テイクの名前ごとに控える（使い回すときは、元の Action の同じ識別子のスロットを使う）
        targets: dict[str, list[tuple[bpy.types.ID, str]]] = {}
        for action in actions:
            targets.setdefault(takes[action.as_pointer()], []).extend((owner, slot.identifier) for owner, slot in _users(action, objects))
        for obj in objects:
            key = getattr(obj.data, "shape_keys", None) if obj.data is not None else None
            for owner in (obj, key):
                if owner is not None and owner.animation_data is not None:
                    owner.animation_data.action = None
        clips = self._clips(info, cached, count=first_time)
        used: set[str] = set()
        for clip in clips:
            take = _take_of(clip.action)
            for owner, identifier in targets.get(take, []):
                slot = clip.action.slots.get(identifier)
                if slot is None:
                    continue
                add_track(owner, clip.name, clip.action, slot, self.scene, active=self.mode == MODE_ACTIVE,
                          frame_range=(clip.start, clip.end), loop=clip.loop)
                used.add(take)
            if first_time:
                self.stats.clips.append(f"{pathname}: {clip.name}")
        removed = [a for a in actions if not (first_time and _take_of(a) in used and cached.get(_take_of(a)) == a)]
        if first_time:
            # どのクリップにも使われなかったテイクは、使い回しの表からも外す
            for take in [t for t in cached if t not in used]:
                del cached[take]
        if removed:
            bpy.data.batch_remove(removed)

    def _clips(self, info: ModelImporterInfo | None, cached: dict[str, bpy.types.Action], count: bool = True) -> list[_Clip]:
        """置くクリップ。``count`` なら、外したものをレポートに数える（同じモデルの 2 回目以降の読み込みでは数えない）。"""
        fps = self.scene.render.fps / (self.scene.render.fps_base or 1.0)
        settings: list[ModelClip] = list(info.clips) if info is not None else []
        clips: list[_Clip] = []
        if not settings:
            for take, action in cached.items():
                start, end = action.frame_range
                settings.append(ModelClip(take, take, start - ANIM_OFFSET, end - ANIM_OFFSET))
        for setting in settings:
            action = cached.get(setting.take)
            if action is None:
                if count:
                    self.stats.missing_takes.append(setting.name)
                continue
            if (setting.last_frame - setting.first_frame) / max(fps, 1e-6) <= STATE_CLIP_MAX_LENGTH:
                if count:
                    self.stats.state += 1
                continue
            clips.append(_Clip(setting.name, action, setting.first_frame + ANIM_OFFSET, setting.last_frame + ANIM_OFFSET, setting.loop))
        return clips
