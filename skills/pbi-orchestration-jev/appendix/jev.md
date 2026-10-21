# 付録: Jev の呼び出し方と運用の決まり

prepare.py の外で Jev を呼ぶのは、報告の状態と検証が食い違うときのゲート report と、差し戻し文の点検 rework_quality だけ。ゲート 1 (brief・pbi_quality) は prepare.py が呼ぶ。

## Jevの呼び出し方

同梱の Jev クライアント `<このディレクトリ>/jev/jev.py` を使う（使い方はリポジトリの README）。API キーはクライアントが読む: 環境変数 `TYPESAFE_API_KEY`、無ければ `~/.config/typesafe/credentials.json`。オーケストレーターはキーやその資格情報ファイルを読まない・表示しない。スクリプトや設定にキーを書かない。

```bash
python <このディレクトリ>/jev/jev.py docs/pbi/jev.config.json <ゲート名> <送る文章のファイル> > docs/pbi/jev/<PBI>.<ゲート名>.json
```

- 設定は `docs/pbi/jev.config.json`。無ければこのディレクトリの `jev.config.template.json` をコピーして作る。以後はそのファイルを読む
- API の形: noul の `criteria` は `{"1": "...", "0": "..."}`、score は段の説明の配列、choice は `{"選択肢": "説明"}`。答えは `answers.<キー>` に返る（noul は `noul`、score は `score`・`confidence`・`probabilities`、choice は `choice`・`confidence`）。応答の JSON は丸ごと表示せず、要るキーだけを取り出す
- 失敗したら共通クライアントが再試行する（429/529 は最大3回、5xx と接続エラーは1回）。だめならそのゲートを飛ばし、設計者が自分で判断する。完了報告の「発見」に書く。Jev が使えないことを理由に作業を止めない

運用の決まり。

- **変えたときだけ記録する**: jev-log には、prepare.py が判定の変化を書く。設計者が書くのは、決定表を上書きしたとき・引き継ぎ・Jev に report を聞いたときだけ。PBI の結果・差し戻し・トークン・コストは jev-log に自由な文で書かず、実行ごとに `record_run.py` で `docs/pbi/runs.jsonl` に記録する（SKILL.md 8 章）
- **しきい値と決定表は仮の値**: PBI が 10〜20 件たまったら、`model_report.py --archive` のモデル別の結果を添えて、決定表と警告のしきい値の見直しをユーザーに提案する（変えるかどうかはユーザーが決める）。sonnet の段の opencode の結果（差し戻しの率）と、引き継ぎのしきい値（250,000）も同じ時期に見直す。キャッシュの TTL は `agent_stats.py --all` の「1h TTL なら防げた」の量と 1h 書き込みの量を比べて見直しを提案する。変えるときは `select_model.py` と `prepare.py` の定数を直し、変えた理由を記録する
- **送ってよいのは文章だけ**: PBI本文、要約、実装担当の報告、差し戻し文。ソースコード、diff、環境変数、認証情報、顧客データは送らない
- **数えさせない**: 件数・回数・日付の比較は Jev に尋ねず、facts と STATUS.md で扱う
