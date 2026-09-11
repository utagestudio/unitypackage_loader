# Unity Package Importer for Blender

`.unitypackage` を Blender の File > Import から直接読み込み、メッシュ（アーマチュア・シェイプキー込み）に
Unity 側のマテリアル設定とテクスチャを反映した状態で配置する Extension です。

- 対応 Blender: 4.2 以降（開発・検証は 5.2 LTS）
- 読み込むもの: FBX / OBJ / glTF / Collada / 同梱 .blend、`.mat`（lilToon / MToon / Poiyomi / Standard / URP / HDRP、その他は一般規則で最善努力）、参照テクスチャ
- 読み込まないもの: シェーダー本体、C#、アニメーション、prefab 階層、Expression メニュー等

## インストール

**開発中（リポジトリを直接使う）**

```sh
ln -s "$PWD/unitypackage_loader" ~/.config/blender/5.2/extensions/user_default/unitypackage_loader
```

Blender の Preferences > Get Extensions で右上メニューから "Refresh Local" を実行し、
"Unity Package Importer" を有効にします。

**配布用 zip**

```sh
blender --command extension build --source-dir unitypackage_loader --output-dir dist
```

生成された zip を Preferences > Get Extensions > "Install from Disk" で読み込みます。

## 使い方

1. File > Import > Unity Package (.unitypackage)
2. 右側のオプションを必要に応じて変更して Import

| セクション | 項目 | 説明 |
|---|---|---|
| Model | Models | `Ask`（複数モデルがあれば選択ダイアログ）/ `All` / `First Only` |
| | FBX Importer | 新 C++ インポーター優先（`Auto`）/ 明示指定 |
| Materials | Material Mode | `Auto`（トゥーン系は Unlit、PBR 系は Principled）/ `Principled BSDF` / `Unlit (Emission)` / `Names Only` |
| | Force Opaque | Unity の Cutout / Transparent 設定を無視する |
| | Backface Culling / Normal Maps / Emission | 各要素を反映するか |
| | Reuse Existing Materials | 同名マテリアルが既にあれば作り直さず再利用 |
| | Bundled .blend Materials | 同梱 .blend のマテリアルを `Keep`（既定）/ `Rebuild` |
| | Store Unity Properties | GUID や影色などをカスタムプロパティに保存 |
| Textures | Extract To | `Beside .blend`（`//textures/<パッケージ名>/`）/ `Add-on Cache` / `Custom Path` |
| | Pack Into .blend | 画像を .blend に同梱 |
| | Import Unreferenced Images | マテリアル未参照の画像（マスク・アイコン等）も画像データとして読み込む |

結果は Info バーに要約が、3D View のサイドバー（N キー）の "Unity Package" タブに件数・マテリアルごとの解決状況・警告が、
システムコンソールに詳細（各マテリアルの対応先 .mat、使用テクスチャ、警告）が出ます。
パッケージ名の Collection が作られ、その中にオブジェクトが入ります。

既定値は Preferences（Edit > Preferences > Add-ons > Unity Package Importer）で変更できます。
独自シェーダーの GUID 表（`shader_guids.json` と同じ書式の JSON）を追加登録することもできます。

### メッシュとマテリアルの対応付け

1. モデルの `.meta`（ModelImporter の externalObjects）に書かれた「マテリアル名 → .mat」
2. `.mat` の名前との一致（同名が複数あればモデルと同じフォルダに近いもの）
3. prefab 内の同名 GameObject の Renderer が同じスロットに持つ .mat

の順で解決します。どれにも当たらないマテリアルはインポーターが作ったまま残し、警告に出します。

### マテリアルの対応方針

Unity のシェーダーと Blender のノードは 1:1 に対応しないため、
`.mat` → シェーダー非依存の中間表現 → Blender ノード の 2 段変換にしています。
中間表現に落とせない値（lilToon の影色・アウトライン・MatCap など）は
マテリアルのカスタムプロパティ `unity_props`（JSON）に保存され、後から手動再現の参考にできます。

## 開発

```sh
# bpy 非依存の単体テスト
python3 -m unittest discover -s tests -t .

# 合成パッケージの生成（実在アセット不要）と、それを使った統合テスト
blender -b --factory-startup --python tests/make_synthetic_package.py
blender -b --factory-startup --python tests/integration_import.py -- _local/synthetic_multi.unitypackage tests/expectations_synthetic.json

# 手元の実パッケージでの統合テスト（_local/ に検証用パッケージと expectations.json が必要）
blender -b --factory-startup --python tests/integration_import.py
```

検証用データ（unitypackage、展開物、期待値）は `_local/` に置きます。このディレクトリは gitignore 対象で、
コミットするファイルに検証用アセット固有の名称や数値を書かないことをルールにしています。
書式は `tests/expectations.schema.md` を参照してください。

設計の詳細は [DESIGN.md](DESIGN.md) を参照してください。

## ディレクトリ構成

```
unitypackage_loader/         Extension 本体
  blender_manifest.toml
  core/                      bpy 非依存（YAML パーサー、tar 索引、.meta、マテリアル正規化、GUID 表）
  blender/                   bpy 依存（インポート実行、ノード生成、画像読み込み）
  operators/                 File > Import のオペレーターとモデル選択ダイアログ
  ui/                        Preferences とサイドバーのレポートパネル
tests/                       単体テスト、統合テスト、合成パッケージ生成
```
