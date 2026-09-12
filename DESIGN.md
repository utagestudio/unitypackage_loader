# unitypackage_loader — 設計書

Blender から `.unitypackage` を直接読み込み、メッシュ（アーマチュア・シェイプキー込み）とマテリアル／テクスチャの設定までを一度に行うアドオンの設計。
対象 Blender: **4.2 以降（Extension 形式）**、開発・検証環境は **5.2 LTS / Python 3.13**。

---

## 0. サンプル調査で分かった前提

手元にある VRChat 向けアバターの unitypackage 1 種を調査した。配布物のためリポジトリには含めず、`_local/`（gitignore 済み）に置いて開発・検証に使う。本書ではアセット名を伏せ、マテリアル名などは一般化して記載する。

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
3. **フォールバック B: prefab の `MeshRenderer` / `SkinnedMeshRenderer` の `m_Materials`** — prefab 内の同名 GameObject が同じスロットに持つ .mat を採用する。ネストされた PrefabInstance の上書き（`m_Modifications`）は対象 fileID が FBX 内部 ID のため名前に結び付けられず対象外。

**スロット単位の分割**: FBX 内では少数のマテリアルを全メッシュが共有し、Unity 側では prefab の Renderer ごとに別の .mat を割り当てているパッケージがある（工業製品系アセットで確認）。この場合「FBX マテリアル 1 つ = .mat 1 つ」では色もテクスチャも失われるため、prefab の (GameObject, スロット) → .mat 表を作り、FBX マテリアルの解決結果と異なるスロットは .mat 名の Blender マテリアルに差し替える。同じ .mat は 1 つの Blender マテリアルを共有し、使われなくなった FBX マテリアルは削除する。prefab が複数ある（車体色違いなど）場合は、モデル選択ダイアログで使用する prefab を選ぶ（既定はパス順で最初）。

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
- シェーダー本体（`.shader` `.cginc`）、C#（`.cs` `.dll`）、アニメーション（`.anim` `.controller`）、Expression/Parameter アセット、PhysBone 等の MonoBehaviour、prefab 階層、アイコン画像、`preview.png`。

### マテリアルの対応方針
Unity と 1:1 は不可能なので、**「Unity マテリアル → シェーダー非依存の中間表現（NormalizedMaterial）→ Blender ノード」** の 2 段変換にし、シェーダーごとの差は「プロファイル」に閉じ込める（§4.5）。
中間表現に落とせなかった値も **カスタムプロパティとして保存**し、後から手動再現できるようにする。

---

## 2. ユーザーフロー

```
File > Import > Unity Package (.unitypackage)      .unitypackage を 3D View にドラッグ＆ドロップ
        │                                              │（FileHandler → 同じオペレーターを filepath 付きで invoke）
        ▼                                              ▼
 ファイルブラウザ（右側にオプション）      ← §3.1   オプションのポップアップ（invoke_props_dialog）
        │  [Import]                                    │  [Import]
        ▼                                              ▼
 パッケージ走査（索引作成、モデル候補列挙）
        │
        ├─ モデルが 1 つ & 「毎回確認」OFF → そのまま続行
        └─ それ以外 → モデル選択ダイアログ    ← §3.3
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

**Model**
| 項目 | 型 / 既定値 | 説明 |
|---|---|---|
| Models to import | Enum: `All` / `Ask` / `First only` = `Ask` | `Ask` は複数ある時だけ §3.3 のダイアログを出す |
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

### 3.3 モデル選択ダイアログ（`invoke_props_dialog`）

「Ask」指定で、パッケージ内に複数モデルがある時、または prefab が複数ある時に表示。prefab が複数あれば、マテリアル割り当てに使う prefab のドロップダウンも出す。

```
┌ Import from Avatar_v1.0.unitypackage ─────────────────┐
│ Models                                                 │
│  [x] Assets/Avatar/FBX/Avatar_v1.0.fbx   12.3 MB  5 mat│
│  [ ] Assets/Avatar/Prefabs/Avatar_LOD.fbx ...          │
│                                                        │
│ Materials: 7 found (7 resolved, 0 unresolved)          │
│ Textures : 20 referenced, 3 missing from package       │
│                                       [Cancel] [Import]│
└────────────────────────────────────────────────────────┘
```
`UIList` + `CollectionProperty`。行に「マッピング解決数」を出して、読み込む前に問題が見える状態にする。

### 3.4 完了レポート

- **Info バー**: `Imported 20 objects, 7 materials (7 mapped), 12 textures. 2 warnings — see Unity Package panel.`
- **3D View > N パネル > "Unity Package" タブ**: 直近のインポートのレポート（読み取り専用）
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
│   └─ select_models.py          # UNITYPKG_OT_select_models（invoke_props_dialog）
├─ ui/
│   ├─ panel_report.py           # N パネル
│   └─ preferences.py
├─ core/                         # bpy 非依存。純 Python で単体テスト可能
│   ├─ package.py                # tar 走査・索引・オンデマンド展開
│   ├─ unity_yaml.py             # Unity YAML サブセットパーサー
│   ├─ meta.py                   # ModelImporter / TextureImporter の読み取り
│   ├─ material.py               # UnityMaterial / NormalizedMaterial dataclass
│   ├─ mapping.py                # FBX マテリアル名 → .mat 解決
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
- 出力: `dict / list / str / int / float`。`{fileID, guid, type}` は `UnityRef` にする。
- 安全性: ブロック・フローともネスト深さに上限（`MAX_DEPTH` = 256）を置き、超えたら `UnityYamlError`（`ValueError` 派生）。
  万一の `RecursionError` も `UnityYamlError` に揃える。呼び出し側（`.mat` / `.meta` / prefab）は `ValueError` を捕捉してそのアセットだけスキップする。
- 検証: サンプルパッケージ内の全 `.mat` と全 `.meta` をパースして例外ゼロ。
- 将来 PyYAML を wheel 同梱する余地は残す（`blender_manifest.toml` の `wheels`）が、初期は専用パーサーで十分。

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
| 非対応画像形式（PSD 等） | 警告、展開のみ |
| .mat の YAML 解析失敗 | 警告、そのマテリアルのみスキップ |

### 4.10 テスト

- `tests/test_unity_yaml.py`: リポジトリ同梱の **合成フィクスチャ**（手書きの最小 .mat / .meta）でパーサーを検証。`_BaseMap` の GUID、`_Cutoff`、`_Color` を assert。加えて `_local/` にサンプルがあれば、その全 .mat / .meta をパースして例外ゼロを確認（無ければ skip）。
- `tests/test_mapping.py`: externalObjects の `.001` 解決、名前一致フォールバック。
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

- **検証用データはリポジトリに含めない。** unitypackage・展開物・参考 .blend・期待値 JSON は全て `_local/` 配下に置き、コミットするファイル（ドキュメント、テスト、コメント）にアセット固有の名称や数値を書かない。

- **VRM は再実装しない**。VRM add-on が MToon・Humanoid・スプリングボーンを再現するので、入っていればそちらに委譲する。add-on 無しの環境向けの glTF フォールバックは、既存の glTF 経路と `.mat` 再構築の組み合わせに留める。
- **Unity のライティング再現はしない**。lilToon の影色・MatCap・リムは Phase 3 まではカスタムプロパティに保存するだけ。
- FBX の座標系・スケールは Blender 標準インポーターに委ねる。Unity の `globalScale` / `useFileScale` は参考値としてレポートに出すのみ。
- テクスチャの `maxTextureSize`（Unity 側の縮小設定）は無視し、元解像度で読み込む。
- 同一テクスチャが複数マテリアルから参照される場合は 1 つの Image を共有する。
- パッケージ内の `.blend` を直接 append する機能は Phase 2。マテリアルが既に付いている可能性が高く、その場合は本アドオンのマテリアル生成をスキップする選択肢を出す。
