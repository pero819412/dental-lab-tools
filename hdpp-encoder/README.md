# hdppエンコーダー

exocad で設計した製作物（STL）を、DWOS 系 CAM がそのまま読める **STL＋HDPP のペア**に変換するツールです。
exocad が症例フォルダに書き出す `.constructionInfo`（XML）から歯式・製作種別・材料・マージン点群・挿入方向を読み取り、
DWOS 互換の `.hdpp`（XML）を生成して、STL と同じ名前で並べて出力します。

## どんな「痒いところ」を解消するか

- **exocad → DWOS 系 CAM の受け渡しで、HDPP を手作業で作り直す手間をなくす。**
  マージン・材料・方向を CAM 側で入れ直さなくても、STL と HDPP をセットで出せます。
- **ファイル名の付け直しを自動化。** 材料・種別に応じたプレフィックス（`cr_` / `CCbr_` / `Tiin_` / `imp_` など）、
  日付プレフィックス（`YYYY-MM-DD_`）の除去、歯式の補完、`_cad` 終端、任意で作業者名の末尾付与を行います。
- **誤った振り分けを防ぐ。** 判定できない材料、インプラントのマージン不足、STL のハッシュ不一致
  （constructionInfo の SHA-1/SHA-256 と照合）などは、推測で出力せずにエラーで止めます。
- **ブリッジの歯単位判定。** スクリュー固定ブリッジ全体のフラグにつられて、ポンティックや天然歯支台を
  インプラント扱いしないよう、歯ごとの情報で判定します（ポンティックは HDPP のマージン生成から除外、STL の形状と歯番は保持）。
- **支台読みインプラント**（PreparationMargin を MatrixToScanDataFiles × ZRotationMatrix で座標変換）にも対応。
- **安全な書き出し。** 一時ファイル → 検証 → `os.replace` で確定。同名・同内容なら「既存一致」で何もしない。
  内容が違う場合は `(1)` 付きで別名保存、または旧版を `過去データ/` へ退避してから新版を出力します。
- **監視フォルダの自動変換。** ON にした時点で既にあるフォルダは基準登録して触らず、
  その後に作られた症例フォルダだけを、ファイルが書き込み終わって安定してから変換します。

## 動作環境

- Python 3.10 以上
- GUI を使う場合: `pip install PyQt6`（CLI と IPC は PyQt6 なしで動きます）
- 主に Windows（共有フォルダへの出力、エクスプローラーで開く）を想定。CLI は他 OS でも動作します。

## 使い方

```bash
# GUI（ドラッグ＆ドロップ / フォルダ選択 / 自動監視）
python -m hdpp_encoder

# CLI: 症例フォルダを1回だけ変換し、結果を JSON で表示
python -m hdpp_encoder --encode "C:\path\to\case_folder" --output "\\SERVER\Share\Output"

# オプション
#   --staff-name NAME     出力ファイル名の末尾に _NAME を付ける
#   --material GROUP      constructionInfo に材料がない場合の材料（ZR/CC/TI/WAX/TEMP/PEEK）
#   --also-source         症例フォルダ内にも STL＋HDPP を出力
#   --archive-existing    内容が違う既存ペアを 過去データ/ に退避してから出力
```

`hdpp_encoder/` を含むディレクトリ（このリポジトリの `hdpp-encoder/`）をカレントにするか、`PYTHONPATH` に入れて実行してください。

## 必要な設定

**出力先は必須です。既定値はありません。** 未設定（または見本のダミー値のまま）だと変換・自動監視は開始しません。

1. `config.example.json` を設定ファイルの場所へ `settings.json` としてコピーします。
   - 既定の場所: Windows は `%APPDATA%\hdpp_encoder\settings.json`、その他は `~/.config/hdpp_encoder/settings.json`
   - 環境変数 `HDPP_ENCODER_HOME`（データ置き場ごと変更）または `HDPP_ENCODER_CONFIG`（設定ファイルのパス）で変更できます。
   - GUI の「出力・監視・作業者設定」から保存しても作成されます。
2. 各項目を自分の環境に合わせて書き換えます。

| キー | 内容 |
|---|---|
| `output_path` | STL＋HDPP の出力先（例 `\\SERVER\Share\Output` はダミー。**必ず書き換える**） |
| `watch_path` | 自動変換で監視するフォルダ（exocad の症例フォルダが作られる場所）。自動変換を使わないなら空でよい |
| `staff_name` | ファイル名末尾に付ける作業者名。空なら付けない |
| `auto_enabled` | 起動時に自動変換を ON にするか |
| `material_names` | `.hdpp` の `<material>` に書く名前。**各ラボで使用している CAM 材料ライブラリの名称をそのまま記入してください**。既定値は用意していないので、未設定の材料グループを使うとエラーになります |

ログ: `<データ置き場>/logs/hdpp_encoder.log`、IPC: `<データ置き場>/ipc/`

## 他のツールからの連携（IPC）

`hdpp_encoder.ipc` はファイルベースの依頼キューです。別プロセスから

```python
from hdpp_encoder import ipc
ipc.enqueue_encode(r"C:\path\to\case_folder", source="my_tool", material_override="ZR")
```

で依頼を置き、常駐側で `claim_requests()` → `app.encode_case(...)` → `finish_request()` と処理します。
（常駐サービス本体はこの抜き出しには含まれていません。）

## 注意

- 生成した HDPP のマージン・材料・挿入方向は、**切削前に必ず CAM 上で確認してください。**
- `.hdpp` の creator は `DWOS 16.2.4` として書き出します（`core.HDPP_CREATOR_*`）。
- 実 CAM での読込・切削結果の保証はありません。
