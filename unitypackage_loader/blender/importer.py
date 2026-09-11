"""インポート全体のオーケストレーション。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import bpy

from ..core.mapping import resolve_materials
from ..core.material import MaterialParseError, NormalizedMaterial, UnityMaterial, parse_material
from ..core.meta import ModelImporterInfo, TextureImporterInfo, parse_meta, strip_numeric_suffix
from ..core.package import PackageError, UnityPackage
from ..core.profiles import normalize_material
from ..core.report import ImportReport, MaterialReport
from . import materials as mat_builder
from .textures import load_image

_ROOT_PACKAGE = __package__.rsplit(".", 1)[0]  # bl_ext.<repo>.unitypackage_loader

# 直近のインポート結果（N パネル表示用）
LAST_REPORT: ImportReport | None = None


@dataclass
class ImportOptions:
    models: str = "ALL"  # ALL / FIRST
    material_mode: str = mat_builder.MODE_AUTO
    force_opaque: bool = False
    backface_culling: bool = True
    use_normal_maps: bool = True
    use_emission: bool = True
    reuse_existing: bool = False
    store_props: bool = True
    extract_mode: str = "BESIDE_BLEND"  # BESIDE_BLEND / CACHE / CUSTOM
    extract_path: str = ""
    pack_images: bool = False
    import_unreferenced: bool = False
    overwrite_extracted: bool = False
    fbx_importer: str = "AUTO"  # AUTO / NEW / LEGACY
    use_anim: bool = False
    ignore_leaf_bones: bool = True
    global_scale: float = 1.0
    extra_fbx_kwargs: dict = field(default_factory=dict)


class ImportCancelled(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 展開先
# ---------------------------------------------------------------------------


def cache_root() -> Path:
    return Path(bpy.utils.extension_path_user(_ROOT_PACKAGE, path="cache", create=True))


def resolve_extract_root(opts: ImportOptions, package_path: Path) -> Path:
    stem = package_path.stem
    if opts.extract_mode == "CUSTOM" and opts.extract_path:
        return Path(bpy.path.abspath(opts.extract_path)) / stem
    if opts.extract_mode == "BESIDE_BLEND" and bpy.data.filepath:
        return Path(bpy.data.filepath).parent / "textures" / stem
    return cache_root() / stem


# ---------------------------------------------------------------------------
# bpy.data の差分取得
# ---------------------------------------------------------------------------

_TRACKED = ("objects", "materials", "images", "meshes", "armatures", "actions")


def _snapshot() -> dict[str, set[str]]:
    return {name: {d.name for d in getattr(bpy.data, name)} for name in _TRACKED}


def _new_since(before: dict[str, set[str]]) -> dict[str, list]:
    result = {}
    for name in _TRACKED:
        coll = getattr(bpy.data, name)
        result[name] = [d for d in coll if d.name not in before[name]]
    return result


def _find_layer_collection(layer_coll, target):
    if layer_coll.collection == target:
        return layer_coll
    for child in layer_coll.children:
        found = _find_layer_collection(child, target)
        if found is not None:
            return found
    return None


def _import_fbx(context, path: Path, opts: ImportOptions) -> None:
    use_new = hasattr(bpy.ops.wm, "fbx_import") and opts.fbx_importer in ("AUTO", "NEW")
    if use_new:
        kwargs = dict(
            filepath=str(path),
            global_scale=opts.global_scale,
            use_anim=opts.use_anim,
            ignore_leaf_bones=opts.ignore_leaf_bones,
            use_custom_normals=True,
        )
        kwargs.update(opts.extra_fbx_kwargs)
        result = bpy.ops.wm.fbx_import(**kwargs)
    else:
        kwargs = dict(
            filepath=str(path),
            global_scale=opts.global_scale,
            use_anim=opts.use_anim,
            ignore_leaf_bones=opts.ignore_leaf_bones,
            use_custom_normals=True,
            use_image_search=False,
        )
        kwargs.update(opts.extra_fbx_kwargs)
        result = bpy.ops.import_scene.fbx(**kwargs)
    if "FINISHED" not in result:
        raise RuntimeError(f"FBX import failed for {path.name}: {result}")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def run_import(context, filepath: str, opts: ImportOptions, progress=None) -> ImportReport:
    global LAST_REPORT
    package_path = Path(filepath)
    report = ImportReport(package=package_path.name)

    def step(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    step(0.0, "Scanning package")
    pkg = UnityPackage(package_path)
    pkg.scan()

    models = pkg.models()
    if not models:
        raise PackageError("the package contains no model files (.fbx/.obj/.dae/.blend)")
    supported = [m for m in models if m.ext == ".fbx"]
    skipped = [m for m in models if m.ext != ".fbx"]
    for m in skipped:
        report.warn(f"model format not supported yet, skipped: {m.pathname}")
    if not supported:
        raise PackageError("no FBX models in the package (other formats are not supported yet)")
    if opts.models == "FIRST":
        supported = supported[:1]

    # --- .mat の解析と正規化 ---
    step(0.1, "Parsing materials")
    unity_mats: dict[str, UnityMaterial] = {}
    normalized: dict[str, NormalizedMaterial] = {}
    for entry in pkg.materials():
        try:
            umat = parse_material(pkg.read_text(entry.guid), entry.guid, entry.pathname)
        except (MaterialParseError, ValueError) as exc:
            report.warn(f"could not parse {entry.pathname}: {exc}")
            continue
        unity_mats[entry.guid] = umat
        normalized[entry.guid] = normalize_material(umat)

    # --- 必要なテクスチャを決めて展開 ---
    needed_tex: set[str] = set()
    for norm in normalized.values():
        needed_tex.update(t.guid for t in norm.texture_refs())
    if opts.import_unreferenced:
        needed_tex.update(e.guid for e in pkg.textures())
    missing_tex = {g for g in needed_tex if pkg.get(g) is None}
    for g in sorted(missing_tex):
        report.warn(f"texture {g} is referenced but not included in the package")
    needed_tex -= missing_tex

    extract_root = resolve_extract_root(opts, package_path)
    report.extract_root = str(extract_root)
    step(0.2, "Extracting files")
    paths = pkg.extract(
        [m.guid for m in supported] + sorted(needed_tex),
        extract_root,
        overwrite=opts.overwrite_extracted,
        progress=lambda f, n: step(0.2 + 0.3 * f, f"Extracting {n}"),
    )

    # --- 画像の読み込み ---
    step(0.5, "Loading textures")
    images: dict[str, bpy.types.Image | None] = {}
    tex_infos: dict[str, TextureImporterInfo] = {}
    for guid in sorted(needed_tex):
        entry = pkg.get(guid)
        info = TextureImporterInfo.from_meta(entry.meta_text) if entry.meta_text else TextureImporterInfo()
        tex_infos[guid] = info
        path = paths.get(guid)
        image = load_image(path, info, pack=opts.pack_images) if path else None
        images[guid] = image
        if image is None:
            report.warn(f"could not load texture {entry.pathname} (unsupported format?)")
        else:
            report.images.append(image.name)

    # --- パッケージ用コレクション ---
    scene = context.scene
    collection = bpy.data.collections.new(package_path.stem)
    scene.collection.children.link(collection)
    view_layer = context.view_layer
    prev_active = view_layer.active_layer_collection
    layer_coll = _find_layer_collection(view_layer.layer_collection, collection)
    if layer_coll is not None:
        view_layer.active_layer_collection = layer_coll

    build_opts = mat_builder.MaterialBuildOptions(
        mode=opts.material_mode,
        force_opaque=opts.force_opaque,
        backface_culling=opts.backface_culling,
        use_normal_maps=opts.use_normal_maps,
        use_emission=opts.use_emission,
        store_props=opts.store_props,
    )

    try:
        for index, model in enumerate(supported):
            step(0.55 + 0.4 * index / len(supported), f"Importing {model.name}")
            model_info = ModelImporterInfo.from_meta(model.meta_text) if model.meta_text else ModelImporterInfo()
            before = _snapshot()
            existing_materials = {m.name: m for m in bpy.data.materials}
            _import_fbx(context, paths[model.guid], opts)
            new = _new_since(before)
            report.models.append(model.pathname)
            report.objects.extend(o.name for o in new["objects"])

            new_materials: list[bpy.types.Material] = new["materials"]
            fbx_names = [m.name for m in new_materials]
            resolution = resolve_materials(fbx_names, model_info, unity_mats, model.pathname)

            for bmat in new_materials:
                res = resolution[bmat.name]
                mrep = MaterialReport(blender_name=bmat.name, fbx_name=bmat.name, guid=res.guid, method=res.method)
                report.materials.append(mrep)

                if opts.reuse_existing:
                    base = strip_numeric_suffix(bmat.name)
                    existing = existing_materials.get(base)
                    if existing is not None and existing is not bmat:
                        _replace_material(new["objects"], bmat, existing)
                        bpy.data.materials.remove(bmat)
                        mrep.method = "reused"
                        mrep.blender_name = existing.name
                        continue

                if res.guid is None:
                    mrep.warnings.append("no matching .mat in package; material left as imported")
                    report.warn(f"material {bmat.name!r}: no matching .mat in package")
                    continue

                norm = normalized[res.guid]
                mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
                try:
                    mode, warnings = mat_builder.build_material(bmat, norm, images, tex_infos, build_opts)
                except Exception as exc:  # noqa: BLE001 - 1 マテリアルの失敗で全体を止めない
                    mrep.warnings.append(f"node build failed: {exc!r}")
                    report.warn(f"material {bmat.name!r}: node build failed: {exc!r}")
                    continue
                mrep.mode = mode
                mrep.warnings.extend(warnings)
                for w in warnings:
                    report.warn(f"material {bmat.name!r}: {w}")
                mrep.textures = [
                    f"{role}={pkg.get(t.guid).pathname if pkg.get(t.guid) else t.guid}"
                    for role, t in (
                        ("base", norm.base_color_tex),
                        ("normal", norm.normal_tex),
                        ("emission", norm.emission_tex),
                        ("metallic", norm.metallic_tex),
                    )
                    if t is not None
                ]
    finally:
        if prev_active is not None:
            view_layer.active_layer_collection = prev_active

    collection["unity_package"] = package_path.name
    step(1.0, "Done")
    LAST_REPORT = report
    return report


def _replace_material(objects, old: bpy.types.Material, new: bpy.types.Material) -> None:
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material == old:
                slot.material = new
