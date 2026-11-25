"""インポートの設定（オペレーターのプロパティと Preferences から作る）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .materials import MODE_AUTO


@dataclass
class ImportOptions:
    """1 回の読み込みの設定。オペレーターのプロパティと同じ名前のものは ``operators/import_package.py`` が写す。"""

    models: str = "ASK"  # ASK / ALL / FIRST
    unit: str = "MODELS"  # 読み込む単位: SCENES / PREFABS / MODELS
    model_guids: list[str] | None = None  # 明示的に選ばれたモデル（ダイアログ経由）
    prefab_paths: list[str] | None = None  # 明示的に選ばれた prefab の pathname（ダイアログ経由）
    scene_paths: list[str] | None = None  # 明示的に選ばれたシーンの pathname（ダイアログ経由）
    scene_lights: bool = True  # シーンのライトを読み込む
    scene_cameras: bool = True  # シーンのカメラを読み込む
    scene_exposure: bool = True  # HDRP のシーンの Volume の露出を、そのシーンの HDRP のライトの強さに反映する（#130）
    scene_decals: bool = True  # シーンの DecalProjector を板にして読み込む（#165）
    scene_terrain: str = "513"  # シーンの Terrain: NONE / 129 / 257 / 513 / 1025 / FULL（1 辺の点の数の上限。#205）
    hide_lods: bool = True  # LODGroup の遠景用の段（LOD1 以降）を非表示にする（Scenes / Prefabs 単位）
    skip_unused_parts: bool = True  # prefab・シーンが使っていない FBX の部品を読み込まない（Scenes / Prefabs 単位。#152）
    arrange: str = "SIDE_BY_SIDE"  # prefab を複数読み込むときの並べ方: SIDE_BY_SIDE / STACK
    material_mode: str = MODE_AUTO
    force_opaque: bool = False
    backface_culling: bool = False  # Unity の _Cull に従って背面を隠すか（既定は両面を描く。#169）
    use_normal_maps: bool = True
    use_emission: bool = True
    reuse_existing: bool = False
    store_props: bool = True
    extract_mode: str = "BESIDE_BLEND"  # BESIDE_BLEND / CACHE / CUSTOM
    extract_path: str = ""
    pack_images: bool = False
    import_unreferenced: bool = False
    overwrite_extracted: bool = False
    max_extract_size: int = 0  # 1 回のインポートで展開する合計バイト数の上限（0 は無制限。Preferences から）
    fbx_importer: str = "AUTO"  # AUTO / NEW / LEGACY
    use_anim: bool = False
    import_clips: bool = True  # パッケージの .anim を Action にして NLA にミュートで並べる（#209）
    ignore_leaf_bones: bool = True
    global_scale: float = 1.0
    import_blend: bool = False  # 同梱 .blend を append するか（Python スクリプトを含み得るので既定 OFF）
    blend_materials: str = "KEEP"  # .blend 同梱モデル: KEEP（既存マテリアルを残す）/ REBUILD
    use_vrm_addon: bool = True  # .vrm は VRM add-on（extensions.blender.org の "VRM format"）があればそちらで読む
    outlines: bool = False  # Unity のアウトライン設定を Solidify で再現する
    outline_width_scale: float = 0.01
    shader_table_path: str = ""
    extra_fbx_kwargs: dict = field(default_factory=dict)
    package_label: str = ""  # パッケージのコレクションの名前（入れ子のパッケージを読むとき、外側の名前を付ける。#217）

    @property
    def terrain_points(self) -> int | None:
        """Terrain のメッシュの 1 辺の点の数の上限（0 は元の解像度）。読み込まないなら None。"""
        if self.scene_terrain == "NONE":
            return None
        if self.scene_terrain == "FULL":
            return 0
        try:
            return max(int(self.scene_terrain), 2)
        except ValueError:
            return 513
