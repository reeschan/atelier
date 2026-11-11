# atelier の記録の棚卸し (2026-11-11)

[harness-metrics.md](../specs/harness-metrics.md) を決める前に、atelier が今残している記録を調べた結果。PR #40・#41 (設計の受け渡しと、その指摘の直し) の後のコードを見ている。パスは `skills/atelier/` からの相対で書く。

## 記録の一覧

| 記録 | 書く人・いつ | 主な中身 | 読む人 | 外に出せないもの | 結び付けキー |
|---|---|---|---|---|---|
| `docs/pbi/runs.jsonl` | `record_run.py`、合否を決めた直後 | attempt・step・runner・model・tier・時間・トークン・費用・verify・judged | model_report・export_cases・conductor の進みの判定 | repo 名・ブランチ名・保全先の絶対パス | work_key・pbi・report+attempt |
| `docs/pbi/cases.jsonl` | `export_cases.py`、finish で全件を書き直す | worker.md の本文・PBI の本文・結果 | 外のハーネス | 本文 | case id・work_key |
| `docs/pbi/stats/roles.json` | `agent_stats.py --out --run`、finish | 役割ごとの requests・tokens・usd | 完了報告 | なし | conductor の run_id (PBI 別は無い) |
| `docs/pbi/.kiln/compact-events.jsonl` | `kiln_log.py`、区切りごと | 境界・役割・圧縮前のトークン・action・note | 完了報告 | note (自由文) | **kiln 独自の run_id** |
| `docs/pbi/conductor/run.json` | `conductor.py`、状態が変わるたび | run_id・question・answers・history (seq・episode・status・roles) | agent_stats・status | goal・summary・作業ルートの絶対パス | run_id・seq |
| `docs/pbi/design-approvals.jsonl` | `design_record.py check-reply` | design_id・revision・問いと答え | conductor の二重記録の防止 | 問いと答えの本文 | design_id |
| `docs/pbi/adapters.json` | `lint_run.py approve` と設計者の手書き | sends・`map.decision`・任意工程の決定 | resume・lint_run・finish | なし | なし |
| stage record | 設計者が STATUS.md・HANDOFF.md に書く 1 行 | `stage …: <comp> run/skip/fallback/off (理由)` | 承認一覧・完了報告 | 理由の文 | STATUS.md の Work key |
| 出番の結果・review・test-result | 設計者・レビュー担当・テスト担当 | status・question・findings・failing.cause | conductor・bundle | 自由文 | ファイル名の run8・seq |
| session-log (別スキル) | Stop/SessionEnd のフック | 会話の全文と台帳の 1 行 | 人 | 全文 | session_id |

## 重複

- PBI ごとのトークンと費用が、runs.jsonl・cases・stats/roles.json・session-usage にあり、数え方が違うので合計が合わない。
- findings が runs の `judged.findings` (設計者が手で渡す) と review.json の両方にある。
- run_id が 2 つある (conductor と kiln)。`roles.json` も 2 つある (設定と統計)。

## 抜け

- 工程ごとの所要時間: `launched_at` は history へ移すときに捨てられる。
- 問いの数と待ち時間: 問いを出した時刻が残らない。kind が粗く、使えない runner・OMM・recheck・不明点を区別できない。inline の AskUserQuestion は何も残らない。
- 費用: agent で起こした設計者の出番は測れない。レビュー担当とテスト担当は run 全体の合計しかない。
- 差し戻しの原因: `--cause` は省ける。省くと null のまま残る。
- 後ろ盾に落ちた率: `fallback` は stage record の文の中にしかない。設計者の general-purpose 代替は標準出力だけ。
- タスクの難しさとジャンル: どこにも無い。

## #40・#41 で入った問いの残り方

- 使えない runner の decision: 答えは自由文。何を選んだかは、次の attempt の `--step` からしか分からない。
- OMM の repo・cache・skip: `adapters.json` の `map.decision` (設計者の手書き)。
- recheck の skip と「報告だけ」: stage record の文と `jevlint/recheck.md` の行だけ。
- plan の前の不明点: 答えは自由文と spec。
- Approve の ask: design-approvals.jsonl。confirm が off・照合に落ちた・inline のときは残らない。
- 役割の値の出所: run.json の `roles[].from` に構造化されて残る。
