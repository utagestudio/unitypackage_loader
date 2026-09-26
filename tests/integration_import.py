"""Blender 上で実際にインポートし、期待値 JSON と照合する統合テスト。

実行例::

    blender -b --python tests/integration_import.py -- [package.unitypackage] [expectations.json]

引数を省略すると ``_local/unitypackages/`` の最初の .unitypackage と ``_local/expectations.json`` を使う。
期待値ファイルの書式は ``tests/expectations.schema.md`` を参照。検証用データはリポジトリに
含めないため、どちらも gitignore 対象。
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = REPO_ROOT / "_local"
PACKAGES_DIR = LOCAL_DIR / "unitypackages"  # 検証用 .unitypackage の置き場
# 失敗したときに片付けられているかを数えるデータの種類
_DATA_KINDS = ("objects", "collections", "meshes", "materials", "images", "armatures", "actions", "lights", "cameras")


def _local_packages() -> list[Path]:
    """検証用の .unitypackage。``_local/unitypackages/`` を先に、続けて ``_local/`` 直下を探す。"""
    found: list[Path] = []
    for directory in (PACKAGES_DIR, LOCAL_DIR):
        if directory.is_dir():
            found += sorted(directory.glob("*.unitypackage"))
    return found


def _find_package(name: str) -> Path | None:
    """ファイル名から検証用パッケージを探す。見つからなければ ``None``。"""
    for candidate in (PACKAGES_DIR / name, LOCAL_DIR / name):
        if candidate.is_file():
            return candidate
    return None


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
        package = _find_package(named) if named else next(iter(_local_packages()), None)
        if package is None:
            raise SystemExit(f"package not found in {PACKAGES_DIR}: {named or '*.unitypackage'}")
    if not package.is_file():
        raise SystemExit(f"package not found: {package}")
    return package, expectations


def _register_from_repo() -> None:
    """symlink 先ではなくリポジトリの実体をそのまま登録する。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
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


def _strip_suffix(name: str) -> str:
    base, dot, suffix = name.rpartition(".")
    return base if dot and suffix.isdigit() and len(suffix) == 3 else name


def _find_placed(top: str, name: str, parent: str | None = None) -> list:
    found = []
    for obj in bpy.data.objects:
        if _strip_suffix(obj.name) != name:
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
    if "errors" in exp:
        c.eq("error count", len(report.errors), exp["errors"])
    if exp.get("leaves_nothing"):
        for name in _DATA_KINDS:
            c.eq(f"{name} left behind", len(getattr(bpy.data, name)), counts_before[name])
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

    objs = exp.get("objects", {})
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if "count" in objs:
        c.eq("object count", len(report.objects), objs["count"])
    if "mesh_count" in objs:
        c.eq("mesh count", len(meshes), objs["mesh_count"])
    if "armature_count" in objs:
        c.eq("armature count", len(armatures), objs["armature_count"])

    mats = exp.get("materials", {})
    if "count" in mats:
        c.eq("material count", len(report.materials), mats["count"])
    if "mapped" in mats:
        c.eq("mapped materials", report.mapped_count, mats["mapped"])
    if "families" in mats:
        c.eq("shader families", sorted({m.family for m in report.materials}), sorted(mats["families"]))
    if "methods" in mats:
        c.eq("mapping methods", sorted({m.method for m in report.materials}), sorted(mats["methods"]))

    if "count" in exp.get("images", {}):
        c.eq("image count", len(report.images), exp["images"]["count"])
    if "warnings_max" in exp:
        c.le("warning count", len(report.warnings), exp["warnings_max"])
    for text in exp.get("warnings_contain", []):
        c.true(f"warning contains {text!r}", any(text in w for w in report.warnings))
    for text in exp.get("warnings_exclude", []):
        c.eq(f"warnings without {text!r}", [w for w in report.warnings if text in w], [])
    if "split_slots" in exp:
        c.eq("split slots", report.split_slots, exp["split_slots"])

    # ポリゴンで最初に使われた順（Unity のサブメッシュ順）に並べたマテリアル名。スロットの並びに依らず、
    # どのポリゴン群にどのマテリアルが付いたかを確かめる
    for obj_name, names in exp.get("submesh_materials", {}).items():
        obj = bpy.data.objects.get(obj_name)
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

    for spec in exp.get("material_checks", []):
        name = spec["name"]
        mat = bpy.data.materials.get(name)
        c.true(f"material {name} exists", mat is not None)
        if mat is None:
            continue
        if "alpha_mode" in spec:
            c.eq(f"{name}.alpha_mode", mat.get("unity_alpha_mode"), spec["alpha_mode"])
        if "render_method" in spec:
            c.eq(f"{name}.surface_render_method", mat.surface_render_method, spec["render_method"])
        if "backface_culling" in spec:
            c.eq(f"{name}.use_backface_culling", mat.use_backface_culling, spec["backface_culling"])
        if "mode" in spec:
            rep = next((m for m in report.materials if m.blender_name == name), None)
            c.eq(f"{name}.mode", rep.mode if rep else None, spec["mode"])
        if "shader_name" in spec:
            c.eq(f"{name}.shader_name", mat.get("unity_shader_name"), spec["shader_name"])
        for role, label in (("base", "Base Color"), ("normal", "Normal")):
            key = f"{role}_image"
            if key in spec:
                image = _image_of(mat, label) or (_image_of(mat, f"{label} (unused)") if role == "normal" else None)
                c.eq(f"{name}.{key}", image.name if image else None, spec[key])
                if image is not None and f"{role}_colorspace" in spec:
                    c.eq(f"{name}.{role}_colorspace", image.colorspace_settings.name, spec[f"{role}_colorspace"])
        if "emission_strength" in spec or "emission_color" in spec:
            bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            c.true(f"{name} has Principled BSDF", bsdf is not None)
            if bsdf is not None and "emission_strength" in spec:
                c.eq(f"{name}.emission_strength", round(bsdf.inputs["Emission Strength"].default_value, 4), spec["emission_strength"])
            if bsdf is not None and "emission_color" in spec:
                actual = [round(v, 4) for v in bsdf.inputs["Emission Color"].default_value[:3]]
                c.eq(f"{name}.emission_color", actual, spec["emission_color"])
        if "node_types" in spec:
            c.true(
                f"{name}.node_types",
                set(spec["node_types"]) <= {n.bl_idname for n in mat.node_tree.nodes},
                f"missing {set(spec['node_types']) - {n.bl_idname for n in mat.node_tree.nodes}}",
            )

    if "count" in exp.get("prefabs", {}):
        c.eq("prefab count", len(report.prefabs), exp["prefabs"]["count"])

    # 読み込む単位 Prefabs で prefab ごとに作られるコレクション。マテリアルは Blender 上の名前ではなく、
    # 元の .mat のファイル名（unity_material_path の末尾）で比べる（同じモデルを読み直すと名前に連番が付くため）
    for coll_name, spec in exp.get("collections", {}).items():
        coll = bpy.data.collections.get(coll_name)
        c.true(f"collection {coll_name} exists", coll is not None)
        if coll is None:
            continue
        objects = list(coll.all_objects)
        if "objects" in spec:
            c.eq(f"{coll_name}.objects", len(objects), spec["objects"])
        if "prefab" in spec:
            c.eq(f"{coll_name}.unity_prefab", coll.get("unity_prefab"), spec["prefab"])
        if "mat_files" in spec:
            files = {
                slot.material.get("unity_material_path", "").rsplit("/", 1)[-1]
                for obj in objects
                if obj.type == "MESH"
                for slot in obj.material_slots
                if slot.material is not None
            }
            c.eq(f"{coll_name}.mat_files", sorted(files), sorted(spec["mat_files"]))
        # コレクションの中の個々のオブジェクト（非表示か、LODGroup の段）
        for obj_name, part in spec.get("parts", {}).items():
            obj = next((o for o in objects if o.name == obj_name), None)
            c.true(f"{coll_name}/{obj_name} exists", obj is not None)
            if obj is None:
                continue
            if "hidden" in part:
                c.eq(f"{coll_name}/{obj_name}.hidden", obj.hide_get(), part["hidden"])
            if "lod" in part:
                c.eq(f"{coll_name}/{obj_name}.lod", obj.get("unity_lod"), part["lod"])

    if "prefab_collections_overlap" in exp:
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
        c.eq("prefab collections overlap", bool(overlaps), exp["prefab_collections_overlap"])

    if "count" in exp.get("scenes", {}):
        c.eq("scene count", len(report.scenes), exp["scenes"]["count"])

    # 読み込む単位 Scenes で配置したオブジェクト。複製すると名前に連番が付くので、最上位の Empty（Unity の最上位の
    # GameObject）の名前とオブジェクト名（連番を除く）で探す。parent があれば直接の親の Empty の名前でも絞る。
    # tip は重心から最も遠い頂点のワールド座標
    for spec in exp.get("placed_objects", []):
        label = "/".join(filter(None, (spec["top"], spec.get("parent"), spec["object"])))
        found = _find_placed(spec["top"], spec["object"], spec.get("parent"))
        c.eq(f"{label} found", len(found), 1)
        if len(found) != 1:
            continue
        obj = found[0]
        if "tip" in spec:
            tip = _tip_world(obj)
            c.true(f"{label}.tip", (tip - Vector(spec["tip"])).length <= 1e-3, f"expected {spec['tip']}, got {[round(v, 4) for v in tip]}")
        if "hidden" in spec:
            c.eq(f"{label}.hidden", obj.hide_get(), spec["hidden"])
        if "lod" in spec:
            c.eq(f"{label}.lod", obj.get("unity_lod"), spec["lod"])
        if "mat_files" in spec:
            files = sorted({s.material.get("unity_material_path", "").rsplit("/", 1)[-1] for s in obj.material_slots if s.material})
            c.eq(f"{label}.mat_files", files, sorted(spec["mat_files"]))

    # シーンのライト・カメラ（#49）。direction はオブジェクトの -Z（ライト・カメラの向き）のワールドでの向き
    bpy.context.view_layer.update()
    for key, kind in (("lights", "LIGHT"), ("cameras", "CAMERA")):
        for spec in exp.get(key, []):
            obj = bpy.data.objects.get(spec["name"])
            label = f"{kind.lower()} {spec['name']}"
            c.true(f"{label} exists", obj is not None and obj.type == kind)
            if obj is None or obj.type != kind:
                continue
            for attr in ("type", "sensor_fit", "shape", "use_shadow", "use_custom_distance", "use_temperature"):
                if attr in spec:
                    c.eq(f"{label}.{attr}", getattr(obj.data, attr), spec[attr])
            for attr in ("energy", "lens", "ortho_scale", "clip_end", "cutoff_distance", "temperature", "spot_size", "spot_blend", "size", "size_y"):
                if attr in spec:
                    actual = getattr(obj.data, attr)
                    c.true(f"{label}.{attr}", abs(actual - spec[attr]) <= 1e-3 * max(1.0, abs(spec[attr])), f"expected {spec[attr]}, got {actual}")
            if "direction" in spec:
                direction = (obj.matrix_world.to_3x3() @ Vector((0, 0, -1))).normalized()
                c.true(f"{label}.direction", (direction - Vector(spec["direction"])).length <= 1e-3,
                       f"expected {spec['direction']}, got {[round(v, 4) for v in direction]}")
            if "location" in spec:
                c.true(f"{label}.location", (obj.matrix_world.translation - Vector(spec["location"])).length <= 1e-3,
                       f"expected {spec['location']}, got {[round(v, 4) for v in obj.matrix_world.translation]}")
            if "hidden" in spec:
                c.eq(f"{label}.hidden", obj.hide_get(), spec["hidden"])
    if "scene_camera" in exp:
        camera = bpy.context.scene.camera
        c.eq("scene camera", camera.name if camera else None, exp["scene_camera"])
    if "light_count" in exp:
        c.eq("report lights", report.lights, exp["light_count"])
    if "camera_count" in exp:
        c.eq("report cameras", report.cameras, exp["camera_count"])

    for spec in exp.get("shared_meshes", []):
        found = [_find_placed(o["top"], o["object"]) for o in spec["objects"]]
        labels = "+".join(f"{o['top']}/{o['object']}" for o in spec["objects"])
        if all(len(f) == 1 for f in found):
            shared = len({f[0].data.name for f in found}) == 1
            c.eq(f"{labels} share mesh data", shared, spec["shared"])
        else:
            c.true(f"{labels} found", False, "objects not found")

    for obj_name, count in exp.get("shape_keys", {}).items():
        obj = bpy.data.objects.get(obj_name)
        c.true(f"object {obj_name} exists", obj is not None)
        if obj is not None:
            actual = len(obj.data.shape_keys.key_blocks) if obj.data.shape_keys else 0
            c.eq(f"{obj_name}.shape_keys", actual, count)

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
