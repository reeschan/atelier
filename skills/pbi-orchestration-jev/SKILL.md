---
name: pbi-orchestration-jev
description: 複数ファイルにまたがる実装を、PBI起票・モデル振り分け・サブエージェント委任・レビューで進める。Jevで起票内容と報告を点検する。Codex / Antigravity / Claude Code のサブエージェントに振り分け、差し戻しは2回までで、超えたらユーザー判断。
---

# PBIオーケストレーション（Jevゲート付き）

1ファイルで済む小さな修正にはこのスキルを使わず、直接やる。

モデルは「オーケストレーターが数えた事実 + Jev の短い判断 + 決定表（`select_model.py`）」で決める。決定表は段（haiku / sonnet / opus）と、上から順に試す実行先の並び（`route`）を出す。実行先は Codex CLI、Antigravity CLI（`agy`）の Claude、Claude Code の Agent ツールの 3 つで、使えなければ次へ進む（6章）。PBI の品質点検は合否ではなく警告。差し戻しは問題の要約から修正の規模を Jev に聞き、haiku の段は 1 段だけ上げてよい。

## 役割

| 担当 | やること | やらないこと |
|---|---|---|
| オーケストレーター（メインセッション） | 設計、PBI起票、事実の数え上げ、要約の作成、レビュー、差し戻し | 実装、モデルを勘で決めること |
| サブエージェント / Codex / Antigravity | PBIの実装と検証コマンドの実行 | PBIの範囲外の変更 |
| Jev | 短い要約に対する判断（新規性・結合度・あいまいさ・修正の規模）、報告の仕分け、PBIの品質の警告 | 合否判定、数を数えること、diffのレビュー |
| `select_model.py` | 事実と Jev の score から段と実行先の並び（`route`）を決める | — |
| `agent_stats.py` | 会話記録からモデル別トークン数・キャッシュの作り直しを集計する | 会話記録の書き換え |

合否は検証コマンドの実行結果で決める。回数は `docs/pbi/STATUS.md` で決める。モデルは `select_model.py` の出力で決める。

## 0. 止まる場面

止まってユーザーを待つのは次の3つだけ。

1. PBI一覧の承認（5章）
2. 差し戻し上限への到達、または修正の規模が「設計の見直し」と判定されたとき（10章）
3. 破壊的な操作の前（データ削除、force push、リポジトリ外への変更）

それ以外では報告のために止まらない。状況メモは次の行動と同じメッセージに書いて進める。

## 1. 状態ファイル

最初に `docs/pbi/STATUS.md` を作り、状態が変わるたびに更新する。状態と回数は必ずこのファイルから読む。

```markdown
| PBI | モデル | 状態 | 差し戻し | 昇格 | 続行指示 | メモ |
|---|---|---|---|---|---|---|
| PBI-001 | codex medium（haiku 相当） | 実行中 | 1/2 | 0/1 | 0/2 | 条件2がfail |
```

状態は「未着手 / 実行中 / レビュー中 / 完了 / 保留 / 上限到達」のいずれか。作業中に見つけた追加作業は新しいPBIとして行を足す。

## 2. 分解と起票

既存コードを調べて設計を決め、PBIに分解する。`docs/pbi/PBI-<連番>.md` に1件1ファイルで書く。サブエージェントは設計時の文脈を持たないので、PBIだけを読んで作業できるように書く。haiku に渡せるよう、1 PBI は小さく（変更ファイル 3 以下を目安に）切る。

```markdown
# PBI-<連番>: <タイトル>

- 担当モデル: <select_model.py の出力>
- 依存: <先に終わっている必要があるPBI。なければ「なし」>

## 目的
<なぜ必要か、全体の中での位置づけを2〜3文で>

## 対象
<変更するファイル・関数。新規作成ならパスと役割>

## 触らない範囲
<変更してはいけないファイル、API、スキーマ、既存テスト>

## 受け入れ条件
1. <検証コマンドの番号と、期待する出力>

## 検証コマンド
<番号付きで、リポジトリのルートからそのまま実行できる形>

## 補足
<従うべき既存の実装パターン、参照するファイル、落とし穴>
```

## 3. 担当モデルの決め方

### 3.1 事実を数える（オーケストレーター）

PBIごとに `docs/pbi/PBI-<連番>.facts.json` を書く。

```json
{
  "files_changed": 3,
  "files_new": 1,
  "files_deleted": 0,
  "modules": 1,
  "criteria": 5,
  "sensitive": false
}
```

- `files_changed`: 変更・新規・削除を合わせたファイル数（テストを含む）
- `modules`: 変更するファイルが属する機能単位の数（例: `src/services/store` と `e2e/` で 2）
- `sensitive`: 認証・権限・DB スキーマ・ユーザーデータの削除・秘密情報のいずれかに触れるか。リポジトリ内のファイル削除は含めない

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

ゲートは `brief`（`novelty` / `coupling` / `ambiguity`、各 score 0〜2）。

### 3.3 決定表（`select_model.py`）

```bash
python <このディレクトリ>/select_model.py docs/pbi/PBI-001.facts.json docs/pbi/jev/PBI-001.brief.json
```

| 条件（上から順に評価） | モデル |
|---|---|
| `sensitive` が true、または `coupling` = 2 | opus |
| `novelty` = 2 かつ `ambiguity` = 2 | opus |
| `novelty`・`coupling`・`ambiguity` のどれかが 1 以上、または `files_changed` ≥ 4、または `modules` ≥ 2 | sonnet |
| 上のどれでもない | haiku |

上で決まった段から、試す順の実行先（`route`）が決まる。

| 段 | 1. Codex（`select_model.py` の `CODEX_UNTIL` まで） | 2. Antigravity（`agy`） | 3. Agent ツール（Anthropic のサブスク） |
|---|---|---|---|
| haiku | `gpt-6-astra` effort `medium` | `claude-sonnet-5-5-low` | haiku、effort `max` |
| sonnet | `gpt-6-astra` effort `high` | `claude-sonnet-5-5-high` | sonnet、effort `high` |
| opus | （使わない） | `claude-opus-5-5-high` | opus、effort 既定 |

- Jev の score は確率の最も高い段を使う。confidence が 0.7 未満なら 1 段下げて扱う（haiku を使いやすくするため）。
- Jev が使えないときは score をすべて 1 として扱う（= sonnet 以上）。
- 期限の判定は実行した日の日付で行う（試すときは `--today YYYY-MM-DD`）。
- 出力（段、`route` の先頭、理由）を PBI の「担当モデル」と STATUS.md に写す（例: `codex high（sonnet 相当）`）。オーケストレーターが上書きしたいときは、理由を STATUS.md のメモと jev-log に書く。

## 4. ゲート1b: PBIの品質の警告

PBI全文を Jev に渡し、ゲート `pbi_quality` を聞く。合否には使わず、警告として承認一覧に出す。書き直しは警告が出たときに1回まで（任意）。

| キー | 警告の条件 |
|---|---|
| targets_named / off_limits_named / criteria_checkable / commands_runnable | 0.6 未満 |
| needs_outside_context | 0.4 より大きい |

## 5. 承認

PBIの一覧をユーザーに見せ、承認を得てから実行に入る。一覧には、ID、タイトル、担当モデルとその理由（`select_model.py` の reasons）、依存、品質の警告を載せる。

## 6. 実行

- PBIごとに `route` の先頭から試す。runner が codex なら 6.1、agy なら 6.2、agent なら Agent ツール（`model` と `effort` は route の値。effort が null なら付けない）で動かす
- 実行先が使えないとき（6.1・6.2 の条件）は `route` の次へ進む。STATUS.md のモデルを書き換え、メモに理由を書く。PBI の失敗とは数えない
- 差し戻しで起動し直すときは、そのとき使っている実行先・モデル・effort のまま渡す（昇格したときは 9.1）
- Agent ツールの `description` は必ず `PBI-<連番> <短い説明>` で始める（例: `PBI-003 config frontmatter`）。差し戻しで新しいサブエージェントを起動するときも同じ PBI 番号で始める（例: `PBI-003 rework 1`）。統計はこの番号で PBI に結び付ける
- 渡すのはPBIファイルの全文と、次の報告形式だけ
  - 検証コマンドの出力（番号ごとにそのまま貼る）
  - 変更箇所の一覧（ファイル:行）
  - 受け入れ条件ごとの自己判定
- 依存のないPBIは並列に起動する。同じファイルを触るPBIは並列にしない（実行先が違っても同じ）

### 6.1 Codex に渡す（`select_model.py` の `CODEX_UNTIL` まで）

渡す文章は Claude のサブエージェントと同じ（PBI 全文 + 報告形式）。`docs/pbi/codex/PBI-<連番>.prompt.md` に書き、リポジトリのルートで Bash から実行する。時間がかかるので `run_in_background` で起動し、完了の通知を待つ。

```bash
mkdir -p docs/pbi/codex
codex exec -m gpt-6-astra -c model_reasoning_effort='"<route の effort>"' -s workspace-write   -o docs/pbi/codex/PBI-<連番>.last.md - < docs/pbi/codex/PBI-<連番>.prompt.md   > docs/pbi/codex/PBI-<連番>.log 2>&1
```

- 報告は `-o` で書いた `PBI-<連番>.last.md` から読む。ログの末尾の `tokens used` の数を jev-log の完了時の行に書く（12章の統計は Codex を数えない）
- 差し戻しは差し戻し文を同じ形で渡し、ファイル名を `PBI-<連番>.rework-<n>.*` にする。前回の文脈が要るときは `codex exec resume --last` を使ってよい
- effort は route の値（haiku 相当 `medium`、sonnet 相当 `high`）。high より上（xhigh / max）は使わない
- **Codex が使えないとき**（終了コードが 0 以外で、利用上限・認証・接続のエラー、または `-o` のファイルが空）: 1 回だけ再実行し、だめなら `route` の次（Antigravity）へ進む
- Codex は CLAUDE.md を読まず、リポジトリの AGENTS.md を読む。PBI に要る規約は PBI 本文に書く（2章の「PBIだけを読んで作業できるように」と同じ）

### 6.2 Antigravity に渡す（Claude を使う PBI の第一候補）

Antigravity CLI（`agy`、`%LOCALAPPDATA%gyingy.exe`）は Google AI のサブスク特典の Claude を使う。Anthropic のサブスクの枠を残すため、Claude の PBI はまずこちらで動かす。

渡す文章は他と同じ（PBI 全文 + 報告形式）。`docs/pbi/agy/PBI-<連番>.prompt.md` に書き、その末尾に「報告を `docs/pbi/agy/PBI-<連番>.last.md` に書くこと」を足す。リポジトリのルートで Bash から `run_in_background` で起動する。cd はリテラルの絶対パスで行う（変数で cd すると Claude Code の権限確認で止まる）。

```bash
mkdir -p docs/pbi/agy
agy -p "docs/pbi/agy/PBI-<連番>.prompt.md を読み、その指示どおりに作業し、報告を docs/pbi/agy/PBI-<連番>.last.md に書いてください。"   --model <route の model> --mode accept-edits --output-format json --print-timeout 3600s   < /dev/null > docs/pbi/agy/PBI-<連番>.json 2> docs/pbi/agy/PBI-<連番>.err
```

- **拒否は成功扱いで返る**: 許可の無いコマンドは黙って拒否され、終了コード 0・status SUCCESS のまま。必ず `PBI-<連番>.json` の `denied_actions` と `.err` を見る。検証コマンドが拒否されただけなら 8 章で自分で実行すれば足りる。ファイルの編集や作業に要るコマンドが拒否されて作業が進んでいなければ、`route` の次（Agent ツール）へ進む。許可ルール（`~/.gemini/antigravity-cli/settings.json`）はリポジトリ外なので、足すときはユーザーに確認する
- **使えないとき**（終了コードが 0 以外、出力に利用上限・quota・rate limit・`RESOURCE_EXHAUSTED`・認証のエラー、または `.last.md` が無いか空）: 1 回だけ再実行し、だめなら `route` の次（Agent ツール）へ進む。利用上限で落ちたときは STATUS.md の上に「Antigravity: 上限到達（日時）」と書き、このセッションの残りの PBI では agy を飛ばす
- 差し戻しはファイル名を `PBI-<連番>.rework-<n>.*` にし、同じモデルで渡す
- agy の会話には CLAUDE.md も AGENTS.md も効くとは限らない。PBI に要る規約は PBI 本文に書く
- トークン数は 12 章の統計に入らない。完了時の jev-log の行に「agy / モデル名」とだけ書く

## 7. ゲート2: 報告の仕分け

サブエージェントの報告から、検証コマンドの出力・変更箇所・自己判定だけを抜き出して Jev に渡し、ゲート `report` で仕分ける。Jev が使える限り、全PBIで必ず実行する。

| 選択肢 | 次の行動 |
|---|---|
| done_with_evidence | レビュー（8章）へ |
| status_only | 続行を指示する |
| blocked | 不足を補って続行を指示する |
| other | オーケストレーターが報告を読んで自分で仕分ける |

confidence が 0.9 未満のときはオーケストレーターが自分で仕分ける。続行の指示は差し戻しに数えない。続行の指示はPBIごとに2回までとし、3回目が必要なら10章として扱う。

## 8. レビュー

サブエージェントの報告もJevの仕分けも鵜呑みにしない。

1. 検証コマンドを自分で実行する
2. 受け入れ条件を1つずつ pass / fail で判定する
3. diffを読み、マージを止めるべき問題だけを挙げる。それぞれに、ファイル、行、なぜ問題か、再現方法を書く

検証コマンドが PBI の書き方の不備（オーケストレーターの誤り）で fail した場合は、差し戻さずに検証コマンドを直して記録する。

好みや書き方の違いでは差し戻さない。受け入れ条件の fail、触らない範囲の侵害、再現できる不具合のいずれかがあるときだけ差し戻す。

## 9. 差し戻し（最大2回）と ゲート3

### 9.1 問題を要約して修正の規模を聞く（ゲート3a）

指摘の一覧から、次の要約を `docs/pbi/PBI-<連番>.rework-<n>.brief.md` に書いて Jev に送る（ソースコード・diff は送らない）。

```
fail した条件: <件数>（番号）
指摘: 重大 <n> / 中 <n> / 軽微 <n>
影響するファイル: <件数>
指摘の種類: <書き漏れ / 判定の誤り / 範囲外の変更 / 前提の誤解 / 設計の不整合>
1行要約: <何がどう違うか>
```

ゲートは `rework_scope`（`fix_scope` score: 0 指示どおりの修正で済む / 1 実装方針の一部を見直す / 2 設計から見直す）。

| `fix_scope`（confidence 0.7 以上） | 次の行動 |
|---|---|
| 0 | 同じモデルに差し戻す |
| 1 | 段が haiku で、まだ昇格していなければ sonnet の段に上げて差し戻す（昇格はPBIごとに1回まで）。それ以外は同じモデル |
| 2 | 差し戻さずに10章として止める（設計の問題） |

confidence が 0.7 未満なら 0 として扱う。

昇格は `select_model.py <facts> <brief> --tier sonnet` で route を出し直し、**今使っている実行先のまま**その行のモデル・effort に替える（Codex は `medium` → `high`、Antigravity は `claude-sonnet-5-5-low` → `-high`、Agent ツールは haiku `max` → sonnet `high`）。

### 9.2 差し戻し文

```markdown
## 差し戻し <1 | 2> 回目: PBI-<連番>

- 条件 <番号>: fail
  - 現状: <何が起きているか。エラーログや該当行>
  - 期待: <どうなっていれば pass か>
  - 再現: <失敗を確認できるコマンドや手順>
- 範囲外の変更: <あれば、ファイルと戻すべき内容>
```

送る前に、差し戻し文をゲート `rework_quality`（names_condition / states_current_and_expected / has_repro）で点検する。0.6 未満の項目があれば書き直してから送る。送ったら STATUS.md の差し戻し回数（と昇格したら昇格回数）を増やす。

## 10. 止まるとき

2回差し戻しても pass しないPBI、続行の指示が3回目になるPBI、`fix_scope` = 2 のPBIはそこで止める。9.1 の昇格以外に、モデルの昇格、PBIの書き換え、自分での実装をオーケストレーターの判断で行ってはいけない。

そのPBIに依存しない他のPBIは続行する。依存するPBIは保留にする。

ユーザーには次を報告して判断を待つ。

- どのPBIが、どのモデルで止まったか
- 試行ごとに何が fail だったか、`fix_scope` の値
- 原因の見立て（PBIの記述不足 / モデルの能力不足 / 設計の問題 のどれに近いか）
- 選択肢（モデルを1段上げて再実行 / PBIを書き直すか分割する / 設計を見直す / ユーザーが引き取る）

## 11. 完了報告

すべてのPBIが終わったら、全体のdiffを通しで読み、検証コマンドを全体でもう一度実行する。報告は次の3つの見出しで、この順に書く。

- **ユーザー待ち**: 上限到達、保留、品質の警告が残ったPBIと、それぞれに必要な判断
- **変更**: 完了したPBIと変更ファイル
- **発見**: 作業中に見つけた問題、モデル別の結果（一発合格 / 差し戻し1回 / 2回 / 昇格 / 上限到達。実行先（codex / agy / agent）別にも分ける。route の次へ切り替えた件数と理由（Codex 不可 / Antigravity の上限 / 拒否）も書く）、オーケストレーターが決定表を上書きしたPBIとその結果、Jev を飛ばした箇所、12章の統計の要約（モデル別のトークン数、キャッシュの作り直しの回数とトークン数、うち 1h TTL なら防げた分）

## 12. 統計（トークンとキャッシュ）

完了報告の前に、このセッションの統計を取る。途中で状況を見たいときもいつでも実行してよい（読むだけで何も変えない）。

```bash
python <このディレクトリ>/agent_stats.py --out docs/pbi/stats
```

- 引数なしは「今動いているセッション」（最後に更新された会話記録）を集計する。`docs/pbi/stats/<セッションID>.json` に書き、同じセッションは上書きする。要約の Markdown は標準出力に出る
- 集計の元は `~/.claude/projects/<プロジェクト>/<セッション>.jsonl` と `<セッション>/subagents/agent-*.jsonl`。リクエストごとの `usage`（入力・出力・キャッシュ読み・5m / 1h 書き）とモデルを、requestId で重複を除いて足す
- キャッシュの作り直し: 直前までにキャッシュされた量の半分未満しか読めず、新しく書いたリクエスト。間隔が TTL を超えていれば「TTL 切れ」、5m TTL で切れて 1 時間以内なら「1h TTL なら防げた」に数える
- オーケストレーターの行はセッション全体（PBI の前後の会話も含む）
- 過去分の見直しは `--all`（PBI のサブエージェントを含む全セッション）や `--session <ID>` で行う
- `docs/pbi/jev-log.jsonl` の完了時の行に、PBI ごとのモデル・トークン（入力+書き / 出力 / 読み）・作り直しの回数を足す

## Jevの呼び出し方

同梱の Jev クライアント `<このディレクトリ>/jev/jev.py` を使う（使い方はリポジトリの README）。API キーはクライアントが読む: 環境変数 `TYPESAFE_API_KEY`、無ければ `~/.config/typesafe/credentials.json`。オーケストレーターはキーやその資格情報ファイルを読まない・表示しない。スクリプトや設定にキーを書かない。

```bash
python <このディレクトリ>/jev/jev.py docs/pbi/jev.config.json <ゲート名> <送る文章のファイル> > docs/pbi/jev/<PBI>.<ゲート名>.json
```

- 設定は `docs/pbi/jev.config.json`。無ければこのディレクトリの `jev.config.template.json` をコピーして作る。以後はそのファイルを読む
- テストの検証コマンドで `node --test` を使うときは `--test-reporter=tap` を付ける（Node 26 の既定のレポーターは `# pass` / `# fail` の行を出さない）
- API の形: noul の `criteria` は `{"1": "...", "0": "..."}`、score は段の説明の配列、choice は `{"選択肢": "説明"}`。答えは `answers.<キー>` に返る（noul は `noul`、score は `score`・`confidence`・`probabilities`、choice は `choice`・`confidence`）
- 失敗したら共通クライアントが再試行する（429/529 は最大3回、5xx と接続エラーは1回）。だめならそのゲートを飛ばし（3.3 の既定値を使う）、完了報告の「発見」に書く。Jev が使えないことを理由に作業を止めない

運用の決まり。

- **すべて記録する**: 判定ごとに、PBI、ゲート、キー、答え、confidence、採った行動を `docs/pbi/jev-log.jsonl` に1行追記する。モデルを決めたときは `facts` と `select_model.py` の出力も記録し、PBIの結果（一発合格 / 差し戻し回数 / 昇格）を完了時に追記する
- **しきい値と決定表は仮の値**: PBI が 10〜20 件たまったら、モデル別の結果から決定表と警告のしきい値の見直しをユーザーに提案する。キャッシュの TTL も同じ時期に `agent_stats.py --all` の「1h TTL なら防げた」の量と 1h 書き込みの量を比べて見直しを提案する。変えるときは `select_model.py` と `jev.config.json` を直し、変えた理由を記録する
- **送ってよいのは文章だけ**: PBI本文、要約、報告から抜き出した部分、差し戻し文。ソースコード、diff、環境変数、認証情報、顧客データは送らない
- **数えさせない**: 件数・回数・日付の比較は Jev に尋ねず、facts と STATUS.md で扱う
