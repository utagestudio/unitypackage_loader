# 統合テスト期待値ファイルの書式

`tests/integration_import.py` は、`_local/expectations.json`（gitignore 対象）に書かれた期待値と
実際のインポート結果を照合する。検証用パッケージも同じ `_local/` に置く。
全てのキーは省略可能で、書いたものだけ検証される。

```json
{
  "options": { "material_mode": "AUTO" },
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
      "node_types": ["ShaderNodeMixShader"]
    }
  ],
  "shape_keys": { "<オブジェクト名>": 0 }
}
```

| キー | 意味 |
|---|---|
| `options` | オペレーターに渡す追加プロパティ（`extract_mode` は常に一時ディレクトリへ上書きされる） |
| `objects.count` | レポート上の新規オブジェクト数（アーマチュア込み） |
| `materials.mapped` | .mat に対応付けできたマテリアル数 |
| `materials.methods` | 使われた解決手段の集合（`external` / `name` / `none` / `reused`） |
| `images.count` | 読み込まれた画像数 |
| `warnings_max` | 許容する警告数の上限 |
| `material_checks[].normal_image` | Unlit モードでは「Normal (unused)」ノードの画像も対象 |

実行:

```sh
blender -b --python tests/integration_import.py
blender -b --python tests/integration_import.py -- path/to/pkg.unitypackage path/to/expectations.json
```
