# 統合テスト期待値ファイルの書式

`tests/integration_import.py` は、`_local/expectations.json`（gitignore 対象）に書かれた期待値と
実際のインポート結果を照合する。検証用パッケージも同じ `_local/` に置く。
全てのキーは省略可能で、書いたものだけ検証される。

```json
{
  "package": "<_local/ 内のパッケージファイル名。省略時は引数、無ければ _local/ の最初の .unitypackage>",
  "options": { "material_mode": "AUTO" },
  "enable_addons": ["bl_ext.blender_org.vrm"],
  "objects": { "count": 0, "mesh_count": 0, "armature_count": 0 },
  "materials": {
    "count": 0,
    "mapped": 0,
    "families": ["liltoon"],
    "methods": ["external"]
  },
  "images": { "count": 0 },
  "warnings_max": 0,
  "material_checks": [
    {
      "name": "<Blender 上のマテリアル名>",
      "alpha_mode": "opaque | cutout | blend",
      "render_method": "DITHERED | BLENDED",
      "backface_culling": true,
      "mode": "UNLIT | PRINCIPLED | NAMES_ONLY",
      "shader_name": "<shader_guids.json の name>",
      "base_image": "<画像名>",
      "base_colorspace": "sRGB",
      "normal_image": "<画像名>",
      "normal_colorspace": "Non-Color",
      "emission_strength": 0.0,
      "emission_color": [1.0, 1.0, 1.0],
      "node_types": ["ShaderNodeMixShader"]
    }
  ],
  "shape_keys": { "<オブジェクト名>": 0 },
  "split_slots": 0,
  "submesh_materials": { "<オブジェクト名>": ["<マテリアル名>"] },
  "prefabs": { "count": 0 },
  "collections": {
    "<コレクション名>": { "objects": 0, "prefab": "<prefab の pathname>", "mat_files": ["<.mat のファイル名>"] }
  },
  "prefab_collections_overlap": false,
  "scenes": { "count": 0 },
  "placed_objects": [
    { "top": "<最上位の Empty の名前>", "object": "<オブジェクト名（連番を除く）>", "tip": [0.0, 0.0, 0.0], "hidden": false, "mat_files": ["<.mat のファイル名>"] }
  ],
  "shared_meshes": [
    { "objects": [{ "top": "<Empty>", "object": "<オブジェクト>" }], "shared": true }
  ]
}
```

| キー | 意味 |
|---|---|
| `package` | 検証に使うパッケージ（`_local/` 内のファイル名）。コマンドライン引数があればそちらが優先 |
| `options` | オペレーターに渡す追加プロパティ（`extract_mode` は常に一時ディレクトリへ上書きされる） |
| `enable_addons` | factory 設定の後に有効化する add-on のモジュール名。未導入ならテスト全体をスキップする（VRM add-on への委譲を検証する場合に使う） |
| `objects.count` | レポート上の新規オブジェクト数（アーマチュア込み） |
| `materials.mapped` | .mat に対応付けできたマテリアル数 |
| `materials.methods` | 使われた解決手段の集合（`external` / `name` / `prefab` / `none` / `reused` / `kept` / `delegated` / `shared` / `replaced` / `prefab-split`）。`shared` は同じ回に組み立て済みの同じ .mat のマテリアルを使った行 |
| `images.count` | 読み込まれた画像数 |
| `warnings_max` | 許容する警告数の上限 |
| `result` | オペレーターの結果（既定は `FINISHED`。何も読み込めずに失敗する場合は `CANCELLED`） |
| `errors` | レポートのエラー数（読み込めなかったモデル・シーン、続けられなかった失敗） |
| `leaves_nothing` | `true` なら、オブジェクト・コレクション・メッシュ・マテリアル・画像などの数がインポートの前と同じ（失敗したときに片付けられている） |
| `material_checks[].normal_image` | Unlit モードでは「Normal (unused)」ノードの画像も対象 |
| `material_checks[].emission_strength` / `emission_color` | Principled BSDF の Emission Strength と、リンクされていない Emission Color（RGB、小数 4 桁で比較） |
| `split_slots` | prefab の割り当てでマテリアルを差し替えたスロット数 |
| `submesh_materials` | オブジェクトのポリゴンで最初に使われた順（Unity のサブメッシュ順）に並べたマテリアル名。スロットの並びに依らず、ポリゴン群とマテリアルの対応を確かめる |
| `prefabs.count` | 読み込む単位 Prefabs で読み込んだ prefab の数（`options` に `"unit": "PREFABS"` を指定する） |
| `collections` | prefab ごとのコレクションの中身。`objects` はオブジェクト数（子コレクション込み）、`prefab` はコレクションの `unity_prefab`、`mat_files` はメッシュに付いたマテリアルの元の .mat のファイル名の集合（同じモデルを読み直すとマテリアル名に連番が付くため、名前ではなく元のファイルで比べる） |
| `scenes.count` | 読み込む単位 Scenes で読み込んだシーンの数（`options` に `"unit": "SCENES"` を指定する） |
| `placed_objects` | シーンで配置したオブジェクト。最上位の Empty（Unity の最上位の GameObject）の名前とオブジェクト名（`.001` などの連番を除く）で 1 つに絞る。`tip` は評価後のメッシュで重心から最も遠い頂点のワールド座標（誤差 0.001 まで）、`hidden` は `hide_get()`、`mat_files` はスロットのマテリアルの元の .mat のファイル名の集合 |
| `lights` / `cameras` | シーンから作ったライト・カメラ（オブジェクト名で探す）。`type`、`energy`、`spot_size`、`spot_blend`、`size`、`size_y`、`shape`、`use_shadow`、`use_custom_distance`、`cutoff_distance`、`use_temperature`、`temperature`、`lens`、`sensor_fit`、`ortho_scale`、`clip_end` はライト・カメラのデータの値（数値は 0.1% まで）。`direction` はオブジェクトの -Z のワールドでの向き、`location` はワールド座標、`hidden` は `hide_get()` |
| `scene_camera` | シーンのカメラになったオブジェクトの名前 |
| `light_count` / `camera_count` | レポートのライト・カメラの数 |
| `shared_meshes` | 挙げたオブジェクトがメッシュのデータを共有しているか（同じモデル・同じ割り当ての配置は複製で共有する） |
| `prefab_collections_overlap` | prefab ごとのコレクションの外形（XY）が重なっているか。並べる（`"arrange": "SIDE_BY_SIDE"`）なら `false`、原点に重ねる（`"STACK"`）なら `true` |

実行:

```sh
blender -b --python tests/integration_import.py
blender -b --python tests/integration_import.py -- path/to/pkg.unitypackage path/to/expectations.json
```
