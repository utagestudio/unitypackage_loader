# CLAUDE.md — unitypackage_loader 開発ガイド

このリポジトリは、`.unitypackage` を Blender に直接読み込む Extension「Unitypackage Importer」の開発用です。
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
  3. prefab の Renderer.m_Materials（同名 GameObject の同じサブメッシュ）
  さらに、prefab がスロットごとに別の .mat を指す場合は **スロット単位で Blender マテリアルを分割**する（`blender/importer.py`）。
  prefab の割り当ては 1・2 より優先する。表はモデル単位（Renderer のメッシュ参照の GUID で振り分け、`core/prefab.py`）で、
  候補が複数あるモデルはダイアログの行で prefab を選べる（既定はパス順の先勝ち統合）。Prefab Variant / ネストは元が prefab なら
  上書きを重ねる（fileID は PrefabInstance の fileID XOR 元の fileID）。元が FBX の上書きは未対応（Issue #31）。
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
  ui/                     Preferences、サイドバー "UPI" タブ
tests/                    unittest（bpy 不要）＋ Blender 上の統合テスト＋合成パッケージ生成
tools/                    GitHub Pages 用サイトの組み立て（build_site.py）、OGP 画像の元（og_card.html）、スクリーンショット撮影（shoot_screenshots.py）
web/                      紹介ページのソース（英語 index.html、日本語 ja/、assets/）。ビルド時に site/ へコピーし {{VERSION}} 等を埋める
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
- ドキュメント（README.md / README_ja.md / DESIGN.md）は実装と乖離させない。仕様を変えたら同じ作業内で更新する。
- **バージョン番号の運用**（`unitypackage_loader/blender_manifest.toml` の `version`）は下記「バージョン番号のルール」に従う。
  ソースコードに関わるコミットごとに `-dev.<dev>` を上げるので、コードを変えたコミットには manifest の変更も含める。

## バージョン番号のルール

形式は `<major>.<minor>.<fix>(-dev.<dev>)`。

- **major**: 大きな仕様変更を含むバージョンアップ。
- **minor**: 通常の機能開発によるバージョンアップ。
- **fix**: バグ修正など。
- **開発の始め方**: 着手前に、その開発が major / minor / fix のどれを上げるものかを検討し、先立って当該番号を上げて `-dev.1` を付ける
  （例: `1.0.0` から機能開発を始めるなら最初のコミットで `1.1.0-dev.1`）。以後、開発コミットごとに `-dev.<dev>` を 1 ずつ上げる。
- **main へのマージ**: ユーザーが機能開発の内容に問題ないと判断し main にマージすると決めた時点で、`-dev.<dev>` を外したバージョンにする
  （例: `1.1.0-dev.7` → `1.1.0`）。この判断はユーザーが行う。
- **上げない場合**: バージョンを変えるのはプログラムのソースコード（`unitypackage_loader/` 配下）に関わる改変のときだけ。
  GitHub Pages、README、DESIGN.md、CLAUDE.md、テストのみ、CI 設定のみといったドキュメント・周辺の改変では変更しない。

## コマンド

```sh
# 単体テスト（bpy 不要）
python3 -m unittest discover -s tests -t .

# 合成パッケージの生成と統合テスト（実在アセット不要）
blender -b --factory-startup --python tests/make_synthetic_package.py
blender -b --factory-startup --python tests/integration_import.py -- _local/synthetic_multi.unitypackage tests/expectations_synthetic.json
blender -b --factory-startup --python tests/integration_import.py -- _local/synthetic_multi.unitypackage tests/expectations_synthetic_noblend.json  # 同梱 .blend 既定 OFF

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

- バージョンは `unitypackage_loader/blender_manifest.toml` の `version`。付け方は上記「バージョン番号のルール」を参照。
- main への push で `.github/workflows/pages.yml` が zip と `index.json` を生成し、`web/` の紹介ページと一緒に GitHub Pages に公開する。
- 紹介ページのローカル確認: `blender ... extension build` と `server-generate` を `site/` に出したあと `python3 tools/build_site.py site <base_url>`。
  Google Tag Manager は、環境変数 `GTM_ID`（無ければリポジトリ直下の `.env`。gitignore 対象）に `GTM-XXXX` 形式の ID があるときだけ埋め込む。
  公開時はリポジトリの Settings > Secrets and variables > Actions > Variables の `GTM_ID` を `pages.yml` が渡す（未設定なら埋め込まない）。
  フッターの「アクセス解析と Cookie について」は `<!-- gtm-only -->` 〜 `<!-- /gtm-only -->` で囲んであり、GTM ID があるときだけ残る。GTM で入れるタグを増やしたら文面も見直す。
  OGP 画像は `tools/og_card.html` を Chrome でレンダリングして、英語は `web/assets/og.png`、日本語は `?lang=ja` を付けて `og-ja.png` に置く
  （コマンドは同ファイル冒頭。右側の前後比較は `hero-before.webp` / `hero-after.webp` を読むので、ヒーロー画像を差し替えたら作り直す）。
  使い道の画像は全画面のスクリーンショットから 3:2 で切り出し、1200×800 の WebP にする（1 枚目は `use-edit.webp`、2・3 枚目は F12 でレンダリングした画像から作った `use-render.webp` / `use-props.webp`）。
  背景や小物のアセットは、ライセンス上レンダリング画像の公開だけを行い、ファイル自体は `_local/` から出さない。
  ヒーローの読み込み前後（`hero-before.webp` / `hero-after.webp`）は、同じカメラで撮った 2 枚のスクリーンショット（`_local/shots/`）を
  同じ範囲で 4:5 に切り出したもの。スライダーで重ねるので、撮り直すときもカメラと切り出し範囲を揃えること。
  操作デモは `web/assets/demo.mp4`（音声なし・自動ループ）と、最後のコマから作った poster `demo-poster.webp`。録画の元ファイルは `_local/recording/` に置く。
  導入手順の画面写真（`web/assets/shot-menu.webp` / `shot-dialog.webp`）は `tools/shoot_screenshots.py` で撮る。モード比較（`shot-modes.webp`）は
  サイドバーで切り替えた 2 体を手で撮ったスクリーンショットから切り出す。スクリプトで撮る場合も手で撮る場合も、他アドオンが写り込まないよう
  `--factory-startup` の Blender にリポジトリの実体だけを登録する。撮影対象のパッケージは `_local/` に置き、追跡ファイルには名前を書かない。

```sh
blender --factory-startup <空の .blend> --python tools/shoot_screenshots.py -- <package> <outdir> main    # hero / nodes / modes（現在のページでは未使用）
blender --factory-startup <空の .blend> --python tools/shoot_screenshots.py -- <package> <outdir> dialog  # インポートのポップアップ
blender --factory-startup <空の .blend> --python tools/shoot_screenshots.py -- <package> <outdir> menu    # File > Import メニュー
```

  ポップアップとメニューはモーダルで閉じられないため、撮ったら Blender を終了する（別々に起動する）。
  スプラッシュ画面を出さないために、引数に空の .blend を渡すこと。切り出しと WebP 化は ImageMagick の `convert` で行う。
