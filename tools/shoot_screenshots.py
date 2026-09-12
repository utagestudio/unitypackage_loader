"""紹介ページ用のスクリーンショットを、他アドオンの無い素の Blender で撮る。

ユーザーの環境には別のアドオンが入っていることが多く、サイドバーのタブやヘッダーに
それらが写り込む。``--factory-startup`` で起動した Blender にリポジトリの実体だけを
登録して撮ることで、素の Blender + このアドオンだけの画になる。

    blender --factory-startup --python tools/shoot_screenshots.py -- <package> <outdir> [shot]

``shot`` は ``main``（hero / nodes / modes）、``dialog``（インポートのポップアップ）、
``menu``（File > Import メニュー）のいずれか。省略時は ``main``。ポップアップとメニューは
モーダルで閉じられないため、撮ったらそのまま Blender を終了する。別々に起動すること。

アセット固有の名前は持たない。ポーズ対象のボーンは VRM の humanoid 命名から探す。
"""

from __future__ import annotations

import math
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
import mathutils

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- setup


def register_addon() -> None:
    """symlink 先ではなくリポジトリの実体を登録する（統合テストと同じ方法）。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import unitypackage_loader
    from unitypackage_loader.ui import panel_report

    # サイドバーの Materials は既定で閉じている。撮影ではマテリアルの解決状況を見せたい。
    panel_report.UNITYPKG_PT_report_materials.bl_options = set()
    unitypackage_loader.register()


def hide_clutter() -> None:
    """アーマチュア、スプリングボーン先端の空オブジェクト、glTF の非表示用コレクションを隠す。"""
    for obj in bpy.data.objects:
        not_exported = any(c.name == "glTF_not_exported" for c in obj.users_collection)
        if obj.type in {"ARMATURE", "EMPTY"} or not_exported:
            obj.hide_set(True)
            obj.hide_render = True


def pose_arms(down_deg: float = 42.0, elbow_deg: float = 8.0) -> bool:
    """T ポーズを A ポーズにする。VRM humanoid の腕ボーンが見つかったときだけ。"""
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    if arm is None:
        return False

    def find(side: str, part: str) -> str | None:
        for bone in arm.pose.bones:
            name = bone.name.lower()
            if part.lower() in name and f"_{side.lower()}_" in name:
                return bone.name
        return None

    pairs = [(find("L", "UpperArm"), down_deg), (find("R", "UpperArm"), -down_deg),
             (find("L", "LowerArm"), elbow_deg), (find("R", "LowerArm"), -elbow_deg)]
    if not all(name for name, _ in pairs):
        return False

    arm.hide_set(False)
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="POSE")
    for name, deg in pairs:
        bone = arm.pose.bones[name]
        head = bone.matrix.translation.copy()
        rot = (mathutils.Matrix.Translation(head)
               @ mathutils.Matrix.Rotation(math.radians(deg), 4, "Y")
               @ mathutils.Matrix.Translation(-head))
        bone.matrix = rot @ bone.matrix
        bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode="OBJECT")
    arm.hide_set(True)
    return True


def build_lights() -> None:
    """トゥーンの陰が素直に出る三灯。Sun は向きだけが効くので位置は既定のまま。"""
    scene = bpy.context.scene
    rig = bpy.data.collections.new("Lighting")
    scene.collection.children.link(rig)
    for name, energy, rx, rz, angle, color in (
        ("Key", 2.2, 58, -32, 6, (1.0, 1.0, 1.0)),      # 正面やや上・向かって左
        ("Rim", 1.2, -48, 38, 10, (1.0, 0.97, 0.92)),   # 背面やや上・向かって右
        ("Fill", 0.2, 72, 55, 25, (0.93, 0.95, 1.0)),   # 弱い正面フィル
    ):
        data = bpy.data.lights.new(name, type="SUN")
        data.energy, data.angle, data.color = energy, math.radians(angle), color
        obj = bpy.data.objects.new(name, data)
        obj.rotation_euler = (math.radians(rx), 0.0, math.radians(rz))
        rig.objects.link(obj)


def imported_meshes() -> list[bpy.types.Object]:
    return [o for o in bpy.data.objects
            if o.type == "MESH" and not any(c.name == "glTF_not_exported" for c in o.users_collection)]


def view3d():
    area = next(a for a in bpy.context.window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    return area, region


def setup_viewport(sidebar: bool = True) -> None:
    area, region = view3d()
    space = area.spaces.active
    space.shading.type = "MATERIAL"
    space.shading.use_scene_lights = True
    space.shading.use_scene_world = False
    space.shading.studiolight_intensity = 0.08
    space.overlay.show_cursor = False
    space.overlay.show_text = False
    space.overlay.show_relationship_lines = False
    space.overlay.show_extras = False          # ライトやエンプティのギズモ線を出さない
    space.show_region_ui = sidebar


def select_sidebar_tab(category: str = "Unity Package") -> None:
    """サイドバーのタブを選ぶ。領域が一度描画されるまで書き込めないので、手順の中で呼ぶ。"""
    area, _ = view3d()
    ui = next((r for r in area.regions if r.type == "UI"), None)
    if ui is None:
        print("[warn] sidebar region not found")
        return
    try:
        ui.active_panel_category = category
        print("[info] sidebar tab:", ui.active_panel_category)
    except AttributeError as exc:
        print("[warn] could not select sidebar tab:", exc)


def frame_front(objects: list[bpy.types.Object]) -> None:
    area, region = view3d()
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    with bpy.context.temp_override(area=area, region=region):
        bpy.ops.view3d.view_axis(type="FRONT")
        bpy.ops.view3d.view_selected()
    bpy.ops.object.select_all(action="DESELECT")


def shoot(out_dir: Path, name: str) -> None:
    path = out_dir / f"{name}.png"
    bpy.ops.screen.screenshot(filepath=str(path))
    print(f"[shot] {path}")


# --------------------------------------------------------------------------- shots


def richest_material() -> bpy.types.Material | None:
    """画像テクスチャノードが一番多いマテリアル（ノード図が映える）。"""
    best, best_n = None, -1
    for mat in bpy.data.materials:
        if not mat.use_nodes or "unity_normalized" not in mat:
            continue
        n = sum(1 for node in mat.node_tree.nodes if node.type == "TEX_IMAGE")
        if n > best_n:
            best, best_n = mat, n
    return best


def to_shader_editor() -> None:
    """Layout の 3D ビューをシェーダーエディターに切り替え、代表マテリアルを表示する。

    Shading ワークスペースはファイルブラウザを含み、そこにホームディレクトリの中身が
    写ってしまうため使わない。
    """
    mat = richest_material()
    owner = next((o for o in imported_meshes()
                  if any(s.material is mat for s in o.material_slots)), None)
    if owner is not None and mat is not None:
        bpy.ops.object.select_all(action="DESELECT")
        owner.select_set(True)
        bpy.context.view_layer.objects.active = owner
        owner.active_material_index = next(
            i for i, s in enumerate(owner.material_slots) if s.material is mat)
        print("[info] node shot material:", mat.name)
    area, _ = view3d()
    area.type = "NODE_EDITOR"
    space = area.spaces.active
    space.tree_type = "ShaderNodeTree"
    space.shader_type = "OBJECT"
    space.show_region_ui = False


def frame_nodes() -> None:
    area = next(a for a in bpy.context.window.screen.areas if a.type == "NODE_EDITOR")
    region = next(r for r in area.regions if r.type == "WINDOW")
    with bpy.context.temp_override(area=area, region=region):
        bpy.ops.node.view_all()


def back_to_view3d() -> None:
    area = next(a for a in bpy.context.window.screen.areas if a.type == "NODE_EDITOR")
    area.type = "VIEW_3D"


def setup_modes_shot() -> None:
    """同じモデルを Toon と Principled で並べる（サイドバーの Rebuild in Another Mode 相当）。

    アーマチュア変形したメッシュをそのまま複製して動かすと、ボーンとの位置関係が崩れて
    形が壊れる。複製側はモディファイアを適用してポーズを焼き込み、親子付けも切ってから動かす。
    """
    setup_viewport(sidebar=False)
    originals = imported_meshes()
    width = max(o.dimensions.x for o in originals) or 1.0

    bpy.ops.object.select_all(action="DESELECT")
    for obj in originals:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = originals[0]
    bpy.ops.object.duplicate(linked=False)

    copies = list(bpy.context.selected_objects)
    bpy.ops.object.convert(target="MESH")                       # ポーズを焼き込む
    bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
    for obj in copies:
        obj.location.x += width * 1.15
        for slot in obj.material_slots:                         # 複製側だけ別マテリアルにする
            if slot.material is not None:
                slot.material = slot.material.copy()

    bpy.context.view_layer.objects.active = copies[0]
    bpy.ops.unitypkg.rebuild_material(mode="PRINCIPLED", scope="SELECTED")

    # 元のメッシュは動かさない。アーマチュアを置いたままメッシュだけずらすと、
    # モディファイアが変形を引き戻して肩などが歪む。
    frame_front(originals + copies)


def open_import_dialog(package: Path) -> None:
    bpy.ops.import_scene.unitypackage("INVOKE_DEFAULT", filepath=str(package))


def open_import_menu() -> None:
    """File > Import メニューを開く。

    マウスの下にある項目にはツールチップが出て紛らわしいので、開いた直後に
    カーソルをメニューの外へ逃がす。
    """
    window = bpy.context.window
    bpy.ops.wm.call_menu(name="TOPBAR_MT_file_import")
    window.cursor_warp(int(window.width * 0.2), int(window.height * 0.5))


# --------------------------------------------------------------------------- main


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(argv) < 2:
        raise SystemExit(__doc__)
    package, out_dir = Path(argv[0]), Path(argv[1])
    shot = argv[2] if len(argv) > 2 else "main"
    out_dir.mkdir(parents=True, exist_ok=True)

    # UI が小さいまま縮小するとページ上で字が潰れる。撮影時だけ拡大する
    # （--factory-startup なのでユーザーの設定には残らない）。
    bpy.context.preferences.view.ui_scale = 1.6 if shot in {"dialog", "menu"} else 1.25

    register_addon()
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    bpy.context.scene.view_settings.view_transform = "Standard"

    # リポジトリ直接登録では Extension のキャッシュディレクトリが使えないので展開先を明示する
    bpy.ops.import_scene.unitypackage(
        filepath=str(package),
        extract_mode="CUSTOM",
        extract_path=tempfile.mkdtemp(prefix="unitypackage_loader_shots_"),
    )
    hide_clutter()
    pose_arms()
    build_lights()
    setup_viewport()
    frame_front(imported_meshes())

    steps: list = []
    if shot == "main":
        steps = [
            select_sidebar_tab,
            lambda: shoot(out_dir, "hero"),
            to_shader_editor,
            frame_nodes,
            lambda: shoot(out_dir, "nodes"),
            back_to_view3d,
            setup_modes_shot,
            lambda: shoot(out_dir, "modes"),
        ]
    elif shot == "dialog":
        steps = [lambda: open_import_dialog(package), lambda: shoot(out_dir, "dialog")]
    elif shot == "menu":
        steps = [open_import_menu, lambda: shoot(out_dir, "menu")]
    else:
        raise SystemExit(f"unknown shot: {shot}")

    queue = list(steps)

    def tick():
        if not queue:
            bpy.ops.wm.quit_blender()
            return None
        try:
            queue.pop(0)()
        except Exception:
            import traceback
            traceback.print_exc()
            bpy.ops.wm.quit_blender()
            return None
        return 0.7

    bpy.app.timers.register(tick, first_interval=2.0)


try:
    main()
except Exception:            # 失敗しても GUI を開いたまま止めない
    traceback.print_exc()

    def _quit():
        bpy.ops.wm.quit_blender()
        return None

    bpy.app.timers.register(_quit, first_interval=0.5)
