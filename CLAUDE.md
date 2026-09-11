# CLAUDE.md — unitypackage_loader 開発ガイド

このリポジトリは、`.unitypackage` を Blender に直接読み込む Extension「Unity Package Importer」の開発用です。
新しいセッションで作業を始める前に、このファイルと `DESIGN.md` に目を通してください。
非公開の補足（検証用データの詳細、ローカル環境）は `CLAUDE.local.md`（gitignore 対象）にあります。

## 目的とスコープ

- メッシュ（アーマチュア・シェイプキー込み）に、Unity 側のマテリアル設定とテクスチャを反映した状態で読み込む。
- Unity と Blender のマテリアルは 1:1 に対応しないので「参考にできる値だけ」を反映し、残りはカスタムプロパティに保存する。
- シェーダー本体、C#、アニメーション、prefab 階層、Expression メニュー等は読み込まない。

## 設計の要点（詳細は DESIGN.md）

- **2 段変換**: `.mat` → `UnityMaterial`（生データ）→ `NormalizedMaterial`（シェーダー非依存の中間表現）→ Blender ノード。
  シェーダーごとの差は `core/profiles/` のプロファイルに閉じ込める（lilToon / MToon / Poiyomi / Standard・URP・HDRP / generic）。
  プロファイル選択は GUID 表（`shader_guids.json`）→ プロパティ指紋 → generic の順。
- **メッシュとマテリアルの対応付け**（`core/mapping.py`）:
  1. モデル `.meta` の externalObjects（完全一致 → 連番サフィックス除去）
  2. `.mat` 名との一致（同名複数ならモデルに近いフォルダ優先）
  3. prefab の Renderer.m_Materials（同名 GameObject の同じスロット）
  さらに、prefab がスロットごとに別の .mat を指す場合は **スロット単位で Blender マテリアルを分割**する（`blender/importer.py`）。
- **マテリアルモード**: Auto（トゥーン系 → Toon ノードグループ、PBR 系 → Principled）/ Principled / Toon / Unlit / Names Only。
  Toon は `blender/toon_group.py` の `UnityToon`（Shader to RGB を使うため EEVEE 向け）。
- **カスタムプロパティ**: `unity_material_guid` / `unity_shader_*` / `unity_props`（extras JSON）/ `unity_normalized`（中間表現 JSON）。
  画像には `unity_guid` 等。これにより元パッケージ無しで別モードに再構築できる（`operators/rebuild_material.py`）。
- **パッケージ読み取り**（`core/package.py`）: tar.gz を 1 度走査して索引化し、必要な GUID だけ 2 度目の走査で展開する。
  Unity YAML は依存無しの専用パーサー（`core/unity_yaml.py`。Blender 同梱 Python に PyYAML は無い）。

## ディレクトリ構成

```
unitypackage_loader/      Extension 本体（blender_manifest.toml、Blender 4.2 以降の Extension 形式）
  core/                   bpy 非依存。単体テスト可能。bpy を import しないこと
  blender/                bpy 依存（importer / materials / textures / toon_group / outline）
  operators/              File > Import、モデル選択ダイアログ、再構築、アウトライン
  ui/                     Preferences、サイドバー "Unity Package" タブ
tests/                    unittest（bpy 不要）＋ Blender 上の統合テスト＋合成パッケージ生成
tools/                    GitHub Pages 用 index.html 生成
.github/workflows/        Pages への Extension Repository 公開
_local/                   検証用データ置き場（gitignore。詳細は CLAUDE.local.md）
```

## 開発の進め方（守ること）

- **コミットは作業の小さな単位ごと**に行う。メッセージは日本語、先頭に `feat:` `fix:` `docs:` `test:` `refactor:` `chore:` `ci:` を付け、
  本文に変更内容と理由を書く。各コミット時点で Extension が読み込める状態を保つ。
- **追跡ファイルに検証用アセット固有の情報を書かない。** アセット名、ファイル名、メッシュ・マテリアル・テクスチャ名、
  頂点数やファイルサイズなどの識別につながる数値は、ドキュメント・コード・コメント・テスト・コミットメッセージのいずれにも入れない。
  検証用データと期待値（`_local/expectations.json`）は `_local/` に置く。コミット前に `git diff --cached` と履歴を検索して確認する。
  コミットしてよいフィクスチャは手書きの合成データ（`tests/make_synthetic_package.py` の生成物と `tests/expectations_synthetic.json`）だけ。
- 実装を変えたら **単体テストと統合テストの両方**を回し、結果の行を実際に確認する（shell の `set -e` はこの環境では当てにならない）。
- Blender API は 4.2 以降を前提にする（`surface_render_method`、`ShaderNodeMix`、Extension manifest、`ImportHelper.invoke_popup`、FileHandler）。
  新 FBX インポーター `wm.fbx_import` を優先し、無ければ `import_scene.fbx`。
- ドキュメント（README.md / DESIGN.md）は実装と乖離させない。仕様を変えたら同じ作業内で更新する。

## コマンド

```sh
# 単体テスト（bpy 不要）
python3 -m unittest discover -s tests -t .

# 合成パッケージの生成と統合テスト（実在アセット不要）
blender -b --factory-startup --python tests/make_synthetic_package.py
blender -b --factory-startup --python tests/integration_import.py -- _local/synthetic_multi.unitypackage tests/expectations_synthetic.json

# 手元の実パッケージでの統合テスト（_local/expectations.json の "package" キーで対象を指定）
blender -b --factory-startup --python tests/integration_import.py

# 配布用 zip / Extension Repository の index.json
blender -b --factory-startup --command extension build --source-dir unitypackage_loader --output-dir dist
blender -b --factory-startup --command extension server-generate --repo-dir=site
```

開発中は `unitypackage_loader/` を Blender の `extensions/user_default/` に symlink して使う（README 参照）。
Blender MCP が接続されている場合は、`addon_utils.disable` → `sys.modules` から `bl_ext.<repo>.unitypackage_loader*` を削除 →
`addon_utils.enable` で再読み込みし、実際にインポートしてスクリーンショットで確認する。

## リリース

- バージョンは `unitypackage_loader/blender_manifest.toml` の `version`（semantic versioning。開発版は `1.0.0-dev.N`）。
- main への push で `.github/workflows/pages.yml` が zip と `index.json` を生成し GitHub Pages に公開する。
