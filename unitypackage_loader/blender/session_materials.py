"""1 回の読み込みの中でのマテリアルの組み立て（``_ImportSession`` の ``materials``）。

インポーターが作ったマテリアルを .mat に対応付けて組み直し、prefab がスロットごとに指す .mat を割り当てる。同じ .mat は、
この回の読み込みの中で 1 つの Blender マテリアルを共有する（別のモデル同士、同じモデルの読み直し、1 つのモデルの複数の
スロット、prefab のスロット分割のどれでも。#77）。以前は ``_ImportSession`` のメソッドだった（#198）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import bpy

from ..core.mapping import fully_replaced_materials, resolve_materials, slot_assignments
from ..core.material import NormalizedMaterial, UnityMaterial
from ..core.meta import TextureImporterInfo, strip_numeric_suffix
from ..core.package import UnityPackage
from ..core.prefab import RendererMaterials
from ..core.report import ImportReport, MaterialReport
from . import materials as mat_builder
from .log import log_exception
from .model_import import is_removed, submesh_order_of
from .options import ImportOptions
from .prepare import ModelSummary, texture_info
from .textures import load_image

_DECAL_ROUGHNESS = 0.8  # デカールの板のつや（下の面の値は分からないので、つや消し寄りにする。#165）


class TextureImages(dict):
    """テクスチャの GUID → 読み込んだ画像（読めなければ None）。

    まだ読み込んでいないパッケージのテクスチャを引かれたら ``missed`` に覚える。読み込む前の予測から漏れたもので、
    最後にまとめて展開してから、引いたマテリアル（``pending``）を組み直す（#164）。
    """

    def __init__(self, pkg: UnityPackage):
        super().__init__()
        self._pkg = pkg
        self.missed: set[str] = set()
        self.recent: set[str] = set()  # 直前の組み立てで引かれた、読み込んでいないテクスチャ
        # (マテリアル, .mat の GUID, レポートの行, 組んだときの中間表現の表。デカール用は半透明にしたもの)
        self.pending: list[tuple[bpy.types.Material, str, MaterialReport, dict]] = []

    def get(self, guid, default=None):
        if guid not in self and self._pkg.get(guid) is not None:
            self.missed.add(guid)
            self.recent.add(guid)
        return super().get(guid, default)


class SessionMaterials:
    """1 回の読み込みで組んだマテリアルと、読み込んだ画像。"""

    def __init__(
        self, pkg: UnityPackage, unity_mats: dict[str, UnityMaterial], normalized: dict[str, NormalizedMaterial],
        opts: ImportOptions, report: ImportReport,
    ):
        self.pkg = pkg
        self.unity_mats = unity_mats
        self.normalized = normalized
        self.opts = opts
        self.report = report
        self.images = TextureImages(pkg)
        self.tex_infos: dict[str, TextureImporterInfo] = {}
        self.build_opts = mat_builder.MaterialBuildOptions(
            mode=opts.material_mode,
            force_opaque=opts.force_opaque,
            backface_culling=opts.backface_culling,
            use_normal_maps=opts.use_normal_maps,
            use_emission=opts.use_emission,
            store_props=opts.store_props,
        )
        self.built_by_guid: dict[str, bpy.types.Material] = {}  # .mat の GUID → この回で組んだマテリアル（#77）
        self.decal_materials: dict[str, bpy.types.Material | None] = {}  # .mat の GUID → デカール用に組んだマテリアル
        # スロットが無く、prefab・シーンの割り当ても無かったメッシュ（モデル名, オブジェクト）。使われていない部品として
        # 後で消すものもあるので、警告は読み込みの最後に、残ったものにだけ出す（``warn_slotless``。#213）
        self.slotless: list[tuple[str, bpy.types.Object]] = []

    # --- 画像 ---

    def load_textures(self, guids: set[str], paths: dict[str, Path], written: set[str]) -> None:
        """展開済みのテクスチャを画像として読み込み、``images`` に入れる（読めなければ None）。"""
        loaded: dict[str, bpy.types.Image | None] = {}
        for guid in sorted(guids):
            entry = self.pkg.get(guid)
            info = texture_info(entry, self.report.warn)
            self.tex_infos[guid] = info
            path = paths.get(guid)
            image = load_image(path, info, pack=self.opts.pack_images, refresh=guid in written) if path else None
            loaded[guid] = image
            if image is None:
                self.report.warn(f"could not load texture {entry.pathname} (unsupported format?)")
            else:
                self.report.images.append(image.name)
        self.images.update(loaded)
        mat_builder.tag_images(loaded, self.tex_infos)

    def take_pending(self) -> tuple[list, list[str]]:
        """組み立てで足りなかったテクスチャがあったマテリアル（組み直す分）と、まだ読み込んでいないそのテクスチャの GUID（#164）。"""
        pending, self.images.pending = self.images.pending, []
        return pending, sorted(self.images.missed - self.images.keys())

    def rebuild_pending(self, pending: list) -> None:
        """``take_pending`` で受け取ったマテリアルを、テクスチャを読み込んだ後に組み直す。"""
        seen: set[int] = set()
        for bmat, guid, mrep, normalized in pending:
            if is_removed(bmat) or id(mrep) in seen:
                continue
            seen.add(id(mrep))
            self.build_and_report(bmat, guid, mrep, normalized)

    def forget_removed(self) -> None:
        """消されたマテリアル（読み込みに失敗したモデルの片付けで消したもの）を、組み立て済みの表から外す。"""
        for guid in [g for g, m in self.built_by_guid.items() if is_removed(m)]:
            del self.built_by_guid[guid]

    # --- 組み立て ---

    def new_material(self, guid: str, method: str, fbx_name: str = "", normalized: dict | None = None) -> bpy.types.Material:
        """.mat から新しい Blender マテリアルを作って組み、レポートに行を足す（``normalized`` はデカール用などの別の中間表現）。"""
        mat = bpy.data.materials.new(self.unity_mats[guid].name or guid[:8])
        mrep = MaterialReport(blender_name=mat.name, fbx_name=fbx_name, guid=guid, method=method)
        self.report.materials.append(mrep)
        self.build_and_report(mat, guid, mrep, normalized)
        return mat

    def build_and_report(self, bmat, guid: str, mrep: MaterialReport, normalized: dict | None = None) -> None:
        """.mat の中間表現でマテリアルのノードを組み、結果をレポートの行に書く。

        ``normalized`` を渡さなければこの回の中間表現の表を使う。読み込んでいないテクスチャを引いたら、警告を出さずに
        ``images.pending`` に回し、最後に展開してから組み直す（``fill_missed_textures``。#164）。
        """
        normalized = self.normalized if normalized is None else normalized
        norm = normalized[guid]
        mrep.describe(norm)
        self.images.recent.clear()
        try:
            mode, warnings = mat_builder.build_material(bmat, norm, self.images, self.tex_infos, self.build_opts)
        except Exception as exc:  # noqa: BLE001 - 1 マテリアルの失敗で全体を止めない
            mrep.warnings.append(f"node build failed: {exc!r}")
            self.report.warn(f"material {bmat.name!r}: node build failed: {exc!r}")
            log_exception(f"node build failed for material {bmat.name!r}")
            return
        if self.images.recent:
            # 読み込んでいないテクスチャがあった。警告は出さずに、展開してから組み直す（fill_missed_textures）
            self.images.pending.append((bmat, guid, mrep, normalized))
            return
        mrep.mode = mode
        mrep.warnings.extend(warnings)
        for w in warnings:
            if w.startswith(("shader table entry", "shader approximation:")):
                self.report.warn(w)  # シェーダー単位の注意なので 1 回だけ
            else:
                self.report.warn(f"material {bmat.name!r}: {w}")
        mrep.textures = [
            f"{role}={self.pkg.get(t.guid).pathname if self.pkg.get(t.guid) else t.guid}"
            for role, t in (
                ("base", norm.base_color_tex),
                ("normal", norm.normal_tex),
                ("emission", norm.emission_tex),
                ("metallic", norm.metallic_tex),
            )
            if t is not None
        ]

    def split_slots_by_prefab(self, objects, assignments, resolution) -> int:
        """(オブジェクト, スロット) ごとに prefab の .mat を割り当てる。割り当てたスロット数を返す。

        モデルがマテリアルを持たないとスロットは 0 個になるが、Unity では Renderer の ``m_Materials[0]`` で描かれる。
        このときはスロット 0 を作ってから割り当てる（#102）。
        """
        by_name = {o.name: o for o in objects if o.type == "MESH"}
        count = 0
        for (obj_name, index), guid in assignments.items():
            obj = by_name.get(obj_name)
            if obj is None:
                continue
            if index >= len(obj.material_slots):
                if index != 0:  # index が 0 で範囲外なら、スロットが 1 つも無いメッシュ
                    continue
                obj.data.materials.append(None)
            slot = obj.material_slots[index]
            current = slot.material
            current_guid = resolution[current.name].guid if current is not None and current.name in resolution else None
            mat = self.built_by_guid.get(guid)
            if current_guid == guid or (current is not None and current is mat):
                continue
            if mat is None:
                mat = self.built_by_guid[guid] = self.new_material(guid, "prefab-split", current.name if current is not None else "")
            slot.material = mat
            count += 1
        return count

    def material_for(self, guids: list[str | None]) -> bpy.types.Material | None:
        """Renderer の ``m_Materials[0]`` の .mat から組んだマテリアル（この回で組み済みなら共有する）。

        組み込みメッシュはサブメッシュが 1 つなので、先頭だけを使う。パッケージに無い .mat（Unity の既定のマテリアルなど）なら None。
        """
        guid = guids[0] if guids else None
        if not guid or guid not in self.unity_mats:
            return None
        mat = self.built_by_guid.get(guid)
        if mat is None:
            mat = self.built_by_guid[guid] = self.new_material(guid, "builtin-mesh")
        return mat

    def decal_material_for(self, guid: str) -> bpy.types.Material | None:
        """デカールの板に貼るマテリアル（ベースのテクスチャのアルファで半透明にする。この回で組み済みなら共有する。#165）。

        デカールのマテリアルは不透明として保存されている（ベースのテクスチャのアルファは Decal のシェーダーが投影するときに
        不透明度として使う）ので、半透明（blend）でアルファをテクスチャから取る中間表現から、メッシュ用とは別に組む。
        URP の標準の Decal シェーダーは既定で金属感・つや・AO を変えない（Affect MAOS が OFF）ので、Lit から切り替えたときに残る
        ``_MetallicGlossMap`` や ``_Smoothness`` は使わず、金属感 0・つや消し寄り（下の面の値は分からない）にする。
        パッケージに無い .mat や、ベースのテクスチャを持たないもの（法線だけのデカールなど）は None。
        """
        if guid in self.decal_materials:
            return self.decal_materials[guid]
        norm = self.normalized.get(guid)
        mat = None
        if norm is not None and norm.base_color_tex is not None:
            decal_norm = replace(
                norm, alpha_mode="blend", alpha_from_texture=True, smoothness_from_albedo=False, smoothness_from_specular=False,
                metallic=0.0, metallic_tex=None, roughness=_DECAL_ROUGHNESS, specular_tex=None, specular_color=None, occlusion_tex=None,
            )
            mat = self.new_material(guid, "decal", normalized={guid: decal_norm})
            mat["unity_decal"] = True
        self.decal_materials[guid] = mat
        return mat

    def assign_materials(
        self, summary: ModelSummary, prefab_table: dict[str, RendererMaterials], new: dict[str, list], delegated: bool,
        existing_materials: dict[str, bpy.types.Material],
    ) -> None:
        """インポーターが作ったマテリアルを .mat に対応付けて組み直し、prefab がスロットごとに指す .mat を割り当てる。"""
        model = summary.entry
        objects = new["objects"]
        mesh_objects = [o for o in objects if o.type == "MESH"]
        object_slots = {o.name: [slot.material.name if slot.material else "" for slot in o.material_slots] for o in mesh_objects}
        # prefab の m_Materials は Unity のサブメッシュ順で、Blender のスロット順とは限らない
        submesh_order = {o.name: submesh_order_of(o) for o in mesh_objects}
        resolution = resolve_materials(
            [m.name for m in new["materials"]], summary.info, self.unity_mats, model.pathname, prefab_table, object_slots, submesh_order
        )
        assignments = slot_assignments(object_slots, prefab_table, self.unity_mats, submesh_order)
        # マテリアルを 1 つも持たないモデルは、prefab の割り当てしか手掛かりが無い（#102）
        for obj in mesh_objects:
            if not object_slots[obj.name] and (obj.name, 0) not in assignments:
                self.slotless.append((model.name, obj))
        # prefab がすべてのスロットで別の .mat に差し替えるマテリアルは、組み立てても捨てるだけなので組まない
        replaced = fully_replaced_materials(object_slots, assignments, resolution)
        # 同梱 .blend の KEEP と VRM add-on 委譲では、インポーターが作ったマテリアルをそのまま使う
        keep = delegated or (model.ext == ".blend" and self.opts.blend_materials == "KEEP")
        remaining: list[bpy.types.Material] = []  # 削除せずに残した、インポーターが作ったマテリアル
        for bmat in new["materials"]:
            res = resolution[bmat.name]
            mrep = MaterialReport(blender_name=bmat.name, fbx_name=bmat.name, guid=res.guid, method=res.method)
            self.report.materials.append(mrep)
            if keep:
                self.keep_material(bmat, res.guid, mrep, delegated)
                remaining.append(bmat)
            elif bmat.name in replaced:
                if res.guid:
                    mrep.describe(self.normalized[res.guid])
                remaining.append(bmat)  # 差し替えの後、使われなくなったところで "replaced" にして消す
            else:
                self.rebuild_material(bmat, res, mrep, objects, existing_materials, remaining)
        if keep:
            return
        # インポーターが作った画像（glTF の埋め込み画像など）は .mat から組み直した時点で不要になる
        for image in new["images"]:
            if image.users == 0:
                bpy.data.images.remove(image)
        # prefab がスロットごとに別の .mat を指している場合は、そのスロットだけ別マテリアルに差し替える
        if assignments:
            split = self.split_slots_by_prefab(objects, assignments, resolution)
            if split:
                self.report.split_slots += split
                self.drop_replaced_materials(remaining)

    def warn_slotless(self) -> None:
        """スロットも割り当ても無く、読み込みの最後までマテリアルの無いまま残ったメッシュを警告する。"""
        for model_name, obj in self.slotless:
            if is_removed(obj) or any(slot.material for slot in obj.material_slots):
                continue
            self.report.warn(
                f"{model_name}: mesh {obj.name!r} has no material slots and no prefab assignment; it is left without a material"
            )
        self.slotless = []

    def keep_material(self, bmat, guid: str | None, mrep: MaterialReport, delegated: bool) -> None:
        """インポーター（VRM add-on・同梱 .blend）が作ったマテリアルをそのまま使い、Unity の情報だけを残す。"""
        mrep.method = "delegated" if delegated else "kept"
        if guid:
            norm = self.normalized[guid]
            mrep.describe(norm)
            if self.opts.store_props:
                mat_builder.store_props(bmat, norm)

    def rebuild_material(self, bmat, res, mrep: MaterialReport, objects, existing_materials, remaining: list) -> None:
        """インポーターが作ったマテリアル 1 つを .mat から組み直すか、同じ .mat のマテリアルに付け替える。

        ファイルに既にある同名のマテリアルを使う設定ならそれを使い、無ければこの回で組み立て済みのものを共有する（#77）。
        どちらも無ければ組む。組み直して残したものは ``remaining`` に足す。
        """
        if res.warning:
            mrep.warnings.append(res.warning)
            self.report.warn(f"material {bmat.name!r}: {res.warning}")
        if self.opts.reuse_existing:
            existing = existing_materials.get(strip_numeric_suffix(bmat.name))
            if existing is not None and existing is not bmat:
                _replace_material(objects, bmat, existing)
                bpy.data.materials.remove(bmat)
                mrep.method = "reused"
                mrep.blender_name = existing.name
                return
        if res.guid is None:
            remaining.append(bmat)
            mrep.warnings.append("no matching .mat in package; material left as imported")
            self.report.warn(f"material {bmat.name!r}: no matching .mat in package")
            return
        shared = self.built_by_guid.get(res.guid)
        if shared is None or shared is bmat:
            remaining.append(bmat)
            self.build_and_report(bmat, res.guid, mrep)
            self.built_by_guid.setdefault(res.guid, bmat)
        elif _named_after_mat(bmat, shared, self.unity_mats[res.guid]):
            remaining.append(bmat)
            self.take_over_shared(bmat, shared, res.guid, mrep, remaining)
        else:
            _replace_material(objects, bmat, shared)
            bpy.data.materials.remove(bmat)
            mrep.describe(self.normalized[res.guid])
            mrep.method = "shared"
            mrep.blender_name = shared.name

    def take_over_shared(self, bmat, shared, guid: str, mrep: MaterialReport, remaining: list) -> None:
        """共有するマテリアルの名前を、なるべく .mat の名前にする。

        先に組んだものが FBX 側の別の名前（prefab で解決したものなど）なら、.mat と同じ名前のこちらを組み、先のものの
        使用箇所をこちらへ付け替えて消す。
        """
        self.build_and_report(bmat, guid, mrep)
        old_name = shared.name
        shared.user_remap(bmat)
        for row in self.report.materials:
            if row is not mrep and row.blender_name == old_name:
                row.method, row.blender_name = "shared", bmat.name
        if shared in remaining:
            remaining.remove(shared)
        bpy.data.materials.remove(shared)
        self.built_by_guid[guid] = bmat

    def drop_unused_materials(self, materials) -> None:
        """使われていない部品を消して誰も使わなくなった、この回に組んだマテリアルを消し、レポートの行を "unused" にする（#221）。"""
        names = {mrep.blender_name for mrep in self.report.materials}
        # 同名の既存のマテリアルを使ったもの（reuse_existing）は、この回より前からあるので消さない
        names -= {mrep.blender_name for mrep in self.report.materials if mrep.method in ("reused", "kept", "delegated")}
        for bmat in materials:
            if is_removed(bmat) or bmat.users != 0 or bmat.name not in names:
                continue  # ほかのオブジェクトが使うもの・この回より前からあるもの（同名の再利用など）は残す
            for mrep in self.report.materials:
                if mrep.blender_name == bmat.name and mrep.method not in ("replaced", "shared", "unused"):
                    mrep.method = "unused"
            for guid in [g for g, m in self.built_by_guid.items() if m == bmat]:
                del self.built_by_guid[guid]
            bpy.data.materials.remove(bmat)

    def drop_replaced_materials(self, remaining: list) -> None:
        """prefab のスロットの差し替えで使われなくなった、インポーターが作ったマテリアルを消し、レポートの行を "replaced" にする。"""
        gone = [bmat for bmat in remaining if bmat.users == 0]
        if not gone:
            return
        names = {bmat.name for bmat in gone}
        for mrep in self.report.materials:
            if mrep.blender_name in names and mrep.method != "replaced":
                mrep.method = "replaced"
        # 組み立て済みの表からも外す。残すと、同じモデルを読み直したときに削除済みのマテリアルを使ってしまう
        for guid in [g for g, m in self.built_by_guid.items() if m.name in names and any(m == b for b in gone)]:
            del self.built_by_guid[guid]
        # 1 つずつ remove すると、そのたびに全データを調べるので、シーンでは何千回も呼んで重い。まとめて消す（#219）
        bpy.data.batch_remove(gone)


def _named_after_mat(candidate: bpy.types.Material, current: bpy.types.Material, umat: UnityMaterial) -> bool:
    """共有しているマテリアルより、``candidate`` のほうが .mat の名前に合うか（連番は外して比べる）。"""
    name = umat.name
    return bool(name) and strip_numeric_suffix(candidate.name) == name and strip_numeric_suffix(current.name) != name


def _replace_material(objects, old: bpy.types.Material, new: bpy.types.Material) -> None:
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material == old:
                slot.material = new
