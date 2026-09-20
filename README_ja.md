# Unitypackage Importer for Blender

日本語 | [English](README.md) | [GitHub](https://github.com/utagestudio/unitypackage_loader)

`.unitypackage` を Blender の File > Import から直接読み込み、メッシュ（アーマチュア・シェイプキー込み）に
Unity 側のマテリアル設定とテクスチャを反映した状態で配置する Extension です。

- 対応 Blender: 4.5 以降（開発は 5.2 LTS、検証は 4.5 LTS と 5.2 LTS）
- 読み込むもの: FBX / OBJ / glTF / VRM / Collada（Blender 4.5 のみ。5.0 で削除された）/ 同梱 .blend（オプトイン）、`.mat`（lilToon / MToon / Poiyomi / Standard / URP / HDRP / VRChat Mobile（Quest 向け）シェーダー、その他は一般規則で最善努力）、参照テクスチャ
- 読み込まないもの: シェーダー本体、C#、アニメーション、Expression メニュー等。prefab の階層、ライト、カメラは、シーンを読み込むときだけ再現します
- VRM: [VRM format](https://extensions.blender.org/add-ons/vrm/) add-on が入っていれば `.vrm` はそちらに委譲します（MToon マテリアル、Humanoid リグ、スプリングボーン、表情は add-on が再現）。無ければ glTF インポーターで読み、同梱の `.mat` からマテリアルを組み直します。

## インストール

**開発中（リポジトリを直接使う）**

```sh
ln -s "$PWD/unitypackage_loader" ~/.config/blender/5.2/extensions/user_default/unitypackage_loader
```

Blender の Preferences > Get Extensions で右上メニューから "Refresh Local" を実行し、
"Unitypackage Importer" を有効にします。

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

- **ドラッグ＆ドロップ**: `.unitypackage` を 3D ビューポートにドラッグ＆ドロップすると、インポートオプションのポップアップが開きます。複数ファイルをまとめてドロップすることもできます。
- **メニュー**: File > Import > Unitypackage (.unitypackage) からファイルブラウザで選び、右側のオプションを必要に応じて変更して Import

オプションで常に表示されるのは Material Mode と Scale だけで、残りは Model / Materials / Textures の折りたたみに入っています。

| セクション | 項目 | 説明 |
|---|---|---|
| （常に表示） | Material Mode | `Auto`（トゥーン系は Toon、PBR 系は Principled）/ `Principled BSDF` / `Toon (Node Group)` / `Unlit (Emission)` / `Names Only` |
| | Scale | モデルのインポーターに渡すスケール |
| Model | FBX Importer | 新 C++ インポーター優先（`Auto`）/ 明示指定 |
| | VRM via VRM Add-on | `.vrm` を VRM format add-on で読む（既定 ON）。add-on が無ければ glTF インポーターで読んで `.mat` から組み直す |
| | Import Bundled .blend Files | パッケージ内の `.blend` からオブジェクトを append する（既定 OFF）。[セキュリティ上の注意](#セキュリティ上の注意) を参照 |
| Materials | Force Opaque | Unity の Cutout / Transparent 設定を無視する |
| | Backface Culling / Normal Maps / Emission | 各要素を反映するか |
| | Outlines (Solidify) | Unity 側のアウトライン色・幅から Solidify のアウトラインを付ける（Width Scale で換算） |
| | Reuse Existing Materials | 同名マテリアルが既にあれば作り直さず再利用 |
| | Bundled .blend Materials | 同梱 .blend のマテリアルを `Keep`（既定）/ `Rebuild`（上のオプションが ON のときのみ） |
| | Store Unity Properties | GUID や影色などをカスタムプロパティに保存 |
| Textures | Extract To | `Beside .blend`（`//textures/<パッケージ名>/`）/ `Add-on Cache` / `Custom Path` |
| | Pack Into .blend | 画像を .blend に同梱 |
| | Import Unreferenced Images | マテリアル未参照の画像（マスク・アイコン等）も画像データとして読み込む |

結果は Info バーに要約が、3D View のサイドバー（N キー）の "UPI" タブに件数・マテリアルごとの解決状況・警告が、
システムコンソールに詳細（各マテリアルの対応先 .mat、使用テクスチャ、警告）が出ます。
パッケージ名の Collection が作られ、その中にオブジェクトが入ります。

### 読み込むものを選ぶ

パッケージに選べるシーン・prefab・モデルが 2 つ以上あると、オプションの後に選択ダイアログが開きます。
上部で読み込む単位を選び、一覧で読み込むものにチェックを入れます。

- **Scenes**: シーン（`.unity`）に置かれたものを、Unity で設定された位置・回転・スケールのまま読み込みます。シーンごとに Collection ができ、
  モデルより上の GameObject は Empty で再現し、非アクティブなものは非表示にします。読み込むのはシーンの内容だけです。
  同じモデル・同じマテリアルの配置は、メッシュのデータを共有した複製になります。
  Unity で共通の親の下に同じ小物を複製して並べたもの（部屋に置いた複数のカップなど）も、1 つずつ置きます。
  prefab やシーンが使っていないモデルファイルの部品（使われていない LOD や余分なノードなど）は、Unity の Renderer が指すメッシュで照合して非表示にします。ライトとカメラも読み込みます（「Also Import」で切り替え）。
  ライトの強さは Unity（Built-in / URP）と Blender の EEVEE で測った値をもとに換算し、元の値はカスタムプロパティに残します。
  Unity でライトマップにだけ効くライト（ベイク、面光源）はリアルタイムのライトになるので、警告に出します。
  UI、Terrain、パッケージに無いメッシュ（Unity 組み込みの Cube など）は読み込まず、警告に出します。モデルファイル（FBX）を直接置いたものは、ルートの位置を読みます。
  中のオブジェクトへの変更（マテリアルや、開けたドアなど部品の位置）は、モデルの `.meta` が Unity 2018.2 以前の形式（名前の表がある）なら当て、
  そうでなければ件数を警告に出します。
- **Prefabs**: prefab ごとに、その prefab のマテリアルの割り当てのまま読み込みます。パッケージの Collection の中に prefab ごとの Collection ができます。
  同じモデルを使う色違いも一緒に読み込めます。2 つ以上選んだときは **Arrange** で、`Side by Side`（重ならないように並べる。数が多ければ格子状に折り返す）と
  `Stack at Origin`（原点に重ねる）を選べます。
- **Models**: モデルファイル（FBX など）をそのまま読み込みます。マテリアルは [メッシュとマテリアルの対応付け](#メッシュとマテリアルの対応付け) のとおりに決まります。

1 回のインポートで使う単位は 1 つなので、同じモデルを誤って二重に読み込むことはありません。読み込めないものも一覧から外さず、
灰色にして理由（`no mesh in package`、`.blend import disabled` など）を表示します。最後に選んだ単位と並べ方は次回の既定になります。
読み込む単位 Prefabs では、prefab の中の配置（子オブジェクトの位置など）は再現せず、prefab のモデルをその Collection の原点に置きます。
配置ごと読み込むには、その prefab を置いたシーンを読み込みます。

既定値は Preferences（Edit > Preferences > Add-ons > Unitypackage Importer）で変更できます。選択ダイアログを出すか
（`Selection Dialog`: `Ask` / `All Models` / `First Model Only`）と、prefab の並べ方（`Arrange Prefabs`）もここで設定します。
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
  `.gltf` と同じフォルダの `.bin`。`.mat` `.meta` `.prefab` はアドオン専用のリーダー（Unity YAML のテキスト、または Unity のバイナリ形式）でメモリ上で解析するだけで、
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
3. prefab 内の同名 GameObject の Renderer が同じサブメッシュに持つ .mat

の順で解決します。どれにも当たらないマテリアルはインポーターが作ったまま残し、警告に出します。

Unity で実際に表示されるのは prefab の Renderer に設定されたマテリアルなので、prefab に割り当てがあれば 1・2 の結果より優先します。
FBX 内では少数のマテリアルを共有し、Unity 側で prefab がパーツごとに別の .mat を割り当てているパッケージでは、
prefab の割り当てに従ってスロット単位で Blender マテリアルを分割します。
モデル自体がマテリアルを 1 つも持たない場合も、prefab の割り当てからスロットを作って当てます。
prefab の Renderer は、そのメッシュを持つモデルにだけ当てはめます（別モデルの同名オブジェクトには使いません）。
読み込む単位が Models のとき、同じモデルを使う prefab が複数ある場合（色違いなど）は、すべてを統合し、パス順で先のものを優先します。
特定の prefab の割り当てを使うには、読み込む単位 Prefabs でその prefab を読み込みます。
Prefab Variant やネストされた prefab のマテリアルの上書きは、元がパッケージ内の prefab なら読みます。
モデル（FBX）を直接置いたものへの上書きは、Unity 2018.2 以前の形式の `.meta`（名前の表がある）ならシーンの読み込みで当て、それ以外は警告に出します。
prefab のマテリアルの並びは Unity のサブメッシュ順（メッシュのポリゴンで最初に使われた順）で、Blender のスロット順と
異なることがあるため、その順でスロットに対応付けます。

ファイルブラウザで複数の `.unitypackage` を選ぶと一括でインポートします（パッケージごとに Collection ができます。
選択ダイアログは出さず、読み込む単位 Models ですべてのモデルを読み込みます）。

### マテリアルの対応方針

Unity のシェーダーと Blender のノードは 1:1 に対応しないため、
`.mat` → シェーダー非依存の中間表現 → Blender ノード の 2 段変換にしています。

- **Toon (Node Group)**: ノードグループ `UnityToon` で影色（境界・ぼかし・強さ）、MatCap（Normal / Add / Screen / Multiply）、
  リムライト、エミッション、アルファを再現します。ライティングに Shader to RGB を使うため EEVEE 向けです（Cycles では影が付きません）。
- **Principled BSDF**: ベースカラー、ノーマル、エミッション、Metallic / Roughness を接続します。
- **Unlit (Emission)**: テクスチャを Emission に直結する、ライティング無しの最も単純な構成です。

中間表現そのものはカスタムプロパティ `unity_normalized`（JSON）に、Blender で使わなかった値は `unity_props` に保存されます。
サイドバーの Tools パネルの "Rebuild in Another Mode" を使うと、元パッケージが無くても選択メッシュのマテリアルを別モードで組み直せます。ダイアログの Force Opaque などの初期値は、インポート時（または前回の組み直し）の設定です。
同じパネルからアウトラインの追加・削除もできます。

## 開発

```sh
# bpy 非依存の単体テスト
python3 -m unittest discover -s tests -t .

# 合成パッケージの生成（実在アセット不要）と、それを使った統合テスト
blender -b --factory-startup --python tests/make_synthetic_package.py
blender -b --factory-startup --python tests/integration_import.py -- _local/unitypackages/synthetic_multi.unitypackage tests/expectations_synthetic.json

# 手元の実パッケージでの統合テスト（_local/unitypackages/ にパッケージ、_local/ に expectations.json）
blender -b --factory-startup --python tests/integration_import.py
```

PR と main / release ブランチへの push では、`.github/workflows/tests.yml` が単体テスト（Blender 4.5 / 5.2 に同梱の Python 3.11 / 3.13）と、
合成パッケージの統合テスト（Blender 4.5 LTS / 5.2 LTS）を回します。

検証用データ（unitypackage、展開物、期待値）は `_local/` に置きます（パッケージ本体は `_local/unitypackages/`）。このディレクトリは gitignore 対象で、
検証に使ったアセットのデータはリポジトリに含めません（コミットするテスト用データは合成データだけです）。
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
