# dental-lab-tools

歯科技工の現場で使う、小さな便利ツールの公開版コレクションです。
大きなシステムではなく、「その手順でだけ発生する面倒」を潰す単機能ツールを、
機能ごとに細かく切り出して公開しています。

Small, single-purpose tools for dental laboratory work — the kind that fix
one specific annoyance in a daily workflow — extracted and published in
small, focused pieces.

## Tools

### hdpp-encoder/
exocad が書き出す `.constructionInfo` を読み取り、CAM（DWOS）向けの
`.hdpp` ファイルを STL とセットで生成するエンコーダー。
材料・歯式・マージン・挿入方向を CAM 側で入れ直す手間をなくします。
詳しくは [hdpp-encoder/README.md](hdpp-encoder/README.md) を参照。

### small-tools/
単体で使える小物ツール集。
- `shade_name_converter.py` — シェード名の表記変換
- `design_file_move_queue.py` / `design_move_transaction.py` — 安全なファイル移動（キュー／トランザクション）
- `related_model_files.py` — 症例に紐づく関連モデルファイルの収集
- `rename_destination_policy.py` / `rename_focus_safety.py` — リネーム時の事故防止ポリシー
- `windows_name_sort.py` — Windows エクスプローラー式の名前順ソート
- `file_drop_paths.py` — ドラッグ＆ドロップされたファイルのパース

## 設定とパスについて

サンプルや設定例に書かれているフォルダパス・サーバ名・材料名は、
すべてダミー、または未設定（空）です。各ラボの環境に合わせて、
ご自身で使用しているパスや名称を設定ファイルに記入して使ってください。

## ライセンスとクレジット

- 基本ライセンス: MIT License（[LICENSE](LICENSE)）
- AI利用について: AIへの入力・改変は許可します。公開する場合は
  クレジットをお願いしています。詳細は [AI_USE_NOTICE.md](AI_USE_NOTICE.md)

> Original: pero819412 — https://github.com/pero819412/dental-lab-tools
