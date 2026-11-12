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

## 関連記事・リンク

このリポジトリの背景や、AIを使った開発・運用の考え方は note の連載「CAD専DTのAI活用術」で書いています。

- 第1弾: [歯科技工の「面倒くさい」を潰すために作ってきたツールを、GitHubに公開しました](https://note.com/aicreate_dt/n/nded948e150bc)
- 第2弾: [ソフトの使い方を一番知っているのは、開発者ではなくあなた自身](https://note.com/aicreate_dt/n/n25fe39fc2650)
- 第3弾（有料）: [頼み方ひとつで結果が変わる ― 実物パック](https://note.com/aicreate_dt/n/n810048324d0a)
- 第5弾: [AIは1つで足りるのか ― Codex・Grok・Claude Codeを3つ契約して分担させてみた実感](https://note.com/aicreate_dt/n/ne0de4a60c2a1)
- 第6弾: [統括役は誰に頼むのか ― Muse に各AIを「内蔵」させて使ってみた使用感](https://note.com/aicreate_dt/n/nc82f91b7c9ae)
- 第7弾: [AIは「答える」か「作業する」かで選ぶ](https://note.com/aicreate_dt/n/na4a18c742e58)
- 第8弾: [自腹で始めて、実績で取り返す ― AIの主力プランを会社負担にしてもらうまでの話](https://note.com/aicreate_dt/n/nd8588ffbbf18)
- Zenn: [職場の内製スクリプトを公開リポジトリに切り出す](https://zenn.dev/pero819412/articles/7bb8462b200fd3)
- X: [@CADCAM_DT](https://x.com/CADCAM_DT)
