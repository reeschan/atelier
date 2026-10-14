# 付録: Jev の呼び出し方と運用の決まり

prepare.py を使わずに Jev を直接呼ぶとき (報告のゲート 2、差し戻しのゲート 3) に読む。ゲート 1 (brief・pbi_quality) は prepare.py が呼ぶ。

### 3.2 Jev に短い要約で聞く（ゲート1a）

PBI全文は送らない。次の形の要約（8行以内）を `docs/pbi/PBI-<連番>.brief.md` に書いて送る。

```
目的: <1文>
変更の種類: <既存パターンの置き換え / 既存パターンの追加 / 新しい仕組み>
新しい抽象: <なし / 型・関数の追加 / モジュール間の新しい契約>
外部とのやりとり: <なし / HTTP n 本 / ストレージ / …>
守るべき不変条件: <なし / 具体的に>
判断が要る点: <なし / 具体的に>
```

ゲートは `brief`（`novelty` / `coupling` / `ambiguity`、各 score 0〜2）。全 PBI 分を 1 回の Bash で呼ぶ（「Jevの呼び出し方」のループ）。

## 4. ゲート1b: PBIの品質の警告

PBI全文を Jev に渡し、ゲート `pbi_quality` を聞く（全 PBI 分を 1 回の Bash で）。合否には使わず、警告として承認一覧に出す。書き直しは警告が出たときに1回まで（任意）。

| キー | 警告の条件 |
|---|---|
| targets_named / off_limits_named / criteria_checkable / commands_runnable | 0.6 未満 |
| needs_outside_context | 0.4 より大きい |

## Jevの呼び出し方

同梱の Jev クライアント `<このディレクトリ>/jev/jev.py` を使う（使い方はリポジトリの README）。API キーはクライアントが読む: 環境変数 `TYPESAFE_API_KEY`、無ければ `~/.config/typesafe/credentials.json`。オーケストレーターはキーやその資格情報ファイルを読まない・表示しない。スクリプトや設定にキーを書かない。

```bash
python <このディレクトリ>/jev/jev.py docs/pbi/jev.config.json <ゲート名> <送る文章のファイル> > docs/pbi/jev/<PBI>.<ゲート名>.json
```

複数の PBI に同じゲートを聞くときは 1 回の Bash でループし、答えだけを 1 行ずつ出す。

```bash
mkdir -p docs/pbi/jev
for f in docs/pbi/PBI-*.brief.md; do p=$(basename "$f" .brief.md)
  python <このディレクトリ>/jev/jev.py docs/pbi/jev.config.json brief "$f" > "docs/pbi/jev/$p.brief.json" 2>/dev/null || echo "$p: jev failed"
done
```

- 設定は `docs/pbi/jev.config.json`。無ければこのディレクトリの `jev.config.template.json` をコピーして作る。以後はそのファイルを読む
- API の形: noul の `criteria` は `{"1": "...", "0": "..."}`、score は段の説明の配列、choice は `{"選択肢": "説明"}`。答えは `answers.<キー>` に返る（noul は `noul`、score は `score`・`confidence`・`probabilities`、choice は `choice`・`confidence`）。応答の JSON は丸ごと表示せず、要るキーだけを取り出す
- 失敗したら共通クライアントが再試行する（429/529 は最大3回、5xx と接続エラーは1回）。だめならそのゲートを飛ばし（3.3 の既定値を使う）、完了報告の「発見」に書く。Jev が使えないことを理由に作業を止めない

運用の決まり。

- **すべて記録する**: 判定ごとに、PBI、ゲート、キー、答え、confidence、採った行動を `docs/pbi/jev-log.jsonl` に1行追記する。モデルを決めたときは `facts` と `select_model.py` の出力も記録し、PBIの結果（一発合格 / 差し戻し回数 / 昇格）を完了時に追記する
- **しきい値と決定表は仮の値**: PBI が 10〜20 件たまったら、モデル別の結果から決定表と警告のしきい値の見直しをユーザーに提案する。sonnet の段の opencode の結果（差し戻し・昇格の率）と、引き継ぎのしきい値（250,000）も同じ時期に見直す。キャッシュの TTL は `agent_stats.py --all` の「1h TTL なら防げた」の量と 1h 書き込みの量を比べて見直しを提案する。変えるときは `select_model.py` と `jev.config.json` を直し、変えた理由を記録する
- **送ってよいのは文章だけ**: PBI本文、要約、実装担当の報告、差し戻し文。ソースコード、diff、環境変数、認証情報、顧客データは送らない
- **数えさせない**: 件数・回数・日付の比較は Jev に尋ねず、facts と STATUS.md で扱う
