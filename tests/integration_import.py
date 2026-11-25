"""Blender 上で実際にインポートし、期待値 JSON と照合する統合テスト。

実行例::

    blender -b --python tests/integration_import.py -- [package.unitypackage] [expectations.json]

引数を省略すると ``_local/unitypackages/`` の最初の .unitypackage と ``_local/expectations.json`` を使う。
期待値ファイルの書式は ``tests/expectations.schema.md`` を参照。合成パッケージの期待値をまとめて回すときは
``tests/run_integration.py`` を使う。

期待値のキーごとに確認する関数を分け、``CHECKS`` の順に呼ぶ（#199）。読み込みの後にオペレーターを呼ぶ ``steps`` では、
各手順の ``checks`` を同じ関数で確かめる。
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._paths import LOCAL_DIR, PACKAGES_DIR, find_package, local_packages  # noqa: E402

# 失敗したときに片付けられているかを数えるデータの種類
_DATA_KINDS = ("objects", "collections", "meshes", "materials", "images", "armatures", "actions", "lights", "cameras")


def _args() -> tuple[Path, Path]:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    expectations = Path(argv[1]) if len(argv) > 1 else LOCAL_DIR / "expectations.json"
    if not expectations.is_file():
        raise SystemExit(f"expectations file not found: {expectations}")
    if argv:
        package = Path(argv[0])
    else:
        # 期待値ファイルの "package" キー（ファイル名）→ 無ければ最初のパッケージ
        named = json.loads(expectations.read_text("utf-8")).get("package")
        package = find_package(named) if named else next(iter(local_packages()), None)
        if package is None:
            raise SystemExit(f"package not found in {PACKAGES_DIR}: {named or '*.unitypackage'}")
    if not package.is_file():
        raise SystemExit(f"package not found: {package}")
    return package, expectations


def _register_from_repo() -> None:
    """symlink 先ではなくリポジトリの実体をそのまま登録する。"""
    import unitypackage_loader

    unitypackage_loader.register()


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def eq(self, label: str, actual, expected) -> None:
        if actual == expected:
            self.passed += 1
        else:
            self.failures.append(f"{label}: expected {expected!r}, got {actual!r}")

    def le(self, label: str, actual, limit) -> None:
        if actual <= limit:
            self.passed += 1
        else:
            self.failures.append(f"{label}: expected <= {limit!r}, got {actual!r}")

    def true(self, label: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
        else:
            self.failures.append(f"{label}: {detail or 'condition failed'}")

    def close(self, label: str, actual: float, expected: float, tolerance: float) -> None:
        self.true(label, abs(actual - expected) <= tolerance, f"expected {expected}, got {actual}")

    def near(self, label: str, actual: Vector, expected, tolerance: float = 1e-3) -> None:
        self.true(label, (actual - Vector(expected)).length <= tolerance,
                  f"expected {expected}, got {[round(v, 4) for v in actual]}")


@dataclass
class Context:
    """確認する関数に渡すもの。``exp`` はいま確かめている期待値（``steps`` の中では手順の ``checks``）。"""

    c: Check
    exp: dict
    report: object  # core.report.ImportReport
    package: Path


# ---------------------------------------------------------------------------
# 探すためのヘルパー
# ---------------------------------------------------------------------------


def _strip_suffix(name: str) -> str:
    base, dot, suffix = name.rpartition(".")
    return base if dot and suffix.isdigit() and len(suffix) == 3 else name


def _find_placed(top: str, name: str, parent: str | None = None, node_only: bool = False) -> list:
    found = []
    for obj in bpy.data.objects:
        # 名前は連番を除いたオブジェクト名か、FBX のノードとしての名前（ノード名がもともと連番の形のとき。#237）。
        # node_only ならノードとしての名前だけで探す（連番を外すと同じ名前になるノードを区別する）
        node = obj.get("unity_fbx_node")
        if node != name and (node_only or _strip_suffix(obj.name) != name):
            continue
        if parent is not None and (obj.parent is None or _strip_suffix(obj.parent.name) != parent):
            continue
        root = obj
        while root.parent is not None:
            root = root.parent
        if _strip_suffix(root.name) == top:
            found.append(obj)
    return found


def _tip_world(obj) -> Vector:
    """評価後（アーマチュア変形後）のメッシュで、重心から最も遠い頂点のワールド座標。"""
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        points = [v.co.copy() for v in mesh.vertices]
        centroid = sum(points, Vector()) / len(points)
        tip = max(points, key=lambda p: (p - centroid).length_squared)
        return evaluated.matrix_world @ tip
    finally:
        evaluated.to_mesh_clear()


def _image_of(mat: bpy.types.Material, label: str):
    for node in mat.node_tree.nodes:
        if node.bl_idname == "ShaderNodeTexImage" and node.label == label:
            return node.image
    return None


def _principled(mat: bpy.types.Material):
    return next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


def _mat_files(materials) -> list[str]:
    """マテリアルの元の .mat のファイル名（unity_material_path の末尾）。読み直すと名前に連番が付くので、こちらで比べる。"""
    return sorted({m.get("unity_material_path", "").rsplit("/", 1)[-1] for m in materials if m is not None})


# ---------------------------------------------------------------------------
# 期待値のキーごとの確認
# ---------------------------------------------------------------------------


def check_result_state(ctx: Context) -> None:
    """片付け（``leaves_nothing``）と、成否によらず元に戻っているはずのもの。"""
    c, exp, report = ctx.c, ctx.exp, ctx.report
    if "errors" in exp:
        c.eq("error count", len(report.errors), exp["errors"])
    # 読み込み中だけビューレイヤーから外すパッケージのコレクションと、作業用のコレクション（#75）は、成否によらず元に戻っている
    c.eq("staging collections left", [x.name for x in bpy.data.collections if x.name.endswith("(importing)")], [])
    excluded: list[str] = []
    pending = list(bpy.context.view_layer.layer_collection.children)
    while pending:
        layer = pending.pop()
        if layer.exclude:
            excluded.append(layer.name)
        pending.extend(layer.children)
    c.eq("excluded layer collections", excluded, [])
    # マテリアルのノードの組み立ては、失敗しても警告にして続ける。期待値に書いていなくても失敗は見逃さない
    c.eq("node build failures", [w for w in report.warnings if "node build failed" in w], [])


def check_objects(ctx: Context) -> None:
    c, objs = ctx.c, ctx.exp.get("objects", {})
    if "count" in objs:
        c.eq("object count", len(ctx.report.objects), objs["count"])
    if "mesh_count" in objs:
        c.eq("mesh count", sum(1 for o in bpy.data.objects if o.type == "MESH"), objs["mesh_count"])
    if "armature_count" in objs:
        c.eq("armature count", sum(1 for o in bpy.data.objects if o.type == "ARMATURE"), objs["armature_count"])
        # 読み込んだアーマチュアは線で表示する（#260）
        displays = {o.display_type for o in bpy.data.objects if o.type == "ARMATURE"}
        c.true("armatures are displayed as wire", displays <= {"WIRE"})


def check_materials(ctx: Context) -> None:
    c, report, mats = ctx.c, ctx.report, ctx.exp.get("materials", {})
    if "count" in mats:
        c.eq("material count", len(report.materials), mats["count"])
    if "mapped" in mats:
        c.eq("mapped materials", report.mapped_count, mats["mapped"])
    if "families" in mats:
        c.eq("shader families", sorted({m.family for m in report.materials}), sorted(mats["families"]))
    if "methods" in mats:
        c.eq("mapping methods", sorted({m.method for m in report.materials}), sorted(mats["methods"]))
    if "modes" in mats:
        c.eq("material modes", sorted({m.mode for m in report.materials if m.mode}), sorted(mats["modes"]))


def check_images(ctx: Context) -> None:
    c, images = ctx.c, ctx.exp.get("images", {})
    if "count" in images:
        c.eq("image count", len(ctx.report.images), images["count"])
    if "missing" in images:
        # ファイルの見つからない画像（モデルの中のパスが作者の PC のものなど。#220）
        missing = [
            i.name for i in bpy.data.images
            if i.source == "FILE" and not i.packed_file and i.filepath and not Path(bpy.path.abspath(i.filepath)).is_file()
        ]
        c.eq("images with missing files", len(missing), images["missing"])
    if "packed" in images:
        # レポートに載った画像（このアドオンが読み込んだもの）がすべて .blend に取り込まれているか
        unpacked = [n for n in ctx.report.images if bpy.data.images.get(n) is None or bpy.data.images[n].packed_file is None]
        c.eq("images not packed", unpacked if images["packed"] else [], [])


def check_shadows_only(ctx: Context) -> None:
    """Shadows Only の Renderer（#232）: カメラなどから見えず、影だけを落とすオブジェクト（名前の連番は外して比べる）。"""
    if "shadows_only" not in ctx.exp:
        return
    from unitypackage_loader.core.meta import strip_numeric_suffix

    c = ctx.c
    found = sorted({
        strip_numeric_suffix(o.name) for o in bpy.data.objects
        if o.get("unity_shadows_only") and not o.visible_camera and o.visible_shadow
    })
    c.eq("shadows-only objects", found, sorted(ctx.exp["shadows_only"]))


def check_warnings(ctx: Context) -> None:
    c, exp, report = ctx.c, ctx.exp, ctx.report
    if "warnings_max" in exp:
        c.le("warning count", len(report.warnings), exp["warnings_max"])
    for text in exp.get("warnings_contain", []):
        c.true(f"warning contains {text!r}", any(text in w for w in report.warnings))
    for text in exp.get("warnings_exclude", []):
        c.eq(f"warnings without {text!r}", [w for w in report.warnings if text in w], [])
    if "split_slots" in exp:
        c.eq("split slots", report.split_slots, exp["split_slots"])


def _find_mesh(spec: str):
    """オブジェクト名か、"コレクション/FBX のノード名"（読み込む単位 Prefabs では、prefab の GameObject の Empty が
    同じ名前を取るので、メッシュには連番が付く。#240）で引く。"""
    if "/" not in spec:
        return bpy.data.objects.get(spec)
    coll_name, node = spec.split("/", 1)
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        return None
    return next(
        (o for o in coll.all_objects if o.type == "MESH" and (o.get("unity_fbx_node") or o.name) == node), None
    )


def check_submesh_materials(ctx: Context) -> None:
    """ポリゴンで最初に使われた順（Unity のサブメッシュ順）に並べたマテリアル名。スロットの並びに依らず、
    どのポリゴン群にどのマテリアルが付いたかを確かめる。"""
    c = ctx.c
    for obj_name, names in ctx.exp.get("submesh_materials", {}).items():
        obj = _find_mesh(obj_name)
        c.true(f"object {obj_name} exists", obj is not None)
        if obj is None:
            continue
        seen: list[int] = []
        for polygon in obj.data.polygons:
            if polygon.material_index not in seen:
                seen.append(polygon.material_index)
        slots = obj.material_slots
        actual = [slots[i].material.name if i < len(slots) and slots[i].material else "" for i in seen]
        c.eq(f"{obj_name}.submesh_materials", actual, names)


def _check_bsdf_inputs(c: Check, name: str, mat, expected: dict) -> None:
    """Principled の入力の値（つながっていれば "linked"。#117）。"""
    bsdf = _principled(mat)
    for key, value in expected.items():
        sock = bsdf.inputs[key] if bsdf is not None else None
        if sock is None:
            actual = None
        elif sock.is_linked:
            actual = "linked"
        elif hasattr(sock.default_value, "__len__"):
            actual = [round(v, 4) for v in sock.default_value[:3]]
        else:
            actual = round(sock.default_value, 4)
        c.eq(f"{name}.bsdf.{key}", actual, value)


def check_material_checks(ctx: Context) -> None:
    c, report = ctx.c, ctx.report
    for spec in ctx.exp.get("material_checks", []):
        name = spec["name"]
        mat = bpy.data.materials.get(name)
        if spec.get("absent"):  # 作られていない（同名のマテリアルを使ったときの連番付きの名前など）
            c.true(f"material {name} absent", mat is None)
            continue
        c.true(f"material {name} exists", mat is not None)
        if mat is None:
            continue
        for key, value in spec.get("props", {}).items():  # カスタムプロパティの値（無ければ None）
            c.eq(f"{name}[{key!r}]", mat.get(key), value)
        if "alpha_mode" in spec:
            c.eq(f"{name}.alpha_mode", mat.get("unity_alpha_mode"), spec["alpha_mode"])
        if "render_method" in spec:
            c.eq(f"{name}.surface_render_method", mat.surface_render_method, spec["render_method"])
        if "backface_culling" in spec:
            c.eq(f"{name}.use_backface_culling", mat.use_backface_culling, spec["backface_culling"])
        if "alpha_linked" in spec:  # 出力につながる BSDF の Alpha に何かがつながっているか（テクスチャのアルファで透けるか）
            bsdf = _principled(mat)
            c.eq(f"{name}.alpha_linked", bool(bsdf and bsdf.inputs["Alpha"].is_linked), spec["alpha_linked"])
        if "mode" in spec:
            rep = next((m for m in report.materials if m.blender_name == name), None)
            c.eq(f"{name}.mode", rep.mode if rep else None, spec["mode"])
        if "method" in spec:
            rep = next((m for m in report.materials if m.blender_name == name), None)
            c.eq(f"{name}.method", rep.method if rep else None, spec["method"])
        if "shader_name" in spec:
            c.eq(f"{name}.shader_name", mat.get("unity_shader_name"), spec["shader_name"])
        if "has_unity_data" in spec:  # Unity の情報（unity_normalized など）をカスタムプロパティに残したか
            c.eq(f"{name}.has_unity_data", "unity_normalized" in mat, spec["has_unity_data"])
        for role, label in (("base", "Base Color"), ("normal", "Normal")):
            key = f"{role}_image"
            if key in spec:
                image = _image_of(mat, label) or (_image_of(mat, f"{label} (unused)") if role == "normal" else None)
                c.eq(f"{name}.{key}", image.name if image else None, spec[key])
                if image is not None and f"{role}_colorspace" in spec:
                    c.eq(f"{name}.{role}_colorspace", image.colorspace_settings.name, spec[f"{role}_colorspace"])
        if "emission_strength" in spec or "emission_color" in spec:
            bsdf = _principled(mat)
            c.true(f"{name} has Principled BSDF", bsdf is not None)
            if bsdf is not None and "emission_strength" in spec:
                c.eq(f"{name}.emission_strength", round(bsdf.inputs["Emission Strength"].default_value, 4), spec["emission_strength"])
            if bsdf is not None and "emission_color" in spec:
                actual = [round(v, 4) for v in bsdf.inputs["Emission Color"].default_value[:3]]
                c.eq(f"{name}.emission_color", actual, spec["emission_color"])
        if "smoothness_scale" in spec:
            # Roughness = 1 - Smoothness の元に掛けた倍率（倍率 1 なら掛け算のノードは無い。#112）
            nodes = mat.node_tree.nodes
            inverted = any(n.type == "MATH" and n.label.startswith("Roughness = 1 - Smoothness") for n in nodes)
            scale = next((n.inputs[1].default_value for n in nodes if n.type == "MATH" and n.label.startswith("Smoothness ×")), 1.0)
            c.eq(f"{name}.smoothness_scale", round(scale, 4) if inverted else None, spec["smoothness_scale"])
        if "bsdf_inputs" in spec:
            _check_bsdf_inputs(c, name, mat, spec["bsdf_inputs"])
        for key, value in spec.get("normalized", {}).items():
            # 中間表現（unity_normalized）の値。色は 4 桁に丸めて比べる（#118）
            actual = json.loads(mat.get("unity_normalized", "{}")).get(key)
            if isinstance(actual, list):
                actual = [round(v, 4) if isinstance(v, float) else v for v in actual]
            c.eq(f"{name}.normalized.{key}", actual, value)
        node_types = {n.bl_idname for n in mat.node_tree.nodes} if mat.node_tree else set()
        if "node_types" in spec:
            c.true(f"{name}.node_types", set(spec["node_types"]) <= node_types, f"missing {set(spec['node_types']) - node_types}")
        if "node_types_absent" in spec:
            c.eq(f"{name}.node_types_absent", sorted(set(spec["node_types_absent"]) & node_types), [])
        if "node_groups" in spec:  # ノードグループのノードが使うグループの名前（Toon の UnityToon など）
            groups = sorted({n.node_tree.name for n in mat.node_tree.nodes if n.type == "GROUP" and n.node_tree} if mat.node_tree else set())
            c.eq(f"{name}.node_groups", groups, sorted(spec["node_groups"]))


def check_outlines(ctx: Context) -> None:
    """アウトライン（Solidify。``outlines`` オプションと、アウトラインのオペレーター）。

    ``count`` はレポートのアウトラインの数、``objects`` はオブジェクト名 → Solidify の厚さ（付いていなければ null）。
    """
    c, spec = ctx.c, ctx.exp.get("outlines")
    if spec is None:
        return
    if "count" in spec:
        c.eq("report outlines", ctx.report.outlines, spec["count"])
    for obj_name, thickness in spec.get("objects", {}).items():
        obj = bpy.data.objects.get(obj_name)
        c.true(f"object {obj_name} exists", obj is not None)
        if obj is None:
            continue
        mod = obj.modifiers.get("UnityOutline")
        if thickness is None:
            c.true(f"{obj_name} has no outline", mod is None)
            continue
        c.true(f"{obj_name} has an outline", mod is not None and mod.type == "SOLIDIFY")
        if mod is not None:
            c.close(f"{obj_name}.outline thickness", mod.thickness, thickness, 1e-5)


def check_prefabs(ctx: Context) -> None:
    if "count" in ctx.exp.get("prefabs", {}):
        ctx.c.eq("prefab count", len(ctx.report.prefabs), ctx.exp["prefabs"]["count"])


def check_collections(ctx: Context) -> None:
    """読み込む単位 Prefabs で prefab ごとに作られるコレクション。マテリアルは Blender 上の名前ではなく、
    元の .mat のファイル名（unity_material_path の末尾）で比べる（同じモデルを読み直すと名前に連番が付くため）。"""
    c = ctx.c
    for coll_name, spec in ctx.exp.get("collections", {}).items():
        coll = bpy.data.collections.get(coll_name)
        c.true(f"collection {coll_name} exists", coll is not None)
        if coll is None:
            continue
        objects = list(coll.all_objects)
        if "objects" in spec:
            # Prefabs 単位も prefab の中の GameObject を Empty で再現するので、その Empty（unity_game_object）は数えない（#240）
            placed = [o for o in objects if not (o.type == "EMPTY" and "unity_game_object" in o)]
            c.eq(f"{coll_name}.objects", len(placed), spec["objects"])
        if "prefab" in spec:
            c.eq(f"{coll_name}.unity_prefab", coll.get("unity_prefab"), spec["prefab"])
        if "mat_files" in spec:
            mats = [slot.material for obj in objects if obj.type == "MESH" for slot in obj.material_slots]
            c.eq(f"{coll_name}.mat_files", _mat_files(mats), sorted(spec["mat_files"]))
        # コレクションの中の個々のオブジェクト（非表示か、LODGroup の段）
        for obj_name, part in spec.get("parts", {}).items():
            obj = next((o for o in objects if o.name == obj_name and "unity_game_object" not in o), None) or next(
                (o for o in objects if o.get("unity_fbx_node") == obj_name), None
            )
            if part.get("absent"):  # 読み込まないもの（使われていない部品。#152）
                c.true(f"{coll_name}/{obj_name} absent", obj is None)
                continue
            c.true(f"{coll_name}/{obj_name} exists", obj is not None)
            if obj is None:
                continue
            if "hidden" in part:
                c.eq(f"{coll_name}/{obj_name}.hidden", obj.hide_get(), part["hidden"])
            if "lod" in part:
                c.eq(f"{coll_name}/{obj_name}.lod", obj.get("unity_lod"), part["lod"])


def check_prefab_overlap(ctx: Context) -> None:
    if "prefab_collections_overlap" not in ctx.exp:
        return
    bpy.context.view_layer.update()
    boxes = []
    for coll in bpy.data.collections:
        if coll.get("unity_prefab") is None:
            continue
        xs, ys = [], []
        for obj in coll.all_objects:
            if obj.type != "MESH":
                continue
            for corner in obj.bound_box:
                p = obj.matrix_world @ Vector(corner)
                xs.append(p.x)
                ys.append(p.y)
        if xs:
            boxes.append((coll.name, min(xs), max(xs), min(ys), max(ys)))
    overlaps = [
        (a[0], b[0])
        for i, a in enumerate(boxes)
        for b in boxes[i + 1 :]
        if a[1] < b[2] and b[1] < a[2] and a[3] < b[4] and b[3] < a[4]
    ]
    ctx.c.eq("prefab collections overlap", bool(overlaps), ctx.exp["prefab_collections_overlap"])


def check_scenes(ctx: Context) -> None:
    if "package_collection" in ctx.exp:  # パッケージのコレクションの名前（入れ子のパッケージは「外側 (中身)」。#217）
        ctx.c.true("package collection", ctx.exp["package_collection"] in bpy.data.collections,
                   f"collections: {[c.name for c in bpy.data.collections]}")
    if "count" in ctx.exp.get("scenes", {}):
        ctx.c.eq("scene count", len(ctx.report.scenes), ctx.exp["scenes"]["count"])


def check_placed_objects(ctx: Context) -> None:
    """読み込む単位 Scenes で配置したオブジェクト。複製すると名前に連番が付くので、最上位の Empty（Unity の最上位の
    GameObject）の名前とオブジェクト名（連番を除く）で探す。parent があれば直接の親の Empty の名前でも絞る。
    tip は重心から最も遠い頂点のワールド座標。"""
    c = ctx.c
    for spec in ctx.exp.get("placed_objects", []):
        label = "/".join(filter(None, (spec["top"], spec.get("parent"), spec["object"])))
        found = _find_placed(spec["top"], spec["object"], spec.get("parent"), spec.get("node_only", False))
        if spec.get("absent"):  # 読み込まないもの（使われていない部品。#152）
            c.eq(f"{label} found", len(found), 0)
            continue
        c.eq(f"{label} found", len(found), 1)
        if len(found) != 1:
            continue
        obj = found[0]
        if "tip" in spec:
            c.near(f"{label}.tip", _tip_world(obj), spec["tip"])
        if "bbox" in spec:  # ワールドの範囲 [x0, y0, z0, x1, y1, z1]（頂点が 1 つに決まらない組み込みメッシュなど。#190）
            bpy.context.view_layer.update()
            corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
            box = [min(v[i] for v in corners) for i in range(3)] + [max(v[i] for v in corners) for i in range(3)]
            c.true(f"{label}.bbox", all(abs(a - b) <= 1e-3 for a, b in zip(box, spec["bbox"])),
                   f"expected {spec['bbox']}, got {[round(v, 4) for v in box]}")
        if "normal" in spec:  # 最初の面のワールドでの法線（表の向き）
            c.near(f"{label}.normal", (obj.matrix_world.to_3x3() @ obj.data.polygons[0].normal).normalized(), spec["normal"])
        if "hidden" in spec:
            c.eq(f"{label}.hidden", obj.hide_get(), spec["hidden"])
        if "vertices" in spec:  # メッシュの頂点数と面数（Terrain の間引きと穴。#205）
            c.eq(f"{label}.vertices", len(obj.data.vertices), spec["vertices"])
        if "faces" in spec:
            c.eq(f"{label}.faces", len(obj.data.polygons), spec["faces"])
        if "lod" in spec:
            c.eq(f"{label}.lod", obj.get("unity_lod"), spec["lod"])
        if "mat_files" in spec:
            c.eq(f"{label}.mat_files", _mat_files(s.material for s in obj.material_slots), sorted(spec["mat_files"]))


_LIGHT_ATTRS = ("type", "sensor_fit", "shape", "use_shadow", "use_custom_distance", "use_temperature")
_LIGHT_NUMBERS = ("energy", "lens", "ortho_scale", "clip_end", "cutoff_distance", "temperature", "spot_size", "spot_blend", "size", "size_y")


def check_lights_and_cameras(ctx: Context) -> None:
    """シーンのライト・カメラ（#49）。direction はオブジェクトの -Z（ライト・カメラの向き）のワールドでの向き。"""
    c, exp = ctx.c, ctx.exp
    if not (exp.get("lights") or exp.get("cameras")):
        return
    bpy.context.view_layer.update()
    for key, kind in (("lights", "LIGHT"), ("cameras", "CAMERA")):
        for spec in exp.get(key, []):
            obj = bpy.data.objects.get(spec["name"])
            label = f"{kind.lower()} {spec['name']}"
            c.true(f"{label} exists", obj is not None and obj.type == kind)
            if obj is None or obj.type != kind:
                continue
            for attr in _LIGHT_ATTRS:
                if attr in spec:
                    c.eq(f"{label}.{attr}", getattr(obj.data, attr), spec[attr])
            for attr in _LIGHT_NUMBERS:
                if attr in spec:
                    c.close(f"{label}.{attr}", getattr(obj.data, attr), spec[attr], 1e-3 * max(1.0, abs(spec[attr])))
            if "direction" in spec:
                c.near(f"{label}.direction", (obj.matrix_world.to_3x3() @ Vector((0, 0, -1))).normalized(), spec["direction"])
            if "location" in spec:
                c.near(f"{label}.location", obj.matrix_world.translation, spec["location"])
            if "hidden" in spec:
                c.eq(f"{label}.hidden", obj.hide_get(), spec["hidden"])
            if "light_scale" in spec:  # HDRP の露出の分としてライトに掛けた倍率（#130）
                c.close(f"{label}.light_scale", obj.get("unity_light_scale", 1.0), spec["light_scale"], 1e-3 * spec["light_scale"])
            # 強さの換算に使ったパイプラインと、その根拠（#120）
            for attr, prop in (("pipeline", "unity_render_pipeline"), ("pipeline_source", "unity_render_pipeline_source")):
                if attr in spec:
                    c.eq(f"{label}.{attr}", obj.get(prop), spec[attr])


def check_dialog_selected(ctx: Context) -> None:
    """選択ダイアログの候補で、既定でチェックが入るもの（#134: Scenes は最初の読み込めるシーンだけ）。"""
    if "dialog_selected" not in ctx.exp:
        return
    from unitypackage_loader.blender import importer
    from unitypackage_loader.operators import select_models

    prepared = importer.prepare_package(str(ctx.package))
    items = select_models.dialog_items(bpy.context)
    items.clear()
    select_models.add_prefab_and_model_items(items, prepared)
    select_models.add_scene_items(items, prepared)
    for kind, expected in ctx.exp["dialog_selected"].items():
        actual = sorted(it.pathname for it in items if it.kind == kind and it.selected)
        ctx.c.eq(f"dialog selected {kind}", actual, sorted(expected))
    items.clear()


def check_scene_settings(ctx: Context) -> None:
    c, exp, report = ctx.c, ctx.exp, ctx.report
    if "scene_exposure" in exp:
        # シーンの露出は変えない（#130。HDRP の露出はライトの強さに掛ける）
        c.close("scene exposure", bpy.context.scene.view_settings.exposure, exp["scene_exposure"], 1e-3)
    if "scene_camera" in exp:
        camera = bpy.context.scene.camera
        c.eq("scene camera", camera.name if camera else None, exp["scene_camera"])
    if "light_count" in exp:
        c.eq("report lights", report.lights, exp["light_count"])
    if "camera_count" in exp:
        c.eq("report cameras", report.cameras, exp["camera_count"])
    if "decal_count" in exp:
        c.eq("report decals", report.decals, exp["decal_count"])
    if "terrain_count" in exp:
        c.eq("report terrains", report.terrains, exp["terrain_count"])


def check_shared_meshes(ctx: Context) -> None:
    c = ctx.c
    for spec in ctx.exp.get("shared_meshes", []):
        found = [_find_placed(o["top"], o["object"]) for o in spec["objects"]]
        labels = "+".join(f"{o['top']}/{o['object']}" for o in spec["objects"])
        if all(len(f) == 1 for f in found):
            shared = len({f[0].data.name for f in found}) == 1
            c.eq(f"{labels} share mesh data", shared, spec["shared"])
        else:
            c.true(f"{labels} found", False, "objects not found")


def check_shape_keys(ctx: Context) -> None:
    c = ctx.c
    for obj_name, count in ctx.exp.get("shape_keys", {}).items():
        obj = bpy.data.objects.get(obj_name)
        c.true(f"object {obj_name} exists", obj is not None)
        if obj is not None:
            actual = len(obj.data.shape_keys.key_blocks) if obj.data.shape_keys else 0
            c.eq(f"{obj_name}.shape_keys", actual, count)


def _clip_datablocks(obj) -> list:
    """オブジェクトと、そのシェイプキー（クリップの NLA トラックを持つもの）。"""
    result = [obj]
    if obj.type == "MESH" and obj.data is not None and obj.data.shape_keys is not None:
        result.append(obj.data.shape_keys)
    return result


def _clip_tracks(obj) -> list[str]:
    return sorted(t.name for d in _clip_datablocks(obj) if d.animation_data for t in d.animation_data.nla_tracks)


def _at_clip_time(objects, track: str, seconds: float):
    """``objects`` の ``track`` の NLA トラックだけを有効にして ``seconds`` 秒のフレームにする（戻すときは同じ関数を None で呼ぶ）。"""
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    for obj in objects:
        for d in _clip_datablocks(obj):
            if d.animation_data:
                for t in d.animation_data.nla_tracks:
                    t.mute = t.name != track if seconds is not None else True
    # Python からミュートを切り替えても依存関係は組み直されず、評価済みのトラックは切り替わらない（NLA エディターの操作では
    # 組み直される）。コレクションを付け外しして組み直させる
    temp = bpy.data.collections.new("_clip_check")
    scene.collection.children.link(temp)
    scene.collection.children.unlink(temp)
    bpy.data.collections.remove(temp)
    scene.frame_set(scene.frame_start + round((seconds or 0) * fps))
    bpy.context.view_layer.update()


def _world(obj) -> Matrix:
    return obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()


def check_clips(ctx: Context) -> None:
    """パッケージの .anim から作った Action と NLA トラック（#209）。"""
    c, spec = ctx.c, ctx.exp.get("clips")
    if spec is None:
        return
    if "count" in spec:
        c.eq("clip count", len(ctx.report.clips), spec["count"])
    if "actions" in spec:
        c.eq("actions", sorted(a.name for a in bpy.data.actions if a.get("unity_clip_guid")), sorted(spec["actions"]))
    for text in spec.get("notes_contain", []):
        c.true(f"clip note contains {text!r}", any(text in n for n in ctx.report.clip_notes), f"notes: {ctx.report.clip_notes}")
    for name, count in spec.get("instances", {}).items():  # 同じ名前のオブジェクトの数（シーンに同じモデルを複数置いたとき）
        c.eq(f"objects named {name}", len([o for o in bpy.data.objects if _strip_suffix(o.name) == name]), count)
    for name, tracks in spec.get("tracks", {}).items():
        objects = [o for o in bpy.data.objects if _strip_suffix(o.name) == name]
        c.true(f"objects named {name} exist", bool(objects))
        for obj in objects:
            c.eq(f"{obj.name} NLA tracks", _clip_tracks(obj), sorted(tracks))
            for d in _clip_datablocks(obj):
                if d.animation_data:
                    c.true(f"{obj.name} NLA tracks are muted", all(t.mute for t in d.animation_data.nla_tracks))
    for value in spec.get("values", []):
        objects = [o for o in bpy.data.objects if _strip_suffix(o.name) == value["object"]]
        if not objects:
            c.true(f"object {value['object']} exists", False)
            continue
        label = f"{value['track']} {value['object']}"
        for obj in objects:
            if "shape_key" in value:
                _at_clip_time([obj], value["track"], value["seconds"])
                key = obj.data.shape_keys.evaluated_get(bpy.context.evaluated_depsgraph_get())
                actual = key.key_blocks[value["shape_key"]].value
                c.close(f"{label} {value['shape_key']} at {value['seconds']}s", actual, value["value"], 1e-3)
            if "hidden" in value:
                _at_clip_time([obj], value["track"], value["seconds"])
                evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                c.eq(f"{label} hidden at {value['seconds']}s", (evaluated.hide_viewport, evaluated.hide_render), (value["hidden"],) * 2)
            if "distance_to" in value:
                other = min((o for o in bpy.data.objects if _strip_suffix(o.name) == value["distance_to"]),
                            key=lambda o: (o.matrix_world.translation - obj.matrix_world.translation).length)
                _at_clip_time([obj, other], value["track"], value["seconds"])
                distance = (_world(obj).translation - _world(other).translation).length
                c.close(f"{label} distance to {other.name} at {value['seconds']}s", distance, value["distance"], 1e-3)
            if "rotation_delta" in value:
                delta = value["rotation_delta"]
                _at_clip_time([obj], value["track"], delta["from"])
                before = _world(obj).to_3x3().normalized()
                _at_clip_time([obj], value["track"], delta["to"])
                after = _world(obj).to_3x3().normalized()
                expected = Matrix.Rotation(math.radians(delta["degrees"]), 3, delta["axis"])
                error = max(abs(a - b) for row_a, row_b in zip(after @ before.inverted(), expected) for a, b in zip(row_a, row_b))
                c.true(f"{label} world rotation from {delta['from']}s to {delta['to']}s", error < 1e-3,
                       f"expected {delta['degrees']} deg about {delta['axis']}, got {[[round(v, 3) for v in r] for r in after @ before.inverted()]}")
    _at_clip_time(list(bpy.data.objects), "", None)


# 読み込みの後に、この順で確かめる。``steps`` の手順の ``checks`` も同じ表で確かめる（片付けの確認を除く）
CHECKS: list[Callable[[Context], None]] = [
    check_objects,
    check_materials,
    check_images,
    check_shadows_only,
    check_warnings,
    check_submesh_materials,
    check_material_checks,
    check_outlines,
    check_prefabs,
    check_collections,
    check_prefab_overlap,
    check_scenes,
    check_placed_objects,
    check_lights_and_cameras,
    check_dialog_selected,
    check_scene_settings,
    check_shared_meshes,
    check_shape_keys,
    check_clips,
]


# ---------------------------------------------------------------------------
# 読み込みの前後の操作
# ---------------------------------------------------------------------------


def prepare_before(exp: dict) -> None:
    """読み込む前の .blend の状態を作る（``before``）。``materials`` に挙げた名前の空のマテリアルを作る
    （同名のマテリアルを使う ``reuse_existing`` の確認に使う）。"""
    for name in exp.get("before", {}).get("materials", []):
        mat = bpy.data.materials.new(name)
        mat["test_preexisting"] = True


def run_steps(ctx: Context) -> None:
    """読み込みの後にオペレーターを呼び、手順ごとの ``checks`` を確かめる（``steps``）。

    手順は ``operator``（``unitypkg.rebuild_material`` のような名前）、``select``（選ぶオブジェクト名。最初のものを
    アクティブにする）、``properties``、``result``（既定は FINISHED）、``checks``（``CHECKS`` と同じ書式）。
    """
    for index, step in enumerate(ctx.exp.get("steps", [])):
        label = f"step {index + 1} ({step['operator']})"
        names = step.get("select", [])
        objects = [bpy.data.objects.get(n) for n in names]
        ctx.c.true(f"{label}: objects exist", all(o is not None for o in objects), f"missing {names}")
        for obj in bpy.context.view_layer.objects:
            obj.select_set(False)
        for obj in objects:
            if obj is not None:
                obj.hide_set(False)
                obj.select_set(True)
        if objects and objects[0] is not None:
            bpy.context.view_layer.objects.active = objects[0]
        module, name = step["operator"].split(".")
        try:
            result = getattr(getattr(bpy.ops, module), name)("EXEC_DEFAULT", **step.get("properties", {}))
        except RuntimeError as exc:
            print(f"{label}: operator error: {exc}")
            result = {"CANCELLED"}
        ctx.c.eq(f"{label}: result", set(result), {step.get("result", "FINISHED")})
        sub = Context(ctx.c, step.get("checks", {}), ctx.report, ctx.package)
        for check in CHECKS:
            check(sub)


def main() -> int:
    package, expectations_path = _args()
    exp = json.loads(expectations_path.read_text("utf-8"))
    _register_from_repo()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # 期待値の "enable_addons" に挙げた add-on（例: VRM add-on の "bl_ext.blender_org.vrm"）を有効にする。
    # factory 設定で始めるため、使う add-on は明示的に有効化する。無ければテストをスキップする
    for module_name in exp.get("enable_addons", []):
        import addon_utils

        if addon_utils.enable(module_name, default_set=True) is None:
            print(f"\n=== integration_import: skipped (add-on {module_name} is not installed) ===")
            return 0

    from unitypackage_loader.blender import importer

    prepare_before(exp)
    extract_dir = tempfile.mkdtemp(prefix="unitypackage_loader_test_")
    options = {"filepath": str(package), "extract_mode": "CUSTOM", "extract_path": extract_dir}
    options.update(exp.get("options", {}))
    counts_before = {name: len(getattr(bpy.data, name)) for name in _DATA_KINDS}
    try:
        result = bpy.ops.import_scene.unitypackage(**options)
    except RuntimeError as exc:  # CANCELLED で ERROR を報告すると、バックグラウンドでは例外になる
        print(f"operator error: {exc}")
        result = {"CANCELLED"}
    report = importer.LAST_REPORT

    c = Check()
    c.eq("operator result", set(result), {exp.get("result", "FINISHED")})
    c.true("report exists", report is not None)
    if report is None:
        return _finish(c)
    if exp.get("leaves_nothing"):
        for name in _DATA_KINDS:
            c.eq(f"{name} left behind", len(getattr(bpy.data, name)), counts_before[name])

    ctx = Context(c, exp, report, package)
    check_result_state(ctx)
    for check in CHECKS:
        check(ctx)
    run_steps(ctx)
    return _finish(c)


def _finish(c: Check) -> int:
    print(f"\n=== integration_import: {c.passed} passed, {len(c.failures)} failed ===")
    for f in c.failures:
        print("  FAIL", f)
    return 1 if c.failures else 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        code = 2
    sys.exit(code)
