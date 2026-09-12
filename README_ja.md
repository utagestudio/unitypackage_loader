# Unity Package Importer for Blender

日本語 | [English](README.md) | [GitHub](https://github.com/utagestudio/unitypackage_loader)

`.unitypackage` を Blender の File > Import から直接読み込み、メッシュ（アーマチュア・シェイプキー込み）に
Unity 側のマテリアル設定とテクスチャを反映した状態で配置する Extension です。

- 対応 Blender: 4.2 以降（開発・検証は 5.2 LTS）
- 読み込むもの: FBX / OBJ / glTF / VRM / Collada / 同梱 .blend（オプトイン）、`.mat`（lilToon / MToon / Poiyomi / Standard / URP / HDRP / VRChat Mobile（Quest 向け）シェーダー、その他は一般規則で最善努力）、参照テクスチャ
- 読み込まないもの: シェーダー本体、C#、アニメーション、prefab 階層、Expression メニュー等
- VRM: [VRM format](https://extensions.blender.org/add-ons/vrm/) add-on が入っていれば `.vrm` はそちらに委譲します（MToon マテリアル、Humanoid リグ、スプリングボーン、表情は add-on が再現）。無ければ glTF インポーターで読み、同梱の `.mat` からマテリアルを組み直します。

## インストール

**開発中（リポジトリを直接使う）**

```sh
ln -s "$PWD/unitypackage_loader" ~/.config/blender/5.2/extensions/user_default/unitypackage_loader
```

Blender の Preferences > Get Extensions で右上メニューから "Refresh Local" を実行し、
"Unity Package Importer" を有効にします。

**Extension Repository として登録（推奨）**

GitHub Pages で配布しています。Blender の Preferences > Get Extensions > Repositories の「+」から
"Add Remote Repository" を選び、次の URL を登録すると、一覧からインストール・更新できます。

```
https://utagestudio.github.io/unitypackage_loader/index.json
```

同じページ（`index.json` を除いた URL）に登録用 URL と zip のリンクを載せています。
サイトは `.github/workflows/pages.yml` が main への push ごとに生成します（Blender の
`extension build` と `extension server-generate` を CI 上で実行）。初回のみ、リポジトリの
Settings > Pages > Source を "GitHub Actions" にしてください。

**配布用 zip**

```sh
blender --command extension build --source-dir unitypackage_loader --output-dir dist
```

生成された zip を Preferences > Get Extensions > "Install from Disk" で読み込みます。

## 使い方

- **ドラッグ＆ドロップ**: `.unitypackage` を 3D ビューポートに落とすと、インポートオプションのポップアップが開きます。複数ファイルをまとめて落とすこともできます。
- **メニュー**: File > Import > Unity Package (.unitypackage) からファイルブラウザで選び、右側のオプションを必要に応じて変更して Import

| セクション | 項目 | 説明 |
|---|---|---|
| Model | Models | `Ask`（複数モデルがあれば選択ダイアログ）/ `All` / `First Only` |
| | FBX Importer | 新 C++ インポーター優先（`Auto`）/ 明示指定 |
| | VRM via VRM Add-on | `.vrm` を VRM format add-on で読む（既定 ON）。add-on が無ければ glTF インポーターで読んで `.mat` から組み直す |
| | Import Bundled .blend Files | パッケージ内の `.blend` からオブジェクトを append する（既定 OFF）。[セキュリティ上の注意](#セキュリティ上の注意) を参照 |
| Materials | Material Mode | `Auto`（トゥーン系は Toon、PBR 系は Principled）/ `Principled BSDF` / `Toon (Node Group)` / `Unlit (Emission)` / `Names Only` |
| | Force Opaque | Unity の Cutout / Transparent 設定を無視する |
| | Backface Culling / Normal Maps / Emission | 各要素を反映するか |
| | Outlines (Solidify) | Unity 側のアウトライン色・幅から Solidify のアウトラインを付ける（Width Scale で換算） |
| | Reuse Existing Materials | 同名マテリアルが既にあれば作り直さず再利用 |
| | Bundled .blend Materials | 同梱 .blend のマテリアルを `Keep`（既定）/ `Rebuild`（上のオプションが ON のときのみ） |
| | Store Unity Properties | GUID や影色などをカスタムプロパティに保存 |
| Textures | Extract To | `Beside .blend`（`//textures/<パッケージ名>/`）/ `Add-on Cache` / `Custom Path` |
| | Pack Into .blend | 画像を .blend に同梱 |
| | Import Unreferenced Images | マテリアル未参照の画像（マスク・アイコン等）も画像データとして読み込む |

結果は Info バーに要約が、3D View のサイドバー（N キー）の "Unity Package" タブに件数・マテリアルごとの解決状況・警告が、
システムコンソールに詳細（各マテリアルの対応先 .mat、使用テクスチャ、警告）が出ます。
パッケージ名の Collection が作られ、その中にオブジェクトが入ります。

既定値は Preferences（Edit > Preferences > Add-ons > Unity Package Importer）で変更できます。
独自シェーダーの GUID 表（`shader_guids.json` と同じ書式の JSON）を追加登録することもできます。
"Max Extract Size"（既定 8 GiB、0 で無制限）を超える量を 1 つのパッケージから展開しようとした場合や、展開先の空き容量に
収まらない場合は、何も書かずにインポートを中止します。

### セキュリティ上の注意

- **同梱 .blend は既定では読み込みません。** `.blend` には Python コード（ドライバー式、登録済みテキストブロック）を
  仕込むことができ、Blender の「Auto Run Python Scripts」が有効だと読み込み時に実行されます。出所の確かなパッケージでだけ
  "Import Bundled .blend Files" を ON にし、必要がなければ「Auto Run Python Scripts」は OFF のままにしてください。
- **アドオン自身はパッケージ内のコードを一切実行しません。** C#、シェーダー、Python などのアセットは無視します。
  ディスクに展開・読み込みするのは次のものだけです: モデル（`.fbx` `.obj` `.gltf` `.glb` `.vrm` `.dae`。`.blend` は上のオプションが
  ON のときのみ）、マテリアルが参照するテクスチャ（"Import Unreferenced Images" なら全画像）、`.obj` と同じフォルダの `.mtl`、
  `.gltf` と同じフォルダの `.bin`。`.mat` `.meta` `.prefab` はアドオン専用の YAML リーダーでメモリ上でテキストとして解析するだけで、
  書き出しません。
- **モデルと画像の解析は Blender 本体が行います。** FBX / OBJ / glTF / Collada は Blender のインポーター、PNG / TIFF / TGA / EXR /
  PSD などは Blender の画像ライブラリ（OpenImageIO 等）が処理するため、それらの脆弱性を突く細工ファイルはこのアドオンでは防げません。
  Blender は最新の LTS を使い、出所不明のパッケージは隔離した環境（仮想マシン、サンドボックス、他のアドオンを入れていない
  別の Blender）で開いてください。
- アーカイブの扱いは一般的な細工に備えています: パスの検証（`..`、絶対パス、ドライブ文字、予約デバイス名、制御文字を拒否）、
  展開時にシンボリックリンクを追従しない、巨大なメタデータは無視、展開する合計サイズを "Max Extract Size" と空き容量と
  照合してから書き出す。

### メッシュとマテリアルの対応付け

1. モデルの `.meta`（ModelImporter の externalObjects）に書かれた「マテリアル名 → .mat」
2. `.mat` の名前との一致（同名が複数あればモデルと同じフォルダに近いもの）
3. prefab 内の同名 GameObject の Renderer が同じスロットに持つ .mat

の順で解決します。どれにも当たらないマテリアルはインポーターが作ったまま残し、警告に出します。

FBX 内では少数のマテリアルを共有し、Unity 側で prefab がパーツごとに別の .mat を割り当てているパッケージでは、
prefab の割り当てに従ってスロット単位で Blender マテリアルを分割します。prefab が複数ある場合（車体色違いなど）は
選択ダイアログでどの prefab を使うか選べます。

ファイルブラウザで複数の `.unitypackage` を選ぶと一括でインポートします（パッケージごとに Collection ができます）。

### マテリアルの対応方針

Unity のシェーダーと Blender のノードは 1:1 に対応しないため、
`.mat` → シェーダー非依存の中間表現 → Blender ノード の 2 段変換にしています。

- **Toon (Node Group)**: ノードグループ `UnityToon` で影色（境界・ぼかし・強さ）、MatCap（Normal / Add / Screen / Multiply）、
  リムライト、エミッション、アルファを再現します。ライティングに Shader to RGB を使うため EEVEE 向けです（Cycles では影が付きません）。
- **Principled BSDF**: ベースカラー、ノーマル、エミッション、Metallic / Roughness を接続します。
- **Unlit (Emission)**: テクスチャを Emission に直結する、ライティング無しの最も単純な構成です。

中間表現そのものはカスタムプロパティ `unity_normalized`（JSON）に、Blender で使わなかった値は `unity_props` に保存されます。
サイドバーの Tools パネルの "Rebuild in Another Mode" を使うと、元パッケージが無くても選択メッシュのマテリアルを別モードで組み直せます。
同じパネルからアウトラインの追加・削除もできます。

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

## ライセンス

GPL-3.0-or-later（[LICENSE](LICENSE)）。Blender の Extension として配布するための要件に合わせています。

`unitypackage_loader/core/profiles/shader_guids.json` のシェーダー GUID は、公開リポジトリの
[lilxyzw/lilToon](https://github.com/lilxyzw/lilToon) と [vrm-c/UniVRM](https://github.com/vrm-c/UniVRM)（いずれも MIT License）、
および公開リポジトリにミラーされている VRChat SDK の Mobile シェーダー（`Sample Assets/Shaders/Mobile`）の
`.shader.meta` から収集した識別子です。シェーダーのコードは含みません。
lilToon、MToon、Poiyomi、VRChat、Unity などの名称は各権利者の商標または製品名です。
このアドオンは Unity Technologies、VRChat Inc. および各シェーダー作者とは無関係です。
