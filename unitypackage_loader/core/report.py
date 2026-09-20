"""インポート結果のレポート（Info バー要約・詳細ログ）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["MaterialReport", "ImportReport", "sanitize_display"]

# C0 制御文字（改行・タブ含む）、DEL、C1 制御文字（8 ビットの ESC / CSI など）。
# 名前やパスはパッケージ由来の文字列なので、ANSI エスケープで端末や UI の表示を乱せないようにする
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_display(text: str) -> str:
    """表示・ログ用に制御文字を ``\\x1b`` のような可視表現へ置き換える。"""
    return _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group(0)):02x}", text)


@dataclass
class MaterialReport:
    blender_name: str
    fbx_name: str
    guid: str | None
    method: str  # external / name / prefab / none / reused / kept / delegated / replaced / prefab-split / shared
    family: str = ""
    shader_name: str = ""
    alpha_mode: str = ""
    mode: str = ""  # PRINCIPLED / UNLIT / NAMES_ONLY
    textures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    package: str
    scenes: list[str] = field(default_factory=list)  # 読み込む単位 Scenes で読み込んだシーンの pathname
    prefabs: list[str] = field(default_factory=list)  # 読み込む単位 Prefabs で読み込んだ prefab の pathname
    models: list[str] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    materials: list[MaterialReport] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    extract_root: str = ""
    outlines: int = 0
    split_slots: int = 0  # prefab の割り当てに従って差し替えた（スロットが無ければ作って入れた）マテリアルスロット数
    lights: int = 0  # シーンから作ったライト
    cameras: int = 0  # シーンから作ったカメラ
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # 別のマテリアルに置き換わり、Blender 上に独立して残っていない行（replaced: prefab の割り当てで使われなくなった、
    # shared: 同じモデルを別の prefab 用に読み直し、組み立て済みのマテリアルを使った）
    _INACTIVE_METHODS = frozenset({"replaced", "shared"})

    @property
    def mapped_count(self) -> int:
        return sum(1 for m in self.materials if m.guid and m.method not in self._INACTIVE_METHODS)

    @property
    def active_materials(self) -> list[MaterialReport]:
        return [m for m in self.materials if m.method not in self._INACTIVE_METHODS]

    def summary(self) -> str:
        parts = [
            f"Imported {len(self.objects)} objects",
            f"{len(self.active_materials)} materials ({self.mapped_count} mapped)",
            f"{len(self.images)} textures",
        ]
        if self.split_slots:
            parts.append(f"{self.split_slots} slots assigned from prefab")
        if self.outlines:
            parts.append(f"{self.outlines} outlines")
        if self.lights or self.cameras:
            parts.append(f"{self.lights} lights, {self.cameras} cameras")
        text = ", ".join(parts)
        problems = [f"{len(self.errors)} error(s)"] if self.errors else []
        if self.warnings:
            problems.append(f"{len(self.warnings)} warning(s)")
        if problems:
            text += f". {', '.join(problems)} — see the system console"
        return text

    def as_text(self) -> str:
        lines = [f"[Unitypackage Importer] {self.package}", f"  extract root: {self.extract_root}"]
        if self.scenes:
            lines.append(f"  scenes ({len(self.scenes)}):")
            lines += [f"    - {s}" for s in self.scenes]
        if self.prefabs:
            lines.append(f"  prefabs ({len(self.prefabs)}):")
            lines += [f"    - {p}" for p in self.prefabs]
        lines.append(f"  models ({len(self.models)}):")
        lines += [f"    - {m}" for m in self.models]
        lines.append(f"  objects ({len(self.objects)}):")
        lines += [f"    - {o}" for o in self.objects]
        lines.append(f"  materials ({len(self.materials)}, {self.mapped_count} mapped):")
        for m in self.materials:
            guid = m.guid or "-"
            lines.append(
                f"    - {m.blender_name}  <- {m.fbx_name}  [{m.method}] {m.family}/{m.shader_name or '-'}"
                f" alpha={m.alpha_mode or '-'} mode={m.mode or '-'} guid={guid}"
            )
            lines += [f"        tex: {t}" for t in m.textures]
            lines += [f"        ! {w}" for w in m.warnings]
        lines.append(f"  images ({len(self.images)}):")
        lines += [f"    - {i}" for i in self.images]
        if self.lights or self.cameras:
            lines.append(f"  lights: {self.lights}, cameras: {self.cameras}")
        if self.warnings:
            lines.append(f"  warnings ({len(self.warnings)}):")
            lines += [f"    ! {w}" for w in self.warnings]
        if self.errors:
            lines.append(f"  errors ({len(self.errors)}):")
            lines += [f"    X {e}" for e in self.errors]
        # 各行はこちらで組み立てているので、行ごとに無害化すれば名前に含まれる改行も可視化される
        return "\n".join(sanitize_display(line) for line in lines)
