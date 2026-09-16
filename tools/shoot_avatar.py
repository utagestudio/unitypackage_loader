"""記事用に、アバターのパッケージを「素で読んだ状態」と「読み込んだ状態」で撮り分ける。

FBX をそのまま読むと、マテリアルは名前だけで色もテクスチャも付かない。同じパッケージを
このアドオンで読むと、Unity の .mat から組み直したマテリアルが付く。その差を、同じカメラ・
同じライトの 2 枚で見せるための道具（``tools/shoot_pile.py`` の 1 体版）。

表情のシェイプキーを配合して顔を並べるコマも撮れる。どのシェイプキーをどれだけ混ぜるかは
``--shapes`` で外から渡す（アセット固有の名前をこのファイルに持たせないため）。

``--factory-startup`` の Blender にリポジトリの実体だけを登録する。ビューポートではなく
レンダリングした画を書き出すので ``-b`` で動く。

    blender -b --factory-startup --python tools/shoot_avatar.py -- <package> <outdir> [shot] [--opt=値 ...]

``shot``:

    pair    素で読んだ状態（before.png）と読み込んだ状態（after.png）を同じ構図で撮る
    after   読み込んだ状態だけを撮る
    before  素で読んだ状態だけを撮る
    faces   顔に寄って、``--shapes`` の配合ごとに face_1.png … を撮る

オプション（``--key=値`` 形式）:

    --shapes=<配合>       faces のコマ。``;`` でコマを区切り、コマの中は ``名前=値`` を ``,`` で並べる。
                          空のコマは素の顔。例: ``--shapes=";笑い=0.3;目閉じ=0.5,困り眉=0.4"``
    --focus=<ボーン名>    faces で寄る先のボーン。``,`` で複数渡すとその真ん中（左右の目を指定すると
                          顔の中心になる）。省略するとメッシュの上端から推定する
    --head=<m>            faces で収める範囲の半径（既定 0.26）
    --res=<幅>x<高さ>     出力の大きさ（既定 pair: 1200x1500、faces: 1000x1000）
    --view=<X>,<Z>        視点の向き。X は水平が 0・上から見下ろすほど正（既定 8,18）
    --zoom=<倍率>         収めた後の寄り（1 より小さいほど寄る。既定 1.0）
    --lens=<mm>           レンズ（既定 85）
    --bg=<float>          背景の明るさ（既定 0.05）
    --key=<float>         キーライトの強さ（既定 600）
    --view-transform=<名前>  既定 Standard
    --samples=<N>         EEVEE のサンプル数（既定 64）
    --extract=<dir>       展開先（既定は一時ディレクトリ。同じ場所を使い回すと 2 回目が速い）

アセット固有の名前は持たない。撮影対象のパッケージは ``_local/`` に置く。
"""

from __future__ import annotations

import math
import sys
import tempfile
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
    shot = positional[2] if len(positional) > 2 else "pair"
    return Path(positional[0]), Path(positional[1]), shot, opts


def extract_root(package: Path, opts: dict[str, str]) -> str:
    if opts.get("extract"):
        path = Path(opts["extract"]).expanduser()
    else:
        path = Path(tempfile.gettempdir()) / "unitypackage_loader_shots" / package.stem
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# --------------------------------------------------------------------------- import


def import_package(package: Path, material_mode: str, opts: dict[str, str]) -> list[bpy.types.Object]:
    """読み込む単位 Models で読み、増えたオブジェクトを返す（撮り終えたら消せるように）。"""
    from unitypackage_loader.blender.importer import (
        ImportOptions,
        build_shader_table,
        prepare_package,
        run_import,
    )

    before = set(bpy.data.objects)
    prepared = prepare_package(str(package), build_shader_table(), import_blend=False)
    options = ImportOptions(
        models="ALL",
        unit="MODELS",
        material_mode=material_mode,
        extract_mode="CUSTOM",
        extract_path=extract_root(package, opts),
        max_extract_size=0,  # Preferences が無いので上限なし。手元の信頼できるパッケージにだけ使う
    )
    report = run_import(bpy.context, str(package), options, prepared=prepared)
    print(f"[import] {material_mode}: オブジェクト {len(report.objects)} / "
          f"マテリアル {len(report.materials)} / 警告 {len(report.warnings)}")
    for warning in report.warnings:
        print(f"  [warn] {warning}")
    return [ob for ob in bpy.data.objects if ob not in before]


def discard(objects: list[bpy.types.Object]) -> None:
    for ob in objects:
        bpy.data.objects.remove(ob, do_unlink=True)
    for collection in (bpy.data.meshes, bpy.data.armatures, bpy.data.materials, bpy.data.images):
        for item in list(collection):
            if item.users == 0:
                collection.remove(item)


def meshes(objects: list[bpy.types.Object]) -> list[bpy.types.Object]:
    return [ob for ob in objects if ob.type == "MESH"]


# --------------------------------------------------------------------------- camera


def bounds(objects: list[bpy.types.Object]) -> tuple[mathutils.Vector, mathutils.Vector]:
    lo = mathutils.Vector((float("inf"),) * 3)
    hi = mathutils.Vector((float("-inf"),) * 3)
    for ob in objects:
        for corner in ob.bound_box:
            world = ob.matrix_world @ mathutils.Vector(corner)
            lo = mathutils.Vector(map(min, lo, world))
            hi = mathutils.Vector(map(max, hi, world))
    return lo, hi


def camera_axes(elevation: float, azimuth: float) -> tuple[mathutils.Vector, mathutils.Vector, mathutils.Vector]:
    """視点の向き（カメラから被写体を見る方向の逆）と、その右・上を返す。"""
    el, az = math.radians(elevation), math.radians(azimuth)
    direction = mathutils.Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
    direction.normalize()
    right = direction.cross(mathutils.Vector((0, 0, 1)))
    right = right.normalized() if right.length > 1e-6 else mathutils.Vector((1, 0, 0))
    return direction, right, right.cross(direction).normalized()


def place_camera(targets: list[bpy.types.Object], center: mathutils.Vector | None,
                 opts: dict[str, str]) -> bpy.types.Object:
    """被写体が画角に収まる位置にカメラを置く（背景モードでは view_selected が使えないので自前で解く）。"""
    lens = float(opts.get("lens", "85"))
    width, height = resolution(opts)
    sensor = 36.0
    half_x = math.atan(sensor / 2 / lens)
    half_y = math.atan((sensor * height / width) / 2 / lens)

    lo, hi = bounds(targets)
    ctr = center if center is not None else (lo + hi) / 2
    direction, right, up = camera_axes(*view_angles(opts))

    corners = [mathutils.Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    distance = 0.0
    for corner in corners:
        offset = corner - ctr
        forward = offset.dot(direction)
        distance = max(distance,
                       forward + abs(offset.dot(right)) / math.tan(half_x),
                       forward + abs(offset.dot(up)) / math.tan(half_y))
    distance *= float(opts.get("zoom", "1.0"))

    data = bpy.data.cameras.new("Camera")
    data.lens = lens
    camera = bpy.data.objects.new("Camera", data)
    bpy.context.scene.collection.objects.link(camera)
    camera.location = ctr + direction * distance
    camera.rotation_mode = "QUATERNION"
    camera.rotation_quaternion = (-direction).to_track_quat("-Z", "Y")
    bpy.context.scene.camera = camera
    return camera


def view_angles(opts: dict[str, str]) -> tuple[float, float]:
    raw = opts.get("view", "8,18").split(",")
    return float(raw[0]), float(raw[1])


def resolution(opts: dict[str, str]) -> tuple[int, int]:
    raw = opts.get("res", "")
    if not raw:
        return (1000, 1000) if opts.get("_shot") == "faces" else (1200, 1500)
    width, _, height = raw.partition("x")
    return int(width), int(height)


# --------------------------------------------------------------------------- light


def build_lights(camera: bpy.types.Object, targets: list[bpy.types.Object], opts: dict[str, str]) -> None:
    """カメラを基準にキー・フィル・リムを置く。before と after で同じものを使い回す。"""
    lo, hi = bounds(targets)
    ctr = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.1)
    key = float(opts.get("key", "600"))
    direction, right, up = camera_axes(*view_angles(opts))

    for name, offset, energy, size in (
        ("Key", (-right * 1.1 + up * 0.9 + direction * 1.4), key, radius * 1.6),
        ("Fill", (right * 1.6 + up * 0.2 + direction * 1.2), key * 0.30, radius * 2.0),
        ("Rim", (right * 0.6 + up * 1.2 - direction * 1.5), key * 0.55, radius * 1.4),
    ):
        data = bpy.data.lights.new(name, "AREA")
        data.energy = energy
        data.size = size
        light = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(light)
        light.location = ctr + offset.normalized() * radius * 3.0
        light.rotation_mode = "QUATERNION"
        light.rotation_quaternion = (ctr - light.location).to_track_quat("-Z", "Y")

    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    level = float(opts.get("bg", "0.05"))
    background = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    background.inputs["Color"].default_value = (level, level, level * 1.08, 1.0)
    bpy.context.scene.world = world


# --------------------------------------------------------------------------- render


def setup_render(opts: dict[str, str]) -> None:
    scene = bpy.context.scene
    engines = [i.identifier for i in scene.render.bl_rna.properties["engine"].enum_items]
    for candidate in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        if candidate in engines:
            try:
                scene.render.engine = candidate
            except TypeError:
                continue
            break
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = int(opts.get("samples", "64"))
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = opts.get("view-transform", "Standard")
    width, height = resolution(opts)
    scene.render.resolution_x, scene.render.resolution_y = width, height
    scene.render.resolution_percentage = 100


def render_to(out_dir: Path, name: str) -> None:
    path = out_dir / f"{name}.png"
    bpy.context.scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    print(f"[shot] {path}")


# --------------------------------------------------------------------------- shapes


def parse_shapes(raw: str) -> list[dict[str, float]]:
    frames: list[dict[str, float]] = []
    for chunk in raw.split(";"):
        mix: dict[str, float] = {}
        for item in chunk.split(","):
            name, _, value = item.partition("=")
            if name.strip():
                mix[name.strip()] = float(value or 1.0)
        frames.append(mix)
    return frames


def apply_shapes(objects: list[bpy.types.Object], mix: dict[str, float]) -> None:
    missing = set(mix)
    for ob in meshes(objects):
        keys = ob.data.shape_keys
        if keys is None:
            continue
        for block in keys.key_blocks:
            block.value = 0.0
        for name, value in mix.items():
            block = keys.key_blocks.get(name)
            if block is not None:
                block.value = value
                missing.discard(name)
    for name in sorted(missing):
        print(f"  [warn] シェイプキーが見つかりません: {name}")


def focus_point(objects: list[bpy.types.Object], opts: dict[str, str]) -> mathutils.Vector:
    """顔に寄る先。``--focus`` のボーン、無ければメッシュの上端あたりを返す。"""
    wanted = [name.strip() for name in opts.get("focus", "").split(",") if name.strip()]
    if wanted:
        found = []
        for name in wanted:
            for ob in objects:
                if ob.type != "ARMATURE":
                    continue
                bone = ob.data.bones.get(name)
                if bone is not None:
                    found.append(ob.matrix_world @ bone.head_local)
                    break
            else:
                print(f"  [warn] ボーンが見つかりません: {name}")
        if found:  # 複数渡されたときは真ん中（左右の目を指定して顔の中心を取るなど）
            center = mathutils.Vector((0.0, 0.0, 0.0))
            for point in found:
                center += point
            return center / len(found)
    lo, hi = bounds(meshes(objects))
    return mathutils.Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, hi.z - (hi.z - lo.z) * 0.08))


def head_targets(objects: list[bpy.types.Object], center: mathutils.Vector,
                 opts: dict[str, str]) -> list[bpy.types.Object]:
    """顔だけを収めるための当たり。頭のまわりの立方体を作って被写体の代わりにする。"""
    size = float(opts.get("head", "0.26"))
    mesh = bpy.data.meshes.new("FocusBox")
    mesh.from_pydata([(x, y, z) for x in (-size, size) for y in (-size, size) for z in (-size, size)], [], [])
    box = bpy.data.objects.new("FocusBox", mesh)
    bpy.context.scene.collection.objects.link(box)
    box.location = center
    bpy.context.view_layer.update()
    return [box]


# --------------------------------------------------------------------------- main


def main() -> None:
    package, out_dir, shot, opts = parse_args()
    opts["_shot"] = shot
    out_dir.mkdir(parents=True, exist_ok=True)
    register_addon()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    setup_render(opts)

    if shot == "faces":
        objects = import_package(package, opts.get("materials", "AUTO").upper(), opts)
        center = focus_point(objects, opts)
        box = head_targets(objects, center, opts)
        camera = place_camera(box, center, opts)
        build_lights(camera, meshes(objects), opts)
        bpy.data.objects.remove(box[0], do_unlink=True)
        for index, mix in enumerate(parse_shapes(opts.get("shapes", "")), start=1):
            apply_shapes(objects, mix)
            label = ", ".join(f"{k}={v}" for k, v in mix.items()) or "素"
            print(f"[face {index}] {label}")
            render_to(out_dir, f"face_{index}")
        return

    order = {"pair": ("AUTO", "NAMES_ONLY"), "after": ("AUTO",), "before": ("NAMES_ONLY",)}[shot]
    names = {"AUTO": "after", "NAMES_ONLY": "before"}
    camera = None
    for mode in order:
        objects = import_package(package, mode, opts)
        if camera is None:  # 構図とライトは最初の 1 回だけ作り、2 枚目もそのまま使う
            camera = place_camera(meshes(objects), None, opts)
            build_lights(camera, meshes(objects), opts)
        render_to(out_dir, names[mode])
        if mode != order[-1]:
            discard(objects)


if __name__ == "__main__":
    main()
