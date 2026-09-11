"""インポート全体のオーケストレーション。

``prepare_package`` でパッケージを走査・解析し（モデル選択ダイアログにも使う）、
``run_import`` で実際の展開・FBX 読み込み・マテリアル構築を行う。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import bpy

from ..core.mapping import resolve_materials, slot_assignments
from ..core.material import MaterialParseError, NormalizedMaterial, UnityMaterial, parse_material
from ..core.meta import ModelImporterInfo, TextureImporterInfo, strip_numeric_suffix
from ..core.package import AssetEntry, PackageError, UnityPackage
from ..core.prefab import RendererMaterials, merge_prefab_tables, parse_prefab_materials
from ..core.profiles import ShaderTable, normalize_material
from ..core.profiles.base import default_table
from ..core.report import ImportReport, MaterialReport
from . import materials as mat_builder
from . import outline as outline_builder
from .textures import load_image

_ROOT_PACKAGE = __package__.rsplit(".", 1)[0]  # bl_ext.<repo>.unitypackage_loader

# 直近のインポート結果（N パネル表示用）
LAST_REPORT: ImportReport | None = None

# 読み込めるモデル形式と、同じフォルダから一緒に展開する付随ファイル
SUPPORTED_MODEL_EXTS = frozenset({".fbx", ".obj", ".gltf", ".glb", ".dae", ".blend"})
_SIDECAR_EXTS = {".obj": {".mtl"}, ".gltf": {".bin"}}


@dataclass
class ImportOptions:
    models: str = "ASK"  # ASK / ALL / FIRST
    model_guids: list[str] | None = None  # 明示的に選ばれたモデル（ダイアログ経由）
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
    blend_materials: str = "KEEP"  # .blend 同梱モデル: KEEP（既存マテリアルを残す）/ REBUILD
    outlines: bool = False  # Unity のアウトライン設定を Solidify で再現する
    outline_width_scale: float = 0.01
    prefab: str = ""  # マテリアル割り当てに使う prefab の pathname（空なら全 prefab を先勝ちで統合）
    shader_table_path: str = ""
    extra_fbx_kwargs: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 事前解析
# ---------------------------------------------------------------------------


@dataclass
class ModelSummary:
    entry: AssetEntry
    material_count: int  # externalObjects に登録されたマテリアル数
    resolved_count: int  # そのうちパッケージ内の .mat に対応付けできた数
    supported: bool

    @property
    def guid(self) -> str:
        return self.entry.guid


@dataclass
class PreparedPackage:
    path: Path
    pkg: UnityPackage
    unity_mats: dict[str, UnityMaterial]
    normalized: dict[str, NormalizedMaterial]
    models: list[ModelSummary]
    referenced_textures: set[str]
    missing_textures: set[str]
    prefab_tables: dict[str, dict[str, RendererMaterials]] = field(default_factory=dict)  # pathname → 表
    warnings: list[str] = field(default_factory=list)

    @property
    def supported_models(self) -> list[ModelSummary]:
        return [m for m in self.models if m.supported]

    @property
    def prefab_table(self) -> dict[str, RendererMaterials]:
        return self.table_for("")

    def table_for(self, prefab_pathname: str) -> dict[str, RendererMaterials]:
        """指定 prefab の表。未指定・不明なら全 prefab をパス順に先勝ちで統合したもの。"""
        if prefab_pathname and prefab_pathname in self.prefab_tables:
            return self.prefab_tables[prefab_pathname]
        return merge_prefab_tables([self.prefab_tables[k] for k in sorted(self.prefab_tables)])


def build_shader_table(extra_path: str = "") -> ShaderTable:
    table = default_table()
    if extra_path:
        path = Path(bpy.path.abspath(extra_path))
        if path.is_file():
            merged = ShaderTable()
            extra = ShaderTable(path)
            merged.by_guid.update(extra.by_guid)
            merged.by_builtin_id.update(extra.by_builtin_id)
            return merged
    return table


def prepare_package(filepath: str, shader_table: ShaderTable | None = None) -> PreparedPackage:
    path = Path(filepath)
    pkg = UnityPackage(path)
    pkg.scan()
    table = shader_table or default_table()
    warnings: list[str] = []

    unity_mats: dict[str, UnityMaterial] = {}
    normalized: dict[str, NormalizedMaterial] = {}
    for entry in pkg.materials():
        try:
            umat = parse_material(pkg.read_text(entry.guid), entry.guid, entry.pathname)
        except (MaterialParseError, ValueError) as exc:
            warnings.append(f"could not parse {entry.pathname}: {exc}")
            continue
        unity_mats[entry.guid] = umat
        normalized[entry.guid] = normalize_material(umat, table)

    models: list[ModelSummary] = []
    for entry in pkg.models():
        info = ModelImporterInfo.from_meta(entry.meta_text) if entry.meta_text else ModelImporterInfo()
        names = list(info.external_materials)
        resolution = resolve_materials(names, info, unity_mats, entry.pathname)
        resolved = sum(1 for r in resolution.values() if r.guid)
        models.append(ModelSummary(entry, len(names), resolved, entry.ext in SUPPORTED_MODEL_EXTS))
    if not models:
        raise PackageError("the package contains no model files (.fbx/.obj/.gltf/.dae/.blend)")

    referenced: set[str] = set()
    for norm in normalized.values():
        referenced.update(t.guid for t in norm.texture_refs())
        referenced.update(norm.extra_texture_guids())
    missing = {g for g in referenced if pkg.get(g) is None}

    prefab_tables: dict[str, dict[str, RendererMaterials]] = {}
    for entry in pkg.prefabs():
        try:
            table = parse_prefab_materials(pkg.read_text(entry.guid))
        except Exception as exc:  # noqa: BLE001 - prefab は補助情報なので失敗しても続ける
            warnings.append(f"could not parse prefab {entry.pathname}: {exc}")
            continue
        if table:
            prefab_tables[entry.pathname] = table
    return PreparedPackage(path, pkg, unity_mats, normalized, models, referenced, missing, prefab_tables, warnings)


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


def _sidecar_guids(pkg: UnityPackage, model: AssetEntry) -> list[str]:
    """OBJ の .mtl、glTF の .bin など、モデルと同じフォルダに置くべきファイルの GUID。"""
    exts = _SIDECAR_EXTS.get(model.ext)
    if not exts:
        return []
    folder = model.pathname.rsplit("/", 1)[0]
    return [
        e.guid
        for e in pkg.entries.values()
        if e.has_asset and e.ext in exts and e.pathname.rsplit("/", 1)[0] == folder
    ]


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


def _import_blend(context, path: Path, collection: bpy.types.Collection) -> None:
    """同梱 .blend の全オブジェクトを append してコレクションに入れる。"""
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        data_to.objects = list(data_from.objects)
    for obj in data_to.objects:
        if obj is not None:
            collection.objects.link(obj)


def _import_model(context, path: Path, model: AssetEntry, opts: ImportOptions, collection) -> None:
    ext = model.ext
    if ext == ".fbx":
        _import_fbx(context, path, opts)
        return
    if ext == ".obj":
        result = bpy.ops.wm.obj_import(filepath=str(path), global_scale=opts.global_scale)
    elif ext in (".gltf", ".glb"):
        result = bpy.ops.import_scene.gltf(filepath=str(path))
    elif ext == ".dae":
        result = bpy.ops.wm.collada_import(filepath=str(path))
    elif ext == ".blend":
        _import_blend(context, path, collection)
        return
    else:
        raise RuntimeError(f"unsupported model format: {model.pathname}")
    if "FINISHED" not in result:
        raise RuntimeError(f"import failed for {path.name}: {result}")


def _select_models(prepared: PreparedPackage, opts: ImportOptions) -> list[ModelSummary]:
    supported = prepared.supported_models
    if opts.model_guids is not None:
        wanted = set(opts.model_guids)
        return [m for m in supported if m.guid in wanted]
    if opts.models == "FIRST":
        return supported[:1]
    return supported


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def run_import(
    context,
    filepath: str,
    opts: ImportOptions,
    progress=None,
    prepared: PreparedPackage | None = None,
) -> ImportReport:
    global LAST_REPORT
    package_path = Path(filepath)
    report = ImportReport(package=package_path.name)

    def step(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    step(0.0, "Scanning package")
    if prepared is None:
        prepared = prepare_package(filepath, build_shader_table(opts.shader_table_path))
    pkg = prepared.pkg
    for w in prepared.warnings:
        report.warn(w)
    for m in prepared.models:
        if not m.supported:
            report.warn(f"model format not supported yet, skipped: {m.entry.pathname}")

    models = _select_models(prepared, opts)
    if not models:
        raise PackageError("no importable models selected")

    unity_mats, normalized = prepared.unity_mats, prepared.normalized

    # --- 必要なテクスチャを決めて展開 ---
    needed_tex = set(prepared.referenced_textures)
    if opts.import_unreferenced:
        needed_tex.update(e.guid for e in pkg.textures())
    for g in sorted(prepared.missing_textures):
        report.warn(f"texture {g} is referenced but not included in the package")
    needed_tex -= prepared.missing_textures

    extract_root = resolve_extract_root(opts, package_path)
    report.extract_root = str(extract_root)
    step(0.2, "Extracting files")
    sidecars = [g for m in models for g in _sidecar_guids(pkg, m.entry)]
    paths = pkg.extract(
        [m.guid for m in models] + sidecars + sorted(needed_tex),
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

    mat_builder.tag_images(images, tex_infos)

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

    prefab_table = prepared.table_for(opts.prefab)
    built_by_guid: dict[str, bpy.types.Material] = {}  # 同じ .mat は 1 つの Blender マテリアルを共有

    try:
        for index, summary in enumerate(models):
            model = summary.entry
            step(0.55 + 0.4 * index / len(models), f"Importing {model.name}")
            model_info = ModelImporterInfo.from_meta(model.meta_text) if model.meta_text else ModelImporterInfo()
            before = _snapshot()
            existing_materials = {m.name: m for m in bpy.data.materials}
            _import_model(context, paths[model.guid], model, opts, collection)
            new = _new_since(before)
            report.models.append(model.pathname)
            report.objects.extend(o.name for o in new["objects"])

            new_materials: list[bpy.types.Material] = new["materials"]
            fbx_names = [m.name for m in new_materials]
            object_slots = {
                o.name: [slot.material.name if slot.material else "" for slot in o.material_slots]
                for o in new["objects"]
                if o.type == "MESH"
            }
            resolution = resolve_materials(
                fbx_names, model_info, unity_mats, model.pathname, prefab_table, object_slots
            )
            assignments = slot_assignments(object_slots, prefab_table, unity_mats)

            keep_blend_materials = model.ext == ".blend" and opts.blend_materials == "KEEP"
            for bmat in new_materials:
                res = resolution[bmat.name]
                mrep = MaterialReport(blender_name=bmat.name, fbx_name=bmat.name, guid=res.guid, method=res.method)
                report.materials.append(mrep)
                if keep_blend_materials:
                    mrep.method = "kept"
                    if res.guid:
                        norm = normalized[res.guid]
                        mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
                        if opts.store_props:
                            mat_builder._store_props(bmat, norm)
                    continue
                if res.warning:
                    mrep.warnings.append(res.warning)
                    report.warn(f"material {bmat.name!r}: {res.warning}")

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

                _build_and_report(bmat, res.guid, normalized, images, tex_infos, build_opts, pkg, report, mrep)
                built_by_guid.setdefault(res.guid, bmat)

            # prefab がスロットごとに別の .mat を指している場合は、そのスロットだけ別マテリアルに差し替える
            if assignments and not (model.ext == ".blend" and opts.blend_materials == "KEEP"):
                split = _split_slots_by_prefab(
                    new["objects"], assignments, resolution, unity_mats, normalized, built_by_guid,
                    images, tex_infos, build_opts, pkg, report,
                )
                if split:
                    report.split_slots += split
                    # 差し替えで使われなくなった FBX マテリアルは片付ける
                    for bmat in new_materials:
                        if bmat.users == 0:
                            for mrep in report.materials:
                                if mrep.blender_name == bmat.name and mrep.method != "replaced":
                                    mrep.method = "replaced"
                            bpy.data.materials.remove(bmat)
            if opts.outlines:
                added = outline_builder.apply_outlines(new["objects"], opts.outline_width_scale)
                if added:
                    report.outlines += added
    finally:
        if prev_active is not None:
            view_layer.active_layer_collection = prev_active

    collection["unity_package"] = package_path.name
    step(1.0, "Done")
    LAST_REPORT = report
    return report


def _build_and_report(bmat, guid, normalized, images, tex_infos, build_opts, pkg, report, mrep) -> None:
    norm = normalized[guid]
    mrep.family, mrep.shader_name, mrep.alpha_mode = norm.family, norm.shader_name or "", norm.alpha_mode
    try:
        mode, warnings = mat_builder.build_material(bmat, norm, images, tex_infos, build_opts)
    except Exception as exc:  # noqa: BLE001 - 1 マテリアルの失敗で全体を止めない
        mrep.warnings.append(f"node build failed: {exc!r}")
        report.warn(f"material {bmat.name!r}: node build failed: {exc!r}")
        return
    mrep.mode = mode
    mrep.warnings.extend(warnings)
    for w in warnings:
        if w.startswith("shader table entry"):
            report.warn(w)  # シェーダー単位の注意なので 1 回だけ
        else:
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


def _split_slots_by_prefab(objects, assignments, resolution, unity_mats, normalized, built_by_guid,
                           images, tex_infos, build_opts, pkg, report) -> int:
    """(オブジェクト, スロット) ごとに prefab の .mat を割り当てる。差し替えたスロット数を返す。"""
    by_name = {o.name: o for o in objects if o.type == "MESH"}
    count = 0
    for (obj_name, index), guid in assignments.items():
        obj = by_name.get(obj_name)
        if obj is None or index >= len(obj.material_slots):
            continue
        slot = obj.material_slots[index]
        current = slot.material
        current_guid = resolution[current.name].guid if current is not None and current.name in resolution else None
        if current_guid == guid:
            continue
        mat = built_by_guid.get(guid)
        if mat is None:
            umat = unity_mats[guid]
            mat = bpy.data.materials.new(umat.name or guid[:8])
            mrep = MaterialReport(
                blender_name=mat.name,
                fbx_name=current.name if current is not None else "",
                guid=guid,
                method="prefab-split",
            )
            report.materials.append(mrep)
            _build_and_report(mat, guid, normalized, images, tex_infos, build_opts, pkg, report, mrep)
            built_by_guid[guid] = mat
        slot.material = mat
        count += 1
    return count


def _replace_material(objects, old: bpy.types.Material, new: bpy.types.Material) -> None:
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material == old:
                slot.material = new
