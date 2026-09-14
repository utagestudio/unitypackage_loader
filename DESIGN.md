# unitypackage_loader — 設計書

Blender から `.unitypackage` を直接読み込み、メッシュ（アーマチュア・シェイプキー込み）とマテリアル／テクスチャの設定までを一度に行うアドオンの設計。
対象 Blender: **4.2 以降（Extension 形式）**、開発・検証環境は **5.2 LTS / Python 3.13**。

---

## 0. サンプル調査で分かった前提

手元にある VRChat 向けアバターの unitypackage 1 種を調査した。配布物のためリポジトリには含めず、`_local/`（gitignore 済み）に置いて開発・検証に使う。2026-09-14 まではアセット名を伏せる方針だったため、それ以前に書いた箇所はアセット名を伏せ、マテリアル名などを一般化して記載している。

設計の根拠となる事実。実装時はこの構造を前提にしつつ、他パッケージでの揺れに耐えるようにする。

### 0.1 unitypackage の物理構造

- 実体は **tar.gz**。ルート直下に「GUID 名のディレクトリ」が並ぶ（サンプルでは百数十個）。
- 各 GUID ディレクトリの中身:

| ファイル | 内容 |
|---|---|
| `pathname` | Unity プロジェクト内パス（例 `Assets/<Avatar>/Materials/Body.mat`）。1 行目がパス |
| `asset` | 実データ。フォルダの場合は存在しない |
| `asset.meta` | Unity の .meta（YAML）。インポーター設定を持つ |
| `preview.png` | サムネイル（任意）。読み込み不要 |

- tar.gz は **ランダムアクセス不可**。一度ストリーム走査して索引を作り、必要なものだけ展開する方針が必要（サンプルは数十 MB だが 1GB 超のパッケージも珍しくない）。

### 0.2 サンプルの内訳

| 種別 | 数 | 備考 |
|---|---|---|
| FBX | 1 | Blender から書き出されたもの。テクスチャは **埋め込まれていない** |
| .mat | 数個 | 全て **lilToon** 系シェーダー |
| PNG | 数十枚 | メイン / ノーマル / MatCap / マスク。加えて Expression メニュー用アイコン（無関係） |
| .prefab / .controller / .anim / .asset | 多数 | 全て対象外 |

### 0.3 「どのメッシュにどのマテリアルが付くか」の情報源

**最重要ポイント。** FBX 自体にはマテリアル名しか無く、Unity 側の `.mat` との対応は次の場所にある。

1. **FBX の `.meta`（ModelImporter）の `externalObjects`**
   `FBX 内マテリアル名 → .mat の GUID` の対応表。サンプルでは全マテリアルがここに登録されており、これが正解データ。
   - Unity は同名マテリアルを `Body.001` `.002` のように連番化して登録している場合がある（FBX 内の実マテリアルには連番が無い）。→ **完全一致 → 連番サフィックス除去で再検索** の 2 段で解決する。
2. **フォールバック A: 名前一致** — `.mat` の `m_Name` と FBX マテリアル名が同じものを探す（externalObjects が空のパッケージ向け）。
3. **フォールバック B: prefab の `MeshRenderer` / `SkinnedMeshRenderer` の `m_Materials`** — prefab 内の同名 GameObject が同じサブメッシュに持つ .mat を採用する（サブメッシュとスロットの対応は下記）。

1〜3 は「FBX マテリアル 1 つに .mat を 1 つ」決める順序。Unity で実際に表示されるのは Renderer の `m_Materials` で、externalObjects は FBX を置いたときの既定値にすぎないので、prefab に割り当てがあれば下記のスロット単位の分割で 1・2 の結果より優先する。

**prefab とモデルの対応**（`core/prefab.py`、Issue #25）: prefab の表はモデルごとに作る。Renderer がどのモデルのものかは、メッシュ参照（MeshRenderer と同じ GameObject の MeshFilter、または SkinnedMeshRenderer の `m_Mesh`）の GUID で決め、メッシュ参照が無い Renderer とパッケージ外のメッシュを指す Renderer は使わない。1 つの prefab が複数モデルを含む場合も Renderer 単位で振り分けるので、別モデルの同名オブジェクトには当てはまらない。読み込む単位 Models では、モデルごとに「そのモデルを使う prefab」だけを候補にし、候補をパス順に先勝ちで統合する（`PreparedPackage.table_for`）。

**読み込む単位 Prefabs**（§3.3、Issue #47）: 選んだ prefab ごとに、その prefab の表だけを当てはめてモデルを読み込む（`ImportOptions.unit` / `prefab_paths`）。同じモデルを使う prefab（色違いなど）を複数選ぶと、prefab ごとにモデルを読み直す。読み直したモデルのマテリアルのうち、組み立て済みの .mat と同じものは新しく組まずに同じ Blender マテリアルを使い回し（レポートの method は `shared`。`replaced` と同じく件数からは除く）、prefab の表と違うスロットだけを下記の分割で差し替える。読み込めないモデルを含む prefab は、そのモデルだけ飛ばして警告に出す。

**読み込む単位 Scenes**（§3.3、Issue #48）: `core/hierarchy.py` で .unity を展開し、モデルの配置を求めて読み込む。

- 展開: Transform（RectTransform を含む）・GameObject・Renderer を読み、PrefabInstance は元のアセットを再帰的に差し込む（深さ 16、Transform 20 万個まで、循環は打ち切り）。差し込んだオブジェクトの key は prefab と同じく「PrefabInstance の fileID XOR 元の key」。シーンの fileID はこの規則に従わない（調査で確認）ので、外からの参照（子を付ける親、`m_TransformParent`）は stripped ドキュメントの `m_CorrespondingSourceObject` / `m_PrefabInstance` を対応表にして引く。上書きは位置・回転・スケール、`m_IsActive`、`m_Name`、Renderer の `m_Enabled` とマテリアルを当て、`m_RemovedGameObjects` / `m_RemovedComponents` を除く。
- 配置は 2 種類。モデルの PrefabInstance は、そのルートの Transform（fileID は FBX によらず定数 -8679921383154817045）を配置のルートにする。中のオブジェクトへの上書きは fileID を名前に結び付けられないので数えて警告に出す（Issue #31）。モデルのメッシュを直接指す Renderer（FBX を展開した prefab）は、Transform を直接持つアセットでの最上位の祖先をモデルのルートとみなし、同じ祖先・同じモデルの Renderer をまとめる。
- 座標変換（`core/transform.py`）: Unity のモデル空間 (x, y, z) は Blender では (-x, -z, y)。Unity でモデルのルートに掛かる行列 M は、Blender では C·M·C⁻¹ を原点に読み込んだオブジェクトの行列に左から掛ける。Unity 6 で合成 FBX を 3 通り（FBX を中に置いた prefab、空の親の下への直置き、非一様スケールで置いて中の子を上書きした展開 prefab）に置いたシーンを作り、Renderer ごとの頂点のワールド座標（スキンしたメッシュはボーン行列 × bindpose で計算）と、Blender の FBX インポーターで読んだ同じ FBX から予測した座標が、9 か所すべてで一致することを確かめた。Blender 由来の FBX では、Unity のルート直下のノードは X -90 度・スケール 100 を持つが、Blender のオブジェクトはその値を持たない。そのためノードの値を直接オブジェクトに当てず、行列で計算してから差分として当てる。
- 読み込み: 配置のルートと、その祖先の GameObject を Empty にし（行列は Unity のローカル行列を C·M·C⁻¹ で変換）、モデルの最上位のオブジェクトをルートの Empty の子にする。.meta の `globalScale` は親の逆行列として掛ける。同じモデル・同じマテリアルの割り当ての配置は、2 つ目からメッシュ・アーマチュアのデータを共有した複製（`Object.copy()`）にし、アーマチュアモディファイアとコンストレイントの参照を複製側に付け替える。割り当てが違えば読み直す（組み立て済みの .mat は Prefabs と同じく共有）。展開 prefab の中のノードが上書きで動いていれば、そのノードから逆算したルートの行列でオブジェクトの `matrix_world` を置き直す（親から順に。アーマチュアで変形するものは動かさず件数を警告に出す）。非アクティブな GameObject と無効な Renderer は `hide_set` と `hide_render` で隠す。
- 読み込まないもの: ライト・カメラ（Issue #49）、UI、Terrain、パッケージ外のメッシュを使う Renderer（Unity 組み込みの Cube など）。件数を警告に出す。

**Prefab Variant / ネストされた prefab**: PrefabInstance の `m_SourcePrefab` がパッケージ内の prefab なら、その Renderer を引き継ぎ、`m_Modifications` のうち `m_Materials.Array.data[N]` と `m_Materials.Array.size` を重ねる（`resolve_renderers`）。上書きの `target` は元 prefab 内の fileID。引き継いだオブジェクトの fileID は Unity と同じく「PrefabInstance の fileID XOR 元の fileID」（上位ビットは落とす）で、実パッケージの stripped ドキュメントで一致を確認済み。これで Variant の Variant もたどれる。循環は打ち切り、深さは 16 段、上書きで受け付ける配列長は 1024 まで。元がモデル（FBX 等）の上書きは、対象 fileID がモデル内部の ID（.meta の `internalIDToNameTable` が空なら Unity がハッシュで生成）で名前に結び付けられないため読まず、件数を警告に出す（Issue #31）。

**スロット単位の分割**: FBX 内では少数のマテリアルを全メッシュが共有し、Unity 側では prefab の Renderer ごとに別の .mat を割り当てているパッケージがある（工業製品系アセットで確認）。この場合「FBX マテリアル 1 つ = .mat 1 つ」では色もテクスチャも失われるため、prefab の (GameObject, スロット) → .mat 表を作り、FBX マテリアルの解決結果と異なるスロットは .mat 名の Blender マテリアルに差し替える。同じ .mat は 1 つの Blender マテリアルを共有し、使われなくなった FBX マテリアルは削除する。

**サブメッシュ順とスロット順**: `m_Materials` の並びは Unity のサブメッシュ順で、Unity の FBX インポーターはポリゴン列で最初に使われた順にサブメッシュを作り、ポリゴンの無いマテリアルはサブメッシュにしない。一方 Blender のスロットは FBX 内のマテリアル順なので、両者は一致しないことがある。スロット番号のまま突き合わせると別パーツの .mat に差し替わる（VRChat 向けアバターで、パーツ間のマテリアルが入れ替わって別パーツのテクスチャが半透明の層のように見えた。Issue #22）。そこでメッシュのポリゴンから「最初に使われた順のスロット番号列」を作り（`core/mapping.py` の `submesh_slot_order`）、`m_Materials[i]` をその i 番目のスロットに対応させる。フォールバック B とスロット単位の分割の両方がこの対応を使う。

Blender 標準 FBX インポーターが生成するマテリアル名は FBX 内の名前そのまま（サンプルで検証済み）。よって **Blender のマテリアル名 → externalObjects → .mat** で引ける。

### 0.4 `.mat` の中身（Unity YAML）

```yaml
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!21 &2100000
Material:
  m_Name: Body
  m_Shader: {fileID: 4800000, guid: efa77a80ca0344749b4f19fdd5891cbe, type: 3}   # lilToon 配布物の lts_o.shader（公開 GUID）
  m_CustomRenderQueue: -1
  m_SavedProperties:
    m_TexEnvs:
    - _BaseMap:
        m_Texture: {fileID: 2800000, guid: 0123456789abcdef0123456789abcdef, type: 3}
        m_Scale: {x: 1, y: 1}
        m_Offset: {x: 0, y: 0}
    m_Floats:
    - _Cutoff: 0.5
    m_Colors:
    - _Color: {r: 1, g: 1, b: 1, a: 1}
```

- 独自タグ `!u!21` とアンカー `&2100000` を除けば通常の YAML サブセット。`!u!<classID>` と `fileID` の `2100000`（Material）`4800000`（Shader）`2800000`（Texture2D）は Unity が全プロジェクト共通で使う固定値（classID × 100000）。
- テクスチャは **GUID 参照**。`pathname` 索引（GUID → パス）で引ける。
- **Blender 同梱 Python に PyYAML は無い**（確認済み）。→ 依存無しの専用パーサーを書く（§4.4）。
- 1 つの .mat に ~500 プロパティ。ほとんどが未使用（`fileID: 0`）なので、値が入っているものだけ拾う。

### 0.5 テクスチャの `.meta`（TextureImporter）

| キー | 使い道 |
|---|---|
| `textureType` | `0`=Default, `1`=**NormalMap** → Non-Color 扱い |
| `sRGBTexture` | `0` なら Non-Color |
| `alphaIsTransparency` | アルファ解釈のヒント |
| `wrapU/wrapV` | 0=Repeat, 1=Clamp → 画像ノードの extension |

### 0.6 参考 .blend（制作元ファイル）のマテリアル構成

参考にしたアニメ調モデルの Blender 側セットアップは **「画像テクスチャ → Emission シェーダー（ライティング無し）＋透過は Transparent と Mix」** の非常に単純な構成（メインテクスチャのみ使用、blend=HASHED）。
アニメ調モデルでは Principled より Emission ベースの方が意図に近い。→ マテリアル生成モードに **「Unlit（Emission）」を用意し、トゥーン系シェーダー検出時のデフォルトにする**（§3.2）。

---

## 1. スコープ

### 読み込む
- モデル: `.fbx` `.obj` `.gltf/.glb` `.dae`（Blender 標準インポーターに委譲）と同梱 `.blend`（append。Python スクリプトを含み得るので既定 OFF のオプトイン。マテリアルは既定でそのまま残す）。OBJ の `.mtl`、glTF の `.bin` は同じフォルダから一緒に展開する。
- `.vrm`: VRM add-on（extensions.blender.org の "VRM format"、`import_scene.vrm`）が登録されていればそちらに委譲し、マテリアルも add-on のもの（MToon ノードグループ）をそのまま使う。無ければ glTF バイナリとして標準 glTF インポーターで読み、`.mat`（UniVRM が展開した MToon マテリアル）から組み直す。
- メッシュ・アーマチュア・シェイプキー・UV・頂点カラー（FBX インポーターの能力の範囲）。
- マテリアル: `.mat` を解析し、**Blender で意味を持つ情報だけ**をノードに反映。
- テクスチャ: `.mat` から参照されている画像のみ展開して読み込む（オプションで全画像）。

### 読み込まない
- シェーダー本体（`.shader` `.cginc`）、C#（`.cs` `.dll`）、アニメーション（`.anim` `.controller`）、Expression/Parameter アセット、PhysBone 等の MonoBehaviour、prefab 階層（シーンを読み込む場合はモデルの配置と、その上の GameObject だけ再現する）、ライト・カメラ（Issue #49）、アイコン画像、`preview.png`。

### マテリアルの対応方針
Unity と 1:1 は不可能なので、**「Unity マテリアル → シェーダー非依存の中間表現（NormalizedMaterial）→ Blender ノード」** の 2 段変換にし、シェーダーごとの差は「プロファイル」に閉じ込める（§4.5）。
中間表現に落とせなかった値も **カスタムプロパティとして保存**し、後から手動再現できるようにする。

---

## 2. ユーザーフロー

```
File > Import > Unitypackage (.unitypackage)      .unitypackage を 3D View にドラッグ＆ドロップ
        │                                              │（FileHandler → 同じオペレーターを filepath 付きで invoke）
        ▼                                              ▼
 ファイルブラウザ（右側にオプション）      ← §3.1   オプションのポップアップ（invoke_props_dialog）
        │  [Import]                                    │  [Import]
        ▼                                              ▼
 パッケージ走査（索引作成、シーン・prefab・モデルの候補列挙）
        │
        ├─ 読み込める候補が 1 つ以下、または Selection dialog が All / First → そのまま続行（Models 単位）
        └─ それ以外 → 選択ダイアログ（読み込む単位と候補を選ぶ）    ← §3.3
        │
        ▼
 FBX インポート → マテリアル解決 → テクスチャ展開 → ノード生成
        │
        ▼
 完了レポート（Info バー + N パネル）     ← §3.4
```

ファイルブラウザ側でパッケージ内容を先読みしない理由: tar.gz は全展開しないと一覧が取れず、大きいパッケージでファイル選択のたびに UI が固まるため。

---

## 3. UI 仕様

### 3.1 インポートオプション（ファイルブラウザ右パネル）

常に表示するのは Material mode と Scale だけで、残りは Model / Materials / Textures の折りたたみ（`layout.panel`、既定で閉じる）に入れる。
何を読み込むかは §3.3 のダイアログで、パッケージの中身を見てから選ぶ。同梱 .blend を ON にしているときの警告は、パネルの開閉によらず表示する。

**Model**
| 項目 | 型 / 既定値 | 説明 |
|---|---|---|
| Selection dialog（Preferences のみ） | Enum: `Ask` / `All models` / `First model only` = `Ask` | `Ask` は読み込める候補（シーン・prefab・モデル）が合わせて 2 つ以上ある時だけ §3.3 のダイアログを出す。ポップアップには出さない（オペレーターの `models` プロパティはプリセットとスクリプトのため残す）。複数ファイルの一括インポートは常に All |
| Arrange prefabs（Preferences のみ） | Enum: `Side by Side` / `Stack at Origin` = `Side by Side` | §3.3 の並べ方の既定。ダイアログで選んだ値がここに保存される |
| VRM via VRM Add-on | Bool = ON | `.vrm` を VRM add-on に委譲する。add-on が無ければ glTF インポーターにフォールバックし警告で案内 |
| Import bundled .blend files | Bool = OFF | 同梱 `.blend` を append する。`.blend` はドライバー式等で Python を実行し得るため既定 OFF。ON のとき UI に警告を出す。OFF なら該当モデルはスキップして警告に理由を出す |
| Use FBX file scale / axis | FBX インポーターのパススルー | 既定は Blender FBX インポーターと同じ |
| Import armature / shape keys / animation | Bool = ON / ON / OFF | FBX インポーターへ渡す |

**Materials**
| 項目 | 型 / 既定値 | 説明 |
|---|---|---|
| Material mode | Enum: `Auto` / `Principled BSDF` / `Toon (Node Group)` / `Unlit (Emission)` / `Names only` = `Auto` | `Auto`: トゥーン系（lilToon, Poiyomi, MToon, UTS, VRChat Toon Standard）→ Toon、PBR 系（Standard, URP Lit, HDRP Lit, VRChat Standard Lite）→ Principled、Unlit 系（Unlit/*, VRChat Toon Lit）→ Unlit |
| Transparency | Enum: `Auto` / `Force opaque` = `Auto` | Unity 側の Cutout/Fade/Transparent 判定を `surface_render_method` に反映 |
| Backface culling | Bool = ON | `_Cull` / `_CullMode` / `_Culling` に従う |
| Normal maps | Bool = ON | |
| Emission | Bool = ON | |
| Reuse existing materials by name | Bool = OFF | 同名マテリアルが .blend に既にある場合、置き換えず再利用 |
| Store Unity properties | Bool = ON | 生値をカスタムプロパティに保存 |

**Textures**
| 項目 | 型 / 既定値 | 説明 |
|---|---|---|
| Extract to | Enum: `Beside .blend` / `Addon cache` / `Custom path` = `Beside .blend`（未保存 .blend なら `Addon cache`） | `//textures/<パッケージ名>/Assets/...` のように Unity パスをそのまま再現 |
| Pack into .blend | Bool = OFF | |
| Import unreferenced images | Bool = OFF | マスク画像など .mat 未参照の画像も画像データとして読み込む（ユーザーが手動で使う用）。Unity 側でメニューアイコンも通常テクスチャ（textureType 0）として登録されているため、それらも含まれる |
| Overwrite extracted files | Bool = OFF | 既に展開済みならスキップ |

### 3.2 マテリアルモード別の生成ノード

**Principled BSDF**
```
[TexImage base] ─(Color)─▶ [Mix: Multiply, _Color] ─▶ Base Color
                ─(Alpha)─▶ [Math > cutoff]* ─────────▶ Alpha        *Cutout 時のみ
[TexImage normal (Non-Color)] ─▶ [Normal Map, strength=_BumpScale] ─▶ Normal
[TexImage emission] × _EmissionColor ─▶ Emission Color (Strength 1)
Metallic = _Metallic, Roughness = 1 − _Smoothness（_Glossiness）
_MainTex_ST の scale/offset ≠ (1,1,0,0) なら [TexCoord]→[Mapping] を挿入
```

**Unlit (Emission)** — 参考 .blend と同じ構成
```
[TexImage base] ─(Color)─▶ [Mix: Multiply, _Color] ─▶ [Emission, strength 1] ─┐
                ─(Alpha)─▶ [Math > cutoff]* ───────────────────────────────▶ [Mix Shader] ─▶ Output
                                                        [Transparent BSDF] ─┘
```
ノーマルマップは効果が無いので接続せず、画像だけ読み込んで未接続ノードとして置く（オプション）。

**Toon (Node Group)** — ノードグループ `UnityToon`（`blender/toon_group.py`）
```
[TexImage base] × _Color ─▶ Base Color ─┐
[Normal Map | Geometry.Normal] ─▶ Normal ─┤ UnityToon ─▶ Output
[MatCap tex via view-space normal UV] ──▶ MatCap ─┘
  Shadow Color/Strength/Border/Blur, MatCap Strength/Mode, Rim Color/Strength/Border, Emission は extras から入力値として設定
```
グループ内部: Diffuse BSDF → Shader to RGB → RGB to BW でライティング量を取り、Map Range（border ± blur/2）で影係数に。
Base × Shadow Color と Base を係数で混ぜ、Shadow Strength で元に戻す。MatCap は 4 種のブレンドを Compare で選択。
リムは Layer Weight(Facing) を Map Range。最後に Emission シェーダー + Alpha で Transparent と Mix。EEVEE 向け（Cycles は Shader to RGB 非対応）。

**Names only** — マテリアルは FBX インポーターが作ったまま。マッピング結果とカスタムプロパティだけ付与。

共通:
- `material.surface_render_method`: Opaque/Cutout → `DITHERED`、Fade/Transparent → `BLENDED`（Blender 4.2 以降の API。`blend_method` は使わない）
- `material.use_backface_culling` = `_Cull == 2`（Back）
- `material.diffuse_color` にベースカラー（ソリッド表示用）
- ノードはフレームでグループ化し、位置を整えて読みやすくする

### 3.3 選択ダイアログ（`invoke_props_dialog`、Issue #47）

「Ask」指定で、読み込める候補（シーン・prefab・モデル）が合わせて 2 つ以上ある時に表示する。上部で**読み込む単位**を切り替え、一覧にはその単位の候補だけを出す。単位は排他で、1 回のインポートでは 1 つの単位だけを読む（同じモデルの二重読み込みや、マテリアルの割り当て元の食い違いを構造上起こさない）。

```
┌ Import from Unitypackage ──────────────────────────────────────────┐
│ ▣ Example.unitypackage                                             │
│  [ Scenes (1)     |  Prefabs (3)          |  Models (1)         ]  │
│  [x] Assets/…/Body_Blue.prefab                    1 model · 2 mat  │
│  [x] Assets/…/Body_Red.prefab                     1 model · 2 mat  │
│  [ ] Assets/…/Effect.prefab                    no mesh in package  │ ← 灰色
│  [ All ]  [ None ]                                                 │
│  Arrange  [ Side by Side | Stack at Origin ]                       │ ← Prefabs のみ
│  Materials: 13 found in package                                    │
│  Textures: 20 referenced, 3 missing from package                   │
│                                                  [Cancel] [Import] │
└────────────────────────────────────────────────────────────────────┘
```

| 単位 | 候補 | 読み込み方 |
|---|---|---|
| Scenes | パッケージ内のすべてのシーン（pathname 順） | シーンごとにパッケージのコレクションの子コレクションを作り（`unity_scene` / `unity_scene_guid`）、シーンに置かれたモデルだけを配置どおりに読み込む（§0.3 の「読み込む単位 Scenes」） |
| Prefabs | パッケージ内のすべての prefab（pathname 順） | prefab ごとにパッケージのコレクションの子コレクションを作り（`unity_prefab` / `unity_prefab_guid`）、Renderer が使うモデルをその prefab の表だけで読み込む（§0.3 の「読み込む単位 Prefabs」） |
| Models | パッケージ内のすべてのモデル | 従来どおり。prefab の表は、そのモデルを使う prefab をパス順に先勝ちで統合したもの |

- 候補は推測で外さない。読み込めない候補（Renderer がパッケージ内のモデルを使っていない prefab、使うモデルがすべて読み込めない prefab、非対応形式や同梱 .blend OFF のモデル、パッケージ内のモデルを置いていないシーン、読めないシーン）は灰色にして理由を出す（`core/units.py`）。候補が 1 つも無い単位はタブに出さない。
- 既定の単位は、前回選んだ単位（Preferences の `last_import_unit`）に読み込める候補があればそれ、無ければ Prefabs → Models → Scenes の順。
- **並べ方**（Prefabs で 2 つ以上選んだとき有効）: `Side by Side` は prefab ごとのコレクションのワールド座標の外形を求め、1 つ目を動かさずに +X へ並べる。5 つ以上は ceil(√n) 列の格子に折り返し、次の行を +Y に置く。行の中では手前側（Y の最小）を揃える。間隔は最大の幅・奥行きの 25%（最小 0.1 m）（`core/arrange.py`）。動かすのは親を持たないオブジェクト。`Stack at Origin` は動かさない。行を出し入れするとダイアログの高さと Import ボタンの位置が変わるので、Prefabs では常に表示し、選択が 1 つ以下なら淡色にする。
- 読み込む単位 Prefabs では prefab 内の配置（子オブジェクトの位置など）は再現せず、prefab のモデルはすべてそのコレクションの原点に置く。配置を再現するのは Scenes だけ。
- 一覧は `UIList` + `CollectionProperty`（WindowManager 側。All / None ボタンから書き換えるため）。表示中の単位への絞り込みは `filter_items` で行う（名前での絞り込みも併用）。単位の enum は候補数入りのラベルを動的 items で出すので、文字列をモジュールで保持する。パッケージ由来の pathname は表示用に `sanitize_display` を通し、選択結果は GUID で持つ。
- スクリプトからは、オペレーターの非表示プロパティ `unit`（`SCENES` / `PREFABS` / `MODELS`）と `arrange` で指定できる。ダイアログを出さない場合は、その単位の読み込める候補をすべて読む。

### 3.4 完了レポート

- **Info バー**: `Imported 20 objects, 7 materials (7 mapped), 12 textures. 2 warning(s) — see the system console`
- **3D View > N パネル > "UPI" タブ**: 直近のインポートのレポート（読み取り専用）。読み込む単位 Prefabs なら読み込んだ prefab 数も出す
- オブジェクト名・マテリアル名・パス・例外文はパッケージ由来の文字列なので、コンソール（`as_text`）・N パネル・
  選択ダイアログ・オペレーターの report に出す前に `core/report.py` の `sanitize_display` で制御文字（C0 / DEL / C1）を
  `\x1b` のような可視表現に置き換える（ANSI エスケープで表示を乱せないようにする）。
  - Objects / Materials / Textures の一覧
  - 未解決マテリアル（.mat が見つからない）、パッケージに無いテクスチャ GUID、非対応シェーダー、PSD などの非対応画像形式
  - 「Open extract folder」「Copy log」ボタン
- 警告はインポートを止めず、可能なところまで進めて記録する。

### 3.5 アドオン Preferences

- 既定の展開先（Addon cache のパス）
- 既定の Material mode
- シェーダー GUID → プロファイル の追加登録（テキスト or JSON パス）
- ログレベル

### 3.6 マテリアルに付与するカスタムプロパティ

| キー | 内容 |
|---|---|
| `unity_material_guid` | .mat の GUID |
| `unity_material_path` | `Assets/...` パス |
| `unity_shader_guid` / `unity_shader_family` | シェーダー GUID と判定したファミリー名 |
| `unity_props` | 中間表現に落とせなかった値（JSON 文字列）。例: `_ShadowColor`, `_OutlineWidth`, MatCap 参照 |
| `unity_normalized` | NormalizedMaterial 全体（JSON）。再構築オペレーターが使う。画像側には `unity_guid` / `unity_texture_type` / `unity_clamps` |

---

## 4. 実装仕様

### 4.1 パッケージ構成（Blender Extension）

```
unitypackage_loader/
├─ blender_manifest.toml         # id, version, blender_version_min="4.2.0", permissions=["files"]
├─ __init__.py                   # register / unregister、メニュー登録
├─ operators/
│   ├─ import_package.py         # IMPORT_SCENE_OT_unitypackage（ImportHelper）
│   └─ select_models.py          # IMPORT_SCENE_OT_unitypackage_select（読み込む単位と候補を選ぶ invoke_props_dialog）
├─ ui/
│   ├─ panel_report.py           # N パネル
│   └─ preferences.py
├─ core/                         # bpy 非依存。純 Python で単体テスト可能
│   ├─ package.py                # tar 走査・索引・オンデマンド展開
│   ├─ unity_yaml.py             # Unity YAML サブセットパーサー
│   ├─ unity_binary.py           # バイナリ形式の SerializedFile リーダー（TypeTree 付き）
│   ├─ meta.py                   # ModelImporter / TextureImporter の読み取り
│   ├─ material.py               # UnityMaterial / NormalizedMaterial dataclass
│   ├─ mapping.py                # FBX マテリアル名 → .mat 解決
│   ├─ prefab.py                 # prefab の Renderer → モデルごとのマテリアル表（Variant / ネスト込み）
│   ├─ units.py                  # 読み込む単位（Prefabs / Models）の候補と既定値
│   ├─ arrange.py                # 複数の prefab を重ならないように並べる位置の計算
│   ├─ hierarchy.py              # シーン・prefab の階層の展開と、モデルの配置
│   ├─ transform.py              # Unity の Transform の行列と Unity → Blender の座標変換
│   ├─ profiles/
│   │   ├─ base.py               # ShaderProfile 基底 + 判定（GUID テーブル / プロパティ指紋）
│   │   ├─ liltoon.py
│   │   ├─ standard.py           # Built-in Standard / URP Lit / HDRP Lit
│   │   ├─ mtoon.py              # VRM MToon（Phase 2）
│   │   ├─ poiyomi.py            # Phase 2
│   │   ├─ vrchat_mobile.py      # VRChat SDK Mobile（Quest 向け）シェーダー
│   │   └─ shader_guids.json     # GUID → family 表（ユーザー拡張可）
│   └─ report.py
├─ blender/                      # bpy 依存
│   ├─ importer.py               # 全体オーケストレーション
│   ├─ materials.py              # ノード生成
│   ├─ textures.py               # 画像読み込み・カラースペース
│   └─ props.py                  # Scene / WindowManager プロパティ
└─ tests/
    ├─ test_unity_yaml.py        # pytest（bpy 不要）
    ├─ test_mapping.py
    └─ integration_import.py     # blender -b --python で実行、_local/ のサンプルで検証
```

`core/` を bpy 非依存にするのが要点。パーサーとマッピングは標準ライブラリの unittest で回せる（`python3 -m unittest discover -s tests -t .`。追加依存無し）。

### 4.2 データフロー

```
.unitypackage
  │ (1) 索引作成: 1 パス目 — pathname と asset.meta だけ読む
  ▼
PackageIndex { guid → AssetEntry(pathname, meta_text, has_asset, size) }
  │ (2) 分類: models / materials / textures / other
  │ (3) マッピング解決: model.meta.externalObjects → 名前一致 → prefab
  │ (4) .mat 解析 → UnityMaterial
  │ (5) プロファイル判定 → NormalizedMaterial
  │ (6) 必要なメンバーだけ展開: 2 パス目 — model, 参照 texture を extract_dir へ
  ▼
  (7) bpy.ops.import_scene.fbx  — 前後の bpy.data 差分で新規 Object / Material を捕捉
  (8) 新規 Material ごとに NormalizedMaterial を引き、ノード生成
  (9) 画像読み込み（check_existing、カラースペース、alpha_mode）
 (10) レポート
```

### 4.3 `core/package.py`

```python
@dataclass
class AssetEntry:
    guid: str
    pathname: str          # "Assets/<Avatar>/Materials/Body.mat"
    has_asset: bool
    size: int
    meta_text: str | None  # asset.meta の中身（小さいので常駐）

class UnityPackage:
    def __init__(self, path): ...
    def scan(self) -> dict[str, AssetEntry]       # 1 パス目
    def read_asset(self, guid) -> bytes           # 小さいもの（.mat）向け
    def extract(self, guids, dest_root) -> dict[str, Path]   # 2 パス目、pathname を再現
    # 分類ヘルパ
    def models(self), materials(self), textures(self)
```

- `tarfile.open(path, "r:gz")` をストリームで 1 回走査。`pathname` と `asset.meta` は即読み。`asset` はサイズだけ記録。
- 2 パス目は必要 GUID 集合に絞って `extractfile`。数 GB のパッケージでも展開量は必要分だけ。
- `pathname` の 1 行目のみ使用（2 行目に `00` が入る形式がある）。
- パス正規化（`safe_relative_path`）: `..`・絶対パス・空要素に加え、Windows で展開先の外に出るか異常なファイルになる
  コロン（ドライブ文字・NTFS 代替データストリーム）、制御文字、`<>"|?*`、予約デバイス名（`CON` `NUL` `COM1` 等。拡張子付きも）、
  末尾のドット / 空白を拒否する。規則は展開する OS に依らず同じ（Linux で展開した結果を Windows で開くケースがあるため）。
- メモリへ丸ごと読むメンバーには上限を置く（`pathname` / `asset.meta` は 16 MiB、`read_asset` は 64 MiB。tar ヘッダーのサイズで判定）。
  超えた `pathname` / `asset.meta` は警告して無視し、`read_asset` は `PackageError`（呼び出し側はそのアセットだけスキップ）。
- 展開前に書き出す合計サイズ（tar ヘッダー基準。gzip / sparse で小さく見せても実際に書く量）を求め、
  Preferences の上限（`Max Extract Size`、既定 8 GiB、0 で無制限）と展開先の空き容量（`shutil.disk_usage`）を超えるなら何も書かずに `PackageError`。
- 書き出し時は展開先から対象までの各要素がシンボリックリンクでないことを確認し、リンクなら `PackageError`（既存のリンク経由で外側へ書くのを防ぐ）。展開先自体はユーザーが選んだ場所なのでリンクでもよい。
- 進捗: `wm.progress_begin/update/end`。

### 4.4 `core/unity_yaml.py` — Unity YAML サブセットパーサー

依存無しで `.mat` と `.meta` が読めれば十分。フル YAML は不要。

- ドキュメント分割: `--- !u!<classID> &<fileID>( stripped)?` 行で区切る。`%YAML` `%TAG` は無視。
- 対応構文:
  - ブロックマッピング `key: value` / `key:`（子ブロック）
  - ブロックシーケンス `- item`、および `- key: value`（Unity 頻出の「1 キー辞書のリスト」）
  - フローマッピング `{fileID: 0, guid: xxx, type: 3}`、フローシーケンス `[]`
  - スカラー: int / float / 文字列（クォート有無）。`-` 始まりの数値。`m_Name: ` の空値
  - 複数行に折り返された `{...}`（サンプルの prefab にあり）
  - 親より深いインデントの行に折り返された長いスカラー（`m_ShaderKeywords` や `m_TypeName` など）。YAML と同じく空白 1 つでつなぐ。
    クォート付きは閉じるまでつなぎ、ダブルクォートの行末 `\` では空白を入れない。クォート無しで続きの行が `key:` の形ならインデントの誤りとする
- 出力: `dict / list / str / int / float`。`{fileID, guid, type}` は `UnityRef` にする。
- 安全性: ブロック・フローともネスト深さに上限（`MAX_DEPTH` = 256）を置き、超えたら `UnityYamlError`（`ValueError` 派生）。
  万一の `RecursionError` も `UnityYamlError` に揃える。呼び出し側（`.mat` / `.meta` / prefab）は `ValueError` を捕捉してそのアセットだけスキップする。
- 検証: サンプルパッケージ内の全 `.mat` と全 `.meta` をパースして例外ゼロ。
- 将来 PyYAML を wheel 同梱する余地は残す（`blender_manifest.toml` の `wheels`）が、初期は専用パーサーで十分。

### 4.4.1 `core/unity_binary.py` — バイナリ形式の `.mat` / `.prefab`

プロジェクト設定の Asset Serialization が Mixed / Force Binary だと、`.mat` や `.prefab` は YAML ではなく
バイナリの SerializedFile でパッケージに入る（工業製品系アセットで確認。古い Unity の書き出しでは externalObjects も無いため、
prefab が読めないとマテリアルが一つも当たらない）。

- 判定: 先頭 20 バイトのヘッダー（ビッグエンディアン）の version と、ファイルサイズが実際の長さと一致するか（version 22 以降は 64 ビットの欄）。
  `load_documents(bytes)` がテキストかバイナリかを振り分け、`parse_material` / `parse_prefab` は bytes をそのまま受け取る。
- 対応範囲: version 14〜22（Unity 5.0 〜 Unity 6）で TypeTree が付いたもの（エディタが書くファイルには付いている）。
  形式の読み方は公開されているオープンソース実装（UnityPy、AssetStudio）の記述に従った。共通文字列表も同じ表を持つ。
- 読み方: 型ごとの TypeTree（blob 形式。ノードは 24 バイト、version 19 以降は 32 バイト）をたどり、値を YAML パーサーと同じ形に変換する。
  クラスは dict、配列は list、`map` は 1 要素 dict の list、`FastPropertyName` は中の文字列、`PPtr<...>` は externals の GUID
  （各バイトの上下 4 ビットが入れ替わった並び）から作った `UnityRef`。境界揃え（meta flag 0x4000）はオブジェクト先頭基準。
  組み込みリソース（`Resources/unity_builtin_extra`）は YAML と同じ GUID `0000000000000000f000000000000000` にする。
- 安全性: 全ての読み取りに範囲チェックを置き、件数は残りバイト数で上限を掛ける。TypeTree の階層は 1 バイトなので再帰は 256 段まで。
  例外は `UnityBinaryError`（`ValueError` 派生）に揃える。`[SerializeReference]` のデータなど読めないオブジェクトはそれだけ飛ばす。

### 4.5 `core/material.py` と プロファイル

```python
@dataclass
class UnityMaterial:            # .mat の生データ
    guid: str; name: str; shader_guid: str
    textures: dict[str, TexRef]   # prop → TexRef(guid, scale, offset)。fileID:0 は除外
    floats: dict[str, float]
    colors: dict[str, tuple4]
    render_queue: int
    keywords: list[str]           # m_ValidKeywords（+ 旧 m_ShaderKeywords）
    invalid_keywords: list[str]   # m_InvalidKeywords。別シェーダーから切り替えた名残の判定に使う

@dataclass
class NormalizedMaterial:       # シェーダー非依存の中間表現
    name: str
    family: str                          # "liltoon" | "standard" | "mtoon" | "poiyomi" | "vrchat_mobile" | "unknown"
    lighting: Literal["pbr", "toon", "unlit"]  # Auto モードでの Principled / Toon / Unlit 選択に使う
    base_color_tex: TexRef | None
    base_color: tuple4                   # _Color / _BaseColor。既定 (1,1,1,1)
    alpha_mode: Literal["opaque","cutout","blend"]
    alpha_cutoff: float
    normal_tex: TexRef | None; normal_strength: float
    emission_tex: TexRef | None; emission_color: tuple4
    metallic: float; roughness: float; metallic_tex: TexRef | None
    cull_backface: bool
    uv_scale: tuple2; uv_offset: tuple2
    extras: dict                         # カスタムプロパティ行き
    warnings: list[str]
```

**プロファイル判定順**
1. `shader_guids.json` の GUID 一致（サンプルの 4 GUID は lilToon として登録）
2. プロパティ指紋（GUID 未登録のシェーダー向け）
   - lilToon: `_BaseMap` と `_MatCapTex` と `_UseShadow` がある
   - Standard/URP: `_MainTex` or `_BaseMap` と `_Glossiness`/`_Smoothness` と `_Metallic`、かつ `_ShadowStrength` が無い
   - MToon: `_ShadeTexture` `_ShadeColor`
   - Poiyomi: `_MainTex` と `_Poi...` 系プロパティ
   - VRChat Toon Standard: `_ShadowBoost` `_MinBrightness` `_MetallicStrength` と `_Ramp` スロット（Standard の残骸を持つので Standard より先に判定）
3. どれにも該当しない → `generic`（`_MainTex`/`_BaseMap`/`_Color`/`_BumpMap`/`_EmissionMap`/`_Cutoff` の一般名だけで最善努力）

**lilToon プロファイルの変換規則（サンプルで確認した値）**

| Unity | NormalizedMaterial |
|---|---|
| `_MainTex`（無ければ `_BaseMap`, `_BaseColorMap`） | base_color_tex |
| `_Color` | base_color |
| `_BumpMap`（`_UseBumpMap == 1` のときのみ）、`_BumpScale` | normal_tex / normal_strength |
| `_EmissionMap`, `_EmissionColor`（`_UseEmission == 1` のときのみ） | emission |
| `_Metallic`, `_Smoothness`（`_UseReflection == 1` のとき。0 なら反射無しとみなし metallic 0 / roughness 1.0） | metallic / roughness |
| `_Cull` (0 Off / 1 Front / 2 Back) | cull_backface |
| `_TransparentMode` (0 Normal / 1 OnePass / 2 TwoPass)、`_SrcBlend/_DstBlend`、`m_CustomRenderQueue`、シェーダー名の `Cutout`/`Transparent` | alpha_mode。判定: DstBlend==10(OneMinusSrcAlpha) or queue≥3000 → blend、queue 2450〜2999 or `_Cutoff`>0.001 and `_AlphaMaskMode`>0 → cutout、それ以外 opaque |
| `_Cutoff` | alpha_cutoff |
| `_MainTex_ST` | uv_scale / uv_offset |
| `_ShadowColor` `_Shadow2ndColor` `_ShadowStrength` `_OutlineColor` `_OutlineWidth` `_MatCapTex` `_MatCap2ndTex` `_RimColor` `_AlphaMask` | extras（Phase 3 のトゥーンノードグループ用に温存） |

サンプルでの観測: 顔用の Transparent マテリアルはシェーダー名が Transparent 系で `_DstBlend: 10`, queue 2450 → `blend` と判定。衣装マテリアルは `_AlphaToMask: 1` → cutout 相当。

**Standard 系での emission の扱い**

`_EmissionColor` が黒でなければ emission を有効にするが、`_EMISSION` が `m_InvalidKeywords` にある場合は
現在のシェーダーに emission が無い（別シェーダーから切り替えた名残）とみなして無効にし、警告を出す。

HDRP（HDRP/Lit と HDRP 向け Shader Graph。`_EmissiveColor` を持つかで判定）は、発光しなくても `_EmissionColor` を白で持っている
（ベイク向けの互換用）ので、`_EmissionColor` は使わない。発光は `_EmissiveColor`（線形の HDR 色。`_UseEmissiveIntensity` なら
`_EmissiveColorLDR` × `_EmissiveIntensity`）と `_EmissiveColorMap` で決め、`_EmissiveColor` が黒なら光らせない。
HDRP の強度は物理単位（nits / EV100）で Blender の Emission Strength と対応しないため、発光色は最大成分で割った色味、
強さは 1.0 とし、元の色・強度・単位は `extras["hdrp_emissive"]` に残す。

**VRChat Mobile プロファイル（`vrchat_mobile.py`）**

VRChat SDK 同梱の Quest 向けシェーダー。機能が少なく、Standard から切り替えたマテリアルには `_Color` や
`_EmissionColor` の残骸が残りやすいので、各シェーダーが実際に参照するプロパティだけを読む。
`shader_guids.json` の `variant` でバリアントを選ぶ（GUID 無しで指紋判定できるのは Toon Standard だけ）。

| variant | シェーダー | lighting | 読むもの |
|---|---|---|---|
| `toon_lit` | Toon Lit | unlit | `_MainTex` のみ |
| `standard_lite` | Standard Lite | pbr | `_Color` `_BumpMap` `_Metallic` `_Glossiness` `_MetallicGlossMap` `_OcclusionMap`。emission は `_EMISSION` キーワードが有効なときだけ。不透明のみ |
| `toon_standard` | Toon Standard / (Outline) | toon | `_Color` `_Culling`、`USE_NORMAL_MAPS` / `USE_SPECULAR`（`_MetallicStrength` `_GlossStrength`）/ `USE_OCCLUSION_MAP` / `USE_MATCAP` / `USE_DETAIL_MAPS` / `USE_HUE_SHIFT` / `USE_COLOR_MASK` の各キーワードで機能を ON。emission は常に有効で `_EmissionStrength` を乗数に。Ramp・リム・MatCap・アウトライン等は extras。不透明のみ |
| `matcap_lit` | MatCap Lit | pbr | `_MainTex`、`_MatCap` は乗算として extras |
| `diffuse` / `bumped_diffuse` / `bumped_specular` | Diffuse / Lightmapped / Bumped Diffuse / Bumped Mapped Specular | pbr | `_MainTex`（+ `_BumpMap`、`_Shininess` → roughness） |
| `particle` | Particles/Additive, Alpha Blended, Multiply | unlit | `_MainTex`。半透明・両面。Additive / Multiply はアルファブレンドで近似し警告 |
| `ui` | Worlds/Supersampled UI, Sprites/* | unlit / pbr | `_MainTex` `_Color`。半透明・両面 |

### 4.6 `core/mapping.py`

```python
def resolve(model_entry, fbx_material_names, index) -> dict[str, str | None]:
    """FBX マテリアル名 → .mat GUID（解決不能なら None）"""
    ext = parse_external_objects(model_entry.meta_text)    # {name: guid}
    for name in fbx_material_names:
        guid = ext.get(name) \
            or ext.get(strip_suffix(name))                  # "Body.001" → "Body"
            or find_mat_by_name(index, strip_suffix(name))  # m_Name 一致
            or None
```

- Blender 側でも既存データと衝突すると `.001` が付くので、**FBX インポート前後の差分で得た Material オブジェクト**を対象にし、名前ではなく `bpy.data` の実体で扱う。差分取得後に `strip_suffix` して照合。
- `materialImportMode: 0`（マテリアル無し）の FBX でも externalObjects が空なら名前一致にフォールバック。

### 4.7 `blender/importer.py` オーケストレーション

```python
def run(ctx, filepath, opts) -> Report:
    pkg = UnityPackage(filepath); index = pkg.scan()
    models = choose_models(pkg, opts)                  # §3.3
    mats   = {e.guid: parse_material(pkg.read_asset(e.guid)) for e in pkg.materials()}
    norm   = {g: select_profile(m).normalize(m) for g, m in mats.items()}
    needed_tex = {t.guid for n in norm.values() for t in n.texture_refs()}
    paths  = pkg.extract(models_guids | needed_tex | (all_tex if opts.unreferenced), extract_root)
    for model in models:
        before = snapshot(bpy.data)
        bpy.ops.import_scene.fbx(filepath=paths[model.guid], **fbx_opts)
        new = diff(bpy.data, before)
        mapping = resolve(model, [m.name for m in new.materials], index)
        for mat in new.materials:
            guid = mapping[mat.name]
            if guid: build_material(mat, norm[guid], paths, opts, report)
            else:    report.unresolved(mat.name)
        collection_for(model).link(new.objects)         # モデルごとに Collection
    return report
```

- 1 パッケージ = 1 Collection（名前はパッケージ名）、その下にモデルごとの Collection。
- インポーターがシーンのルートコレクションに直接入れたオブジェクト・コレクション（VRM add-on のコライダー用コレクション等）はパッケージ用コレクションに移す。
- `.vrm` を VRM add-on に委譲した場合はマテリアルを組み直さず（method は `delegated`）、`.mat` 名が一致するものにカスタムプロパティだけ保存する。全モデルが委譲対象なら `.mat` 用テクスチャの展開も省く。
- `.mat` から組み直した後、インポーター由来で未使用になった画像（glTF の埋め込み画像など）は削除する。
- VRM 0.x の制限付きライセンス（CC-ND、VRoid Hub、UV License 備考あり）では add-on が確認ダイアログを出してその場では読み込まないため、オブジェクトが作られなかったことを警告する。自動承認はしない。
- 例外は各マテリアル単位で捕捉して警告にする。FBX インポート自体の失敗だけがエラー。
- `bpy.ops.import_scene.fbx` は `wm.fbx_import` の旧名。4.2 以降では両方存在するが、5.x 系では新 C++ インポーター（`bpy.ops.wm.fbx_import`）を優先し、無ければ旧名にフォールバック。

### 4.8 `blender/textures.py`

- `bpy.data.images.load(path, check_existing=True)`。
- カラースペース: `textureType == 1` or `sRGBTexture == 0` → `Non-Color`、それ以外 `sRGB`。
- `alpha_mode`: `alphaIsTransparency == 1` → `STRAIGHT`、そうでなくアルファをカット/ブレンドに使う場合は `CHANNEL_PACKED`。
- 画像ノードの `extension`: `wrapU == 1` → `EXTEND`、それ以外 `REPEAT`。
- **PSD は Blender で読めない** → 警告して展開のみ行う。TGA/TIFF/EXR/BMP/JPG/PNG は可。
- `Pack into .blend` ON なら `image.pack()`。

### 4.9 エラーと警告の分類

| 状況 | 扱い |
|---|---|
| tar.gz として開けない / GUID 構造でない | エラー（中断） |
| モデルファイルが 1 つも無い | エラー（中断） |
| FBX マテリアルに対応する .mat が無い | 警告、Names only 相当で継続 |
| .mat が参照するテクスチャ GUID がパッケージ内に無い（別パッケージ依存） | 警告、ノードは未接続で作る |
| 未知シェーダー | 警告、generic プロファイル |
| `_EMISSION` が無効キーワード（別シェーダーからの切り替え残骸） | emission を無効化し警告 |
| HDRP の白い `_EmissionColor`（`_EmissiveColor` が黒） | 発光させない（`_EmissiveColor` / `_EmissiveColorMap` だけを見る） |
| 非対応画像形式（PSD 等） | 警告、展開のみ |
| .mat の YAML 解析失敗 | 警告、そのマテリアルのみスキップ |

### 4.10 テスト

- `tests/test_unity_binary.py`: `tests/unity_binary_writer.py`（手書きの SerializedFile 生成器）で作ったバイナリの .mat / prefab を読み、同じ内容の YAML と結果が一致すること、エンディアン・version 22 の違い、切り詰めやバイト破壊で `ValueError` 以外の例外が出ないことを確認。合成パッケージにもバイナリの .mat と prefab を入れて統合テストで割り当てを確認する。
- `tests/test_unity_yaml.py`: リポジトリ同梱の **合成フィクスチャ**（手書きの最小 .mat / .meta）でパーサーを検証。`_BaseMap` の GUID、`_Cutoff`、`_Color` を assert。加えて `_local/` にサンプルがあれば、その全 .mat / .meta をパースして例外ゼロを確認（無ければ skip）。
- `tests/test_mapping.py`: externalObjects の `.001` 解決、名前一致フォールバック。
- `tests/test_units.py` / `tests/test_arrange.py`: 読み込む単位の候補（読み込めない prefab を理由付きで残す、既定の単位）と、並べ方の計算（1 列 / 格子、間隔、外形の無い単位）。
- `tests/test_transform.py` / `tests/test_hierarchy.py`: 行列計算と座標変換、階層の展開（stripped の対応表、上書き、削除、非アクティブ、循環・自己参照）。配置の数値は Unity 6 で作った調査用シーンに合わせ、Unity が書き出した頂点のワールド座標を再現できることを確かめる。
- 読み込む単位 Scenes は、`tests/make_synthetic_scene_package.py` の合成パッケージ（同じ形の FBX と、Unity の保存形式に合わせて手書きした prefab・シーン）を `tests/expectations_synthetic_scene.json` で確認する。配置した各メッシュの突起の頂点の座標（Unity が書き出した値）、非表示、マテリアルの差し替え、メッシュの共有を見る。
- 読み込む単位 Prefabs は、合成パッケージの `tests/expectations_synthetic_prefabs.json`（並べる）と `tests/expectations_synthetic_prefabs_stack.json`（原点に重ねる）で、prefab ごとのコレクションの中身・元の .mat・外形の重なりを統合テストで確認する。
- `tests/integration_import.py`（`blender -b --factory-startup --python`。symlink 先ではなくリポジトリの実体を直接 register する）: `_local/sample.unitypackage` をインポートし、`_local/expectations.json` に書いた期待値（オブジェクト数、マテリアル数、各マテリアルの接続テクスチャとカラースペース、render method）と照合する。期待値ファイルもサンプルも gitignore 対象で、リポジトリにはスキーマ説明（`tests/expectations.schema.md`）だけを置く。
- 見た目の確認は、`_local/` に置いた参考 .blend と並べて比較する手動項目とする。

---

## 5. 開発フェーズ

| Phase | 内容 | 完了条件 |
|---|---|---|
| **1 (MVP)** | Extension 雛形、tar 索引、Unity YAML パーサー、externalObjects マッピング、lilToon + generic プロファイル、Principled / Unlit 生成、テクスチャ展開、Info バー報告 | `_local/` のサンプルが 1 操作でテクスチャ付きで読める |
| **2** | モデル選択ダイアログ、N パネルレポート、Standard/URP/HDRP・MToon・Poiyomi プロファイル、prefab 経由マッピング、`.obj`/`.gltf`/`.dae`/`.blend` 同梱対応、未参照画像の読み込み、Preferences | 実装済み。合成パッケージ（`tests/make_synthetic_package.py`）と手元のサンプルで検証。他の実パッケージでの検証は入手次第 |
| **3** | トゥーン用ノードグループ（Shadow Color / MatCap / Rim / Emission を extras から再現）、Solidify によるアウトライン、カスタムプロパティからの再構築オペレーター、複数パッケージの一括インポート | 実装済み。手元のサンプルと合成パッケージで検証 |

---

## 6. 既知の制約・判断事項

- **検証用データはリポジトリに含めない。** unitypackage・展開物・参考 .blend・実パッケージ用の期待値 JSON は全て `_local/` 配下に置く。コミットするテスト用データは合成データだけ。アセット名などをドキュメントやコミットに書くのはかまわない（2026-09-14 に方針を変更）。

- **VRM は再実装しない**。VRM add-on が MToon・Humanoid・スプリングボーンを再現するので、入っていればそちらに委譲する。add-on 無しの環境向けの glTF フォールバックは、既存の glTF 経路と `.mat` 再構築の組み合わせに留める。
- **Unity のライティング再現はしない**。lilToon の影色・MatCap・リムは Phase 3 まではカスタムプロパティに保存するだけ。
- FBX の座標系・スケールは Blender 標準インポーターに委ねる。Unity の `globalScale` / `useFileScale` は参考値としてレポートに出すのみ。
- テクスチャの `maxTextureSize`（Unity 側の縮小設定）は無視し、元解像度で読み込む。
- 同一テクスチャが複数マテリアルから参照される場合は 1 つの Image を共有する。
- パッケージ内の `.blend` を直接 append する機能は Phase 2。マテリアルが既に付いている可能性が高く、その場合は本アドオンのマテリアル生成をスキップする選択肢を出す。
