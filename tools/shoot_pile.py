"""記事用に「配置のない部品の山」と「シーンごと読み込んだ状態」を同じ構図で撮る。

読み込む単位 Models で全モデルを読むと、モデルファイルがそれぞれの原点に置かれるため、
建物も看板も小物も 1 か所に重なる。同じパッケージを読み込む単位 Scenes で読むと、Unity で
組まれた位置・向き・大きさのまま並ぶ。その差を、同じ視点・同じ設定の 2 枚で見せるための道具。

``tools/shoot_screenshots.py`` と同じく、ユーザーの環境の他アドオンが写り込まないよう
``--factory-startup`` の Blender にリポジトリの実体だけを登録する。スプラッシュを出さないために
引数に空の .blend を渡すこと。

    blender --factory-startup <空の .blend> --python tools/shoot_pile.py -- <package> <outdir> [shot] [--opt=値 ...]

``shot``:

    list    モデル数とシーンの一覧を出すだけ（撮らない。``-b`` でも動く）
    pile    読み込む単位 Models で全モデルを読み、原点に重なった状態を撮る（``pile.png``）
    scene   読み込む単位 Scenes でシーンを 1 つ読み、同じ構図で撮る（``scene.png``）
    both    pile を撮ってから中身を捨てて scene を撮る（構図と設定が確実に揃う。メモリを食う）

オプション（``--key=値`` 形式）:

    --scene=<部分一致>       scene / both で読むシーン（省略時は配置が一番多いシーン）
    --limit=<N>              pile で読むモデル数の上限（既定 0 = 全部）
    --view=<X>,<Z>           視点の向き。X は真上が 0・真横が 90、Z は方位（既定 66,46）
    --zoom=<倍率>            収めた後の寄り（1 より小さいほど寄る。既定 1.0）
    --camera                 scene でシーンのカメラから見た画にする（構図の比較にはならない）
    --shading=<MODE>         SOLID / MATERIAL / RENDERED（既定は pile: MATERIAL、scene: RENDERED）
    --env=<float>            スタジオライトの強さ（既定は pile: 0.5、scene: 0.05）
    --view-transform=<名前>  Standard など（省略時は Blender の既定のまま）
    --extract=<dir>          展開先（既定は一時ディレクトリ。同じ場所を使い回すと 2 回目が速い）
    --settle=<N>             撮る前に待つ手番の数（既定は RENDERED で 6、それ以外は 1）
    --full                   ウィンドウ全体を撮る（既定は 3D ビューの領域だけ）
    --no-overlays            グリッドなどのオーバーレイを消す（既定はグリッドを残す）

画の大きさは Blender のウィンドウの大きさになる。揃えたいときは ``--window-geometry 0 0 1920 1080`` を
blender 自体の引数に渡す（``--factory-startup`` の後、``--python`` の前）。

アセット固有の名前は持たない。撮影対象のパッケージは ``_local/`` に置く。
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
    """symlink 先ではなくリポジトリの実体を登録する（統合テスト・撮影スクリプトと同じ方法）。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import unitypackage_loader

    unitypackage_loader.register()


def parse_args() -> tuple[Path, Path, str, dict[str, str]]:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    positional = [a for a in argv if not a.startswith("--")]
    if len(positional) < 2:
        raise SystemExit(__doc__)
    opts: dict[str, str] = {}
    for arg in argv:
        if arg.startswith("--"):
            key, _, value = arg[2:].partition("=")
            opts[key] = value or "1"
    shot = positional[2] if len(positional) > 2 else "both"
    return Path(positional[0]), Path(positional[1]), shot, opts


# --------------------------------------------------------------------------- package


def prepared_package(package: Path):
    from unitypackage_loader.blender.importer import build_shader_table, prepare_package

    return prepare_package(str(package), build_shader_table(), import_blend=False)


def print_contents(prepared) -> None:
    """パッケージの中身を出す。--scene に渡す名前を知るために使う。"""
    models = prepared.models
    print(f"[models] {len(models)} 件（読み込めるもの {len(prepared.supported_models)} 件）")
    for m in models:
        if not m.supported:
            print(f"  - skip {m.entry.pathname}: {m.skip_reason}")
    scenes = prepared.ensure_scenes()
    print(f"[scenes] {len(scenes)} 件")
    for s in scenes:
        mark = "" if s.supported else f"（読み込めない: {s.skip_reason}）"
        print(f"  - {s.pathname}  配置 {len(s.placements)} 件{mark}")


def pick_scene(prepared, wanted: str):
    """``--scene`` の部分一致で選ぶ。省略時は配置が一番多いシーン（いちばん「街」らしい）。"""
    scenes = prepared.supported_scenes
    if not scenes:
        raise SystemExit("読み込めるシーンがパッケージにありません（shot=pile だけが使えます）")
    if wanted:
        matched = [s for s in scenes if wanted.lower() in s.pathname.lower()]
        if not matched:
            names = "\n".join(f"  - {s.pathname}" for s in scenes)
            raise SystemExit(f"--scene={wanted} に当たるシーンがありません:\n{names}")
        return matched[0]
    return max(scenes, key=lambda s: len(s.placements))


def extract_root(package: Path, opts: dict[str, str]) -> str:
    """リポジトリ直接登録では Extension のキャッシュが使えないので展開先を明示する。

    既定でもパッケージ名ごとに同じ場所を使う。overwrite しないので、2 回目の実行では展開を飛ばせる。
    """
    if opts.get("extract"):
        path = Path(opts["extract"]).expanduser()
    else:
        path = Path(tempfile.gettempdir()) / "unitypackage_loader_shots" / package.stem
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def import_package(package: Path, prepared, unit: str, opts: dict[str, str], scene=None) -> None:
    from unitypackage_loader.blender.importer import ImportOptions, run_import

    limit = int(opts.get("limit", "0") or 0)
    model_guids = None
    if unit == "MODELS" and limit:
        model_guids = [m.guid for m in prepared.supported_models][:limit]
        print(f"[info] モデルを先頭 {len(model_guids)} 件に絞ります（--limit）")

    options = ImportOptions(
        models="ALL",
        unit=unit,
        model_guids=model_guids,
        scene_paths=[scene.pathname] if scene is not None else None,
        scene_lights=True,
        scene_cameras=True,
        material_mode="AUTO",
        extract_mode="CUSTOM",
        extract_path=extract_root(package, opts),
        max_extract_size=0,  # Preferences が無いので上限なし。手元の信頼できるパッケージにだけ使う
    )

    last = [""]

    def progress(fraction: float, message: str) -> None:
        if message != last[0]:                       # 同じ文言を何度も出さない
            print(f"[{fraction * 100:3.0f}%] {message}", flush=True)
            last[0] = message

    report = run_import(bpy.context, str(package), options, progress=progress, prepared=prepared)
    print(f"[done] オブジェクト {len(report.objects)} / マテリアル {len(report.materials)} / "
          f"ライト {report.lights} / カメラ {report.cameras} / 警告 {len(report.warnings)}")


# --------------------------------------------------------------------------- viewport


def view3d():
    area = next(a for a in bpy.context.window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    return area, region


def setup_viewport(shading: str, env: float, overlays: bool, scene_lights: bool) -> None:
    area, _ = view3d()
    space = area.spaces.active
    space.shading.type = shading
    if shading == "MATERIAL":
        space.shading.use_scene_lights = scene_lights
        space.shading.use_scene_world = False
        space.shading.studiolight_intensity = env
    space.overlay.show_overlays = overlays
    space.overlay.show_cursor = False
    space.overlay.show_text = False
    space.overlay.show_relationship_lines = False
    space.overlay.show_extras = False            # ライトやカメラのギズモ線を出さない
    space.show_region_ui = False
    space.show_region_toolbar = False
    space.show_region_header = False         # モード表示やメニューを入れない（切り出さずに使える）
    space.show_gizmo_navigate = False        # 右上の軸ギズモとズームのアイコン


def visible_objects() -> list[bpy.types.Object]:
    return [o for o in bpy.data.objects
            if o.type == "MESH" and o.visible_get()
            and not any(c.name == "glTF_not_exported" for c in o.users_collection)]


def frame_view(view: tuple[float, float], zoom: float = 1.0) -> None:
    """指定した向きから、読み込んだものが収まるように視点を合わせる。

    pile と scene で同じ向きを使うことで、2 枚を並べたときに「同じ場所を見ている」画になる。
    """
    objects = visible_objects()
    if not objects:
        raise SystemExit("読み込めたメッシュがありません")
    area, region = view3d()
    rot = mathutils.Euler((math.radians(view[0]), 0.0, math.radians(view[1])), "XYZ").to_quaternion()
    area.spaces.active.region_3d.view_rotation = rot

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    with bpy.context.temp_override(area=area, region=region):
        bpy.ops.view3d.view_selected()
    if zoom != 1.0:                              # 端に余白が多いときに寄せる
        area.spaces.active.region_3d.view_distance *= zoom
    bpy.ops.object.select_all(action="DESELECT")


def look_through_camera() -> bool:
    """シーンから読み込んだカメラがあれば、そこから見た画にする。"""
    camera = next((o for o in bpy.data.objects if o.type == "CAMERA"), None)
    if camera is None:
        print("[warn] シーンにカメラがありませんでした。視点から撮ります")
        return False
    area, region = view3d()
    bpy.context.scene.camera = camera
    with bpy.context.temp_override(area=area, region=region):
        bpy.ops.view3d.view_camera()
    print("[info] カメラ:", camera.name)
    return True


def shoot(out_dir: Path, name: str, full: bool) -> None:
    path = out_dir / f"{name}.png"
    area, region = view3d()
    if full:
        bpy.ops.screen.screenshot(filepath=str(path))
    else:
        # 3D ビューの領域だけを撮る。トップバーやタイムラインが入らないので切り出さずに使える
        with bpy.context.temp_override(area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=str(path))
    print(f"[shot] {path}")


def clear_all() -> None:
    """撮った後に中身を捨てる。

    ``wm.read_homefile`` はファイルを読み直すため、手順を進めているタイマーごと止まることがある。
    データブロックを消して同じファイルのまま次の読み込みに移る。
    """
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for blocks in (bpy.data.meshes, bpy.data.armatures, bpy.data.lights, bpy.data.cameras,
                   bpy.data.materials, bpy.data.images, bpy.data.node_groups, bpy.data.actions):
        for block in list(blocks):
            blocks.remove(block, do_unlink=True)
    print("[info] 読み込んだものを片付けました")


# --------------------------------------------------------------------------- shots


def apply_view_transform(opts: dict[str, str]) -> None:
    name = opts.get("view-transform", "")
    if name:
        bpy.context.scene.view_settings.view_transform = name


def shot_steps(package: Path, out_dir: Path, kind: str, opts: dict[str, str]) -> list:
    """1 枚分の手順。重い読み込みとビューの更新を分けるため、タイマーで 1 つずつ進める。"""
    view = tuple(float(v) for v in opts.get("view", "66,46").split(","))
    zoom = float(opts.get("zoom", "1.0"))
    overlays = "no-overlays" not in opts
    full = "full" in opts
    is_scene = kind == "scene"
    shading = opts.get("shading", "RENDERED" if is_scene else "MATERIAL").upper()
    env = float(opts.get("env", "0.05" if is_scene else "0.5"))
    # RENDERED はサンプルが溜まるまで描き切らないので、撮る前に何度か手番を空ける
    settle = int(opts.get("settle", "6" if shading == "RENDERED" else "1"))

    state: dict = {}

    def prepare() -> None:
        apply_view_transform(opts)
        state["prepared"] = prepared_package(package)

    def load() -> None:
        prepared = state["prepared"]
        if is_scene:
            scene = pick_scene(prepared, opts.get("scene", ""))
            print(f"[info] シーン: {scene.pathname}（配置 {len(scene.placements)} 件）")
            import_package(package, prepared, "SCENES", opts, scene=scene)
        else:
            import_package(package, prepared, "MODELS", opts)

    def view_setup() -> None:
        setup_viewport(shading, env, overlays, scene_lights=is_scene)
        if not (is_scene and "camera" in opts and look_through_camera()):
            frame_view(view, zoom)

    def settle_step() -> None:
        view3d()[0].tag_redraw()

    return [prepare, load, view_setup] + [settle_step] * settle + [lambda: shoot(out_dir, kind, full)]


def main() -> None:
    package, out_dir, shot, opts = parse_args()
    out_dir.mkdir(parents=True, exist_ok=True)
    register_addon()

    if shot == "list":
        print_contents(prepared_package(package))
        return

    if bpy.app.background:
        raise SystemExit("撮影には GUI が必要です（-b を外し、空の .blend を渡して起動してください）")

    bpy.context.preferences.view.ui_scale = 1.25
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()

    if shot == "pile":
        steps = shot_steps(package, out_dir, "pile", opts)
    elif shot == "scene":
        steps = shot_steps(package, out_dir, "scene", opts)
    elif shot == "both":
        steps = (shot_steps(package, out_dir, "pile", opts)
                 + [clear_all]
                 + shot_steps(package, out_dir, "scene", opts))
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
            traceback.print_exc()
            bpy.ops.wm.quit_blender()
            return None
        return 0.7

    bpy.app.timers.register(tick, first_interval=2.0)


try:
    main()
except SystemExit:
    raise
except Exception:                # 失敗しても GUI を開いたまま止めない
    traceback.print_exc()

    def _quit():
        bpy.ops.wm.quit_blender()
        return None

    if not bpy.app.background:
        bpy.app.timers.register(_quit, first_interval=0.5)
