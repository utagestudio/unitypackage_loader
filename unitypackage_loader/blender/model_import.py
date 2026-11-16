"""モデルファイル（FBX・OBJ・glTF・VRM・Collada・同梱 .blend）を Blender のインポーターで読み込む処理と、
読み込みで増えたデータブロックの差分（失敗したときの片付けに使う）。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import bpy

from ..core.fbx_units import read_unit_scale
from ..core.mapping import submesh_slot_order
from ..core.meta import ModelImporterInfo
from ..core.model_ids import ModelObject
from ..core.package import AssetEntry, UnityPackage
from ..core.report import ImportReport

if TYPE_CHECKING:
    from .options import ImportOptions

# 読み込むモデルと同じフォルダから一緒に展開する付随ファイル
_SIDECAR_EXTS = {".obj": {".mtl"}, ".gltf": {".bin"}}

# 読み込みの前後で数える、bpy.data のデータブロックの種類
_TRACKED = ("objects", "materials", "images", "meshes", "armatures", "actions", "collections", "lights", "cameras")


def snapshot() -> dict[str, set[str]]:
    """いまある、数える種類のデータブロックの名前（``new_since`` / ``remove_created`` に渡す）。"""
    return {name: {d.name for d in getattr(bpy.data, name)} for name in _TRACKED}


def new_since(before: dict[str, set[str]]) -> dict[str, list]:
    """``before`` の後に作られたデータブロック（種類ごと）。"""
    result = {}
    for name in _TRACKED:
        coll = getattr(bpy.data, name)
        result[name] = [d for d in coll if d.name not in before[name]]
    return result


def remove_created(before: dict[str, set[str]]) -> None:
    """``before`` の後に作られたデータブロックを消す（読み込みに失敗したときの片付け）。"""
    new = new_since(before)
    ids = [d for name in _TRACKED for d in new[name]]
    if ids:
        bpy.data.batch_remove(ids)


def model_objects(objects: list[bpy.types.Object]) -> list[ModelObject]:
    """読み込んだモデルのオブジェクトと、最上位からの名前の並び（``model_ids.object_claims`` に渡す形）。

    ボーンの子になっているオブジェクトは、Unity ではボーンの GameObject の下にある。Blender のアーマチュアの
    オブジェクトが FBX のノードとは限らないので、それを含む並びと含まない並びの両方を出す。
    """
    members = set(objects)
    cache: dict[bpy.types.Object, list[tuple[str, ...]]] = {}

    def paths(obj) -> list[tuple[str, ...]]:
        found = cache.get(obj)
        if found is not None:
            return found
        cache[obj] = [(obj.name,)]  # 親子が循環していても止まるようにする
        parent = obj.parent if obj.parent in members else None
        if parent is not None:
            above = paths(parent)
            if obj.parent_type == "BONE" and obj.parent_bone and parent.type == "ARMATURE":
                bones: list[str] = []
                bone = parent.data.bones.get(obj.parent_bone)
                while bone is not None:
                    bones.append(bone.name)
                    bone = bone.parent
                chain = tuple(reversed(bones))
                above = [p + chain for p in above] + [p[:-1] + chain for p in above]
            cache[obj] = [p + (obj.name,) for p in above]
        return cache[obj]

    return [
        ModelObject(
            o.name, paths(o), o.type == "MESH", o.parent.name if o.parent in members else None, o.type == "ARMATURE"
        )
        for o in objects
    ]


def is_removed(datablock) -> bool:
    try:
        _ = datablock.name
    except ReferenceError:
        return True
    return False


def adopt_into_collection(new: dict[str, list], scene, collection) -> None:
    """インポーターがシーンのルートコレクションに直接入れたオブジェクト・コレクションをパッケージ用コレクションに移す。

    VRM add-on はアクティブコレクションを無視してルートに入れ、コライダー用のコレクションも作る。
    """
    root = scene.collection
    for coll in new["collections"]:
        if coll is collection or coll.name not in root.children:
            continue
        root.children.unlink(coll)
        if coll.name not in collection.children:
            collection.children.link(coll)
    for obj in new["objects"]:
        if obj.name in root.objects:
            root.objects.unlink(obj)
            if obj.name not in collection.objects:
                collection.objects.link(obj)


def operator_available(module: str, name: str) -> bool:
    """``bpy.ops.<module>.<name>`` が登録されているか。

    ``hasattr(bpy.ops.<module>, name)`` はどんな名前でも True になり、C で定義されたオペレーター
    （wm.collada_import など）は ``bpy.types`` にも現れないので、``dir`` で登録済みの名前を見る。
    """
    return name in dir(getattr(bpy.ops, module))


def vrm_addon_available() -> bool:
    """VRM add-on（import_scene.vrm）が登録されているか。"""
    return operator_available("import_scene", "vrm")


def sidecar_guids(pkg: UnityPackage, model: AssetEntry) -> list[str]:
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


def _import_fbx(path: Path, opts: ImportOptions, scale: float = 1.0) -> None:
    """FBX を読み込む。既定は新しいインポーター（wm.fbx_import。Blender 4.5 で追加）で、Legacy を選んだときだけ Python 製のもの。

    bpy.ops の hasattr は常に True なので、インポーターの有無の判定には使えない（最小版の 4.5 にはどちらもある）。
    """
    kwargs = dict(
        filepath=str(path),
        global_scale=opts.global_scale * scale,
        use_anim=opts.use_anim,
        ignore_leaf_bones=opts.ignore_leaf_bones,
        use_custom_normals=True,
    )
    if opts.fbx_importer in ("AUTO", "NEW"):
        operator = bpy.ops.wm.fbx_import
    else:
        operator = bpy.ops.import_scene.fbx
        kwargs["use_image_search"] = False  # Legacy だけにある設定（画像は .mat から読むので探させない）
    kwargs.update(opts.extra_fbx_kwargs)
    result = operator(**kwargs)
    if "FINISHED" not in result:
        raise RuntimeError(f"FBX import failed for {path.name}: {result}")


def _import_blend(path: Path, collection: bpy.types.Collection) -> None:
    """同梱 .blend の全オブジェクトを append してコレクションに入れる。"""
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        data_to.objects = list(data_from.objects)
    for obj in data_to.objects:
        if obj is not None:
            collection.objects.link(obj)


def delegate_to_vrm_addon(model: AssetEntry, opts: ImportOptions) -> bool:
    return model.ext == ".vrm" and opts.use_vrm_addon and vrm_addon_available()


def _legacy_import_scale(path: Path, model: AssetEntry) -> float:
    """Unity 4 以前（metaData）のモデルを Unity と同じ大きさで読むための倍率（``ModelImporterInfo.import_scale``。#233）。"""
    if model.legacy_meta is None:
        return 1.0
    try:
        info = ModelImporterInfo.from_meta(model.legacy_meta)
    except ValueError:
        return 1.0
    return info.import_scale(read_unit_scale(path))


def import_model_file(path: Path, model: AssetEntry, opts: ImportOptions, collection, report: ImportReport) -> bool:
    """モデルを読み込む。マテリアルを外部インポーターに任せた（こちらで組み直さない）場合は True。"""
    ext = model.ext
    if ext == ".fbx":
        _import_fbx(path, opts, _legacy_import_scale(path, model))
        return False
    if ext == ".obj":
        # Unity と同じくグループ（g）ごとに分ける。o の名前は Unity が使わないので分けない（#214）
        result = bpy.ops.wm.obj_import(
            filepath=str(path), global_scale=opts.global_scale, use_split_objects=False, use_split_groups=True
        )
    elif ext == ".vrm" and delegate_to_vrm_addon(model, opts):
        # VRM add-on が MToon・Humanoid・スプリングボーンまで再現するので、マテリアルも add-on のものを使う。
        # use_addon_preferences=True で利用者の add-on 設定（テクスチャ展開先など）に従う
        result = bpy.ops.import_scene.vrm(filepath=str(path), use_addon_preferences=True)
        if "FINISHED" not in result:
            raise RuntimeError(f"VRM add-on import failed for {path.name}: {result}")
        return True
    elif ext in (".gltf", ".glb", ".vrm"):
        if ext == ".vrm" and opts.use_vrm_addon:
            report.warn(
                f"VRM add-on is not installed; {model.name} was imported with the glTF importer "
                "and MToon materials were rebuilt from .mat files (install the 'VRM format' add-on for full fidelity)"
            )
        # .vrm は glTF バイナリなので標準の glTF インポーターで読める（VRM 拡張は無視される）
        result = bpy.ops.import_scene.gltf(filepath=str(path))
    elif ext == ".dae":
        result = bpy.ops.wm.collada_import(filepath=str(path))
    elif ext == ".blend":
        _import_blend(path, collection)
        return False
    else:
        raise RuntimeError(f"unsupported model format: {model.pathname}")
    if "FINISHED" not in result:
        raise RuntimeError(f"import failed for {path.name}: {result}")
    return False


def submesh_order_of(obj) -> list[int]:
    """Unity のサブメッシュ順（ポリゴンで最初に使われた順）に並べたスロット番号。"""
    polygons = obj.data.polygons
    indices = [0] * len(polygons)
    polygons.foreach_get("material_index", indices)
    return submesh_slot_order(indices, len(obj.material_slots))
