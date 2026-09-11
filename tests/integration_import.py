"""Blender 上で実際にインポートし、期待値 JSON と照合する統合テスト。

実行例::

    blender -b --python tests/integration_import.py -- [package.unitypackage] [expectations.json]

引数を省略すると ``_local/`` 直下の最初の .unitypackage と ``_local/expectations.json`` を使う。
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

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = REPO_ROOT / "_local"


def _args() -> tuple[Path, Path]:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    expectations = Path(argv[1]) if len(argv) > 1 else LOCAL_DIR / "expectations.json"
    if not expectations.is_file():
        raise SystemExit(f"expectations file not found: {expectations}")
    if argv:
        package = Path(argv[0])
    else:
        # 期待値ファイルの "package" キー（_local/ 内のファイル名）→ 無ければ _local/ の最初のパッケージ
        named = json.loads(expectations.read_text("utf-8")).get("package")
        package = LOCAL_DIR / named if named else next(iter(sorted(LOCAL_DIR.glob("*.unitypackage"))), None)
    if package is None or not package.is_file():
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

    from unitypackage_loader.blender import importer

    extract_dir = tempfile.mkdtemp(prefix="unitypackage_loader_test_")
    options = {"filepath": str(package), "extract_mode": "CUSTOM", "extract_path": extract_dir}
    options.update(exp.get("options", {}))
    result = bpy.ops.import_scene.unitypackage(**options)
    report = importer.LAST_REPORT

    c = Check()
    c.eq("operator result", set(result), {"FINISHED"})
    c.true("report exists", report is not None)
    if report is None:
        return _finish(c)

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
        if "node_types" in spec:
            c.true(
                f"{name}.node_types",
                set(spec["node_types"]) <= {n.bl_idname for n in mat.node_tree.nodes},
                f"missing {set(spec['node_types']) - {n.bl_idname for n in mat.node_tree.nodes}}",
            )

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
