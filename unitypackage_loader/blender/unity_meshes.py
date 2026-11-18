"""パッケージの Unity の Mesh（``.asset`` と、prefab・シーンに埋め込んだもの）から Blender のメッシュを作る（Issue #216）。

Mesh の読み取りは ``core/unity_mesh.py``。1 回の読み込みの中で、同じ Mesh は 1 つの Blender のメッシュを共有し、
マテリアルはオブジェクトのスロット（``link = "OBJECT"``）に付ける（同じ Mesh を別の .mat で使うため。組み込みメッシュと同じ）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import bpy

from ..core.package import PackageError
from ..core.unity_binary import load_documents
from ..core.unity_mesh import CLASS_MESH, MeshError, read_mesh

if TYPE_CHECKING:
    from .importer import _ImportSession


class SessionMeshes:
    """1 回の読み込みで作った Unity の Mesh のメッシュ（(アセットの GUID, fileID) ごと）。"""

    def __init__(self, session: _ImportSession):
        self.session = session
        self._bodies: dict[str, dict[int, dict] | None] = {}  # アセットの GUID → fileID → Mesh の中身
        self._meshes: dict[tuple[str, int], bpy.types.Mesh | None] = {}

    def prefetch(self, guids: Iterable[str]) -> None:
        """使うアセットをまとめて読む（キャッシュに無い .asset を 1 回の再走査で読む）。"""
        wanted = {g for g in guids if g not in self._bodies}
        if not wanted:
            return
        try:
            data = self.session.pkg.read_assets(wanted)
        except PackageError as exc:
            self.session.report.warn(f"could not read mesh assets: {exc}")
            data = {}
        for guid in wanted:
            raw = data.get(guid)
            if raw is None:
                self._bodies[guid] = None
                continue
            try:
                self._bodies[guid] = {
                    doc.file_id: doc.body for doc in load_documents(raw) if doc.class_id == CLASS_MESH and isinstance(doc.body, dict)
                }
            except ValueError as exc:
                self.session.report.warn(f"could not read meshes in {self._name(guid)}: {exc}")
                self._bodies[guid] = None

    def _name(self, guid: str) -> str:
        entry = self.session.pkg.get(guid)
        return entry.pathname if entry is not None else guid

    def mesh(self, guid: str, file_id: int) -> bpy.types.Mesh | None:
        """Mesh の Blender のメッシュ。読めなければ警告して None（同じ Mesh で何度も警告しない）。"""
        key = (guid, file_id)
        if key in self._meshes:
            return self._meshes[key]
        self.prefetch([guid])
        bodies = self._bodies.get(guid) or {}
        body = bodies.get(file_id)
        result = None
        if body is None:
            if self._bodies.get(guid) is not None:
                self.session.report.warn(f"mesh {file_id} is not in {self._name(guid)}")
        else:
            try:
                result = _make_mesh(read_mesh(body))
                result["unity_mesh_asset"] = self._name(guid)
                result["unity_mesh_file_id"] = str(file_id)
            except MeshError as exc:
                self.session.report.warn(f"could not read a mesh in {self._name(guid)}: {exc}")
        self._meshes[key] = result
        return result


def _make_mesh(data) -> bpy.types.Mesh:
    mesh = bpy.data.meshes.new(data.name)
    faces = [tri for sub in data.submeshes for tri in sub]
    mesh.from_pydata(data.vertices, [], faces)
    indices = [i for i, sub in enumerate(data.submeshes) for _ in sub]
    mesh.polygons.foreach_set("material_index", indices)
    if data.uvs is not None:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        uv_layer.data.foreach_set("uv", [c for face in faces for v in face for c in data.uvs[v]])
    for _ in data.submeshes:
        mesh.materials.append(None)
    mesh.update()
    if data.normals is not None:
        mesh.shade_smooth()
        mesh.normals_split_custom_set_from_vertices(data.normals)
    return mesh


def make_mesh_object(name: str, mesh: bpy.types.Mesh, materials: list) -> bpy.types.Object:
    """Mesh のオブジェクト。サブメッシュ i のスロットに ``materials[i]`` を付ける（無ければ空のまま）。"""
    obj = bpy.data.objects.new(name or mesh.name, mesh)
    for slot, material in zip(obj.material_slots, materials):
        slot.link = "OBJECT"
        slot.material = material
    obj["unity_mesh_asset"] = mesh.get("unity_mesh_asset", "")
    return obj
