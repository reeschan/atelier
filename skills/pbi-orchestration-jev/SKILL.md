---
name: pbi-orchestration-jev
description: 複数ファイルにまたがる実装を、PBI起票・モデル振り分け・サブエージェント委任・レビューで進める。Jevで起票内容と報告を点検する。opencode / Codex / Antigravity / Claude Code のサブエージェントに振り分け、オーケストレーターの文脈を細く保ち、大きくなったら引き継ぐ。差し戻しは2回までで、超えたらユーザー判断。
---

# PBIオーケストレーション（Jevゲート付き）

1ファイルで済む小さな修正にはこのスキルを使わず、直接やる。

引数が `resume` なら 6.3 の「再開」から始める。

## 役割

| 担当 | やること | やらないこと |
|---|---|---|
| オーケストレーター（メインセッション、Opus） | 設計、PBI起票、事実の数え上げ、要約の作成、レビュー、差し戻し | 実装、モデルを勘で決めること |
| 実装担当（opencode / Codex / Antigravity / Agent ツール） | PBIの実装と検証コマンドの実行（`worker.md` に従う） | PBIの範囲外の変更 |
| Jev | 短い要約に対する判断（新規性・結合度・あいまいさ・修正の規模）、報告の仕分け、PBIの品質の警告 | 合否判定、数を数えること、diffのレビュー |
| `select_model.py` | 事実と Jev の score から段・実行先の並び（`route`）・レビューの深さを決める | — |
| `agent_stats.py` | 会話記録からエージェント別の読み・ターン数・キャッシュの作り直しを集計し、今の文脈の大きさを出す | 会話記録の書き換え |

合否は検証コマンドの実行結果で決める。回数は `docs/pbi/STATUS.md` で決める。モデルは `select_model.py` の出力で決める。

## 0. 止まる場面

止まってユーザーを待つのは次の4つだけ。

1. PBI一覧の承認（5章）
2. 差し戻し上限への到達、または修正の規模が「設計の見直し」と判定されたとき（10章）
3. 破壊的な操作の前（データ削除、force push、リポジトリ外への変更）
4. 波の区切りで文脈が大きくなったときの引き継ぎ（6.3）

それ以外では報告のために止まらない。状況メモは次の行動と同じメッセージに 1〜2 行で書いて進める。

## 0.5 rtk モード（コマンド出力の圧縮、任意）

[rtk](https://github.com/rtk-ai/rtk) と同梱のフック `rtk/rtk-gate.mjs` を入れているとき（入れ方は README）だけ行う。入れていなければこの節と 11章の rtk の手順は飛ばす。最初の作業（調査・STATUS.md の作成）の前に有効にする。

```bash
node <このディレクトリ>/rtk/rtk-mode.mjs on
```

- 有効なあいだ、このセッションの Bash / PowerShell のコマンド（Agent ツールのサブエージェントを含む）が `rtk-gate.mjs` で `rtk <コマンド>` に書き換わり、出力が圧縮される。Read / Grep / Glob は対象外
- 出力に「rtk: PATH に見つからない」と出たら圧縮は起きない。止まらずに続け、完了報告の「発見」に書く
- 圧縮された出力は完全な結果として扱う。出力が空なのにあるはず、終了コードと食い違う、壊れている、のどれかのときだけ `rtk proxy <コマンド>` で圧縮なしに再実行する
- Codex と opencode は、rtk の全体設定（`rtk init -g --codex`・`rtk init -g --opencode`）を入れていれば常に圧縮される。Antigravity（`agy`）は圧縮されない
- 無効にするのは 11章の完了報告のときだけ。10章・6.3 で止まっているあいだは有効のままにする

## 0.7 文脈を細く保つ

オーケストレーターの文脈に入ったものは、以後のすべてのターンで読み直される。作者の実測（2026-10、2 セッション）では、キャッシュ読みの 55〜72% がオーケストレーターで、1 リクエスト平均 37〜43 万トークンだった。中身は実装担当の長い報告、検証やログの長い出力、大きなファイルの丸読み、PBI 全文の二重の書き込みだった。次の形で受け取り、書く。

- **報告は 4 行で受け取る**: 実装担当の最後のメッセージは `worker.md` の 4 行（結果・変更・未解決・詳細のパス）。詳細ファイルは Jev（7章）にそのまま渡し、自分で読むのは要る見出しだけ（`grep -A` や Read の offset/limit）
- **長くなる出力はファイルに書く**: テスト・ビルド・lint・`git diff`・実行先のログは `docs/pbi/verify/` などにリダイレクトし、表示するのは終了コードと末尾 20 行まで。それ以上が要るときはそのファイルを grep する
- **Read は大きさを見てから**: 中身の分からないファイル・生成物・ログは `wc -lc` で大きさを見て、500 行を超えるなら Grep か offset/limit で必要な部分だけ読む。広い調べ物は Explore サブエージェントに任せ、結論だけを受け取る
- **書くのは 1 回だけ**: PBI 本文は `docs/pbi/PBI-<連番>.md` に 1 回だけ書き、依頼文・プロンプトにはパスを書く（6章）。facts・brief は全 PBI 分を 1 回の Bash（heredoc）でまとめて書く
- **ターンをまとめる**: 独立した操作（複数 PBI の facts・brief の作成、Jev の呼び出し、検証コマンド、実行先の起動の準備）は 1 回の Bash のループか、並列のツール呼び出しにまとめる
- **波の区切りで文脈の大きさを測る**: 6.3

## 1. 状態ファイル

最初に `docs/pbi/STATUS.md` を作り、状態が変わるたびに Edit で該当行だけ更新する。状態と回数は必ずこのファイルから読む。

```markdown
| PBI | モデル | 状態 | 差し戻し | 昇格 | 続行指示 | メモ |
|---|---|---|---|---|---|---|
| PBI-001 | opencode deepseek-v4.1-flash（sonnet 相当） | 実行中 | 1/2 | 0/1 | 0/2 | 条件2がfail |
```

状態は「未着手 / 実行中 / レビュー中 / 完了 / 保留 / 上限到達」のいずれか。作業中に見つけた追加作業は新しいPBIとして行を足す。表の上には、このセッションで外した実行先（`--skip` に渡す値と理由）を書く。

## 2. 分解と起票

既存コードを調べて設計を決め、PBIに分解する。起票の前に `verify-commands.md`（このディレクトリ）を読む。`docs/pbi/PBI-<連番>.md` に1件1ファイルで書く。実装担当は設計時の文脈を持たないので、PBIだけを読んで作業できるように書く。1 PBI は小さく（変更ファイル 3 以下を目安に）切る。

最初に、このディレクトリの `worker.md` を `docs/pbi/worker.md` にコピーする（opencode はリポジトリ外を読めないため）。

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

PBIごとに `docs/pbi/PBI-<連番>.facts.json` を書く（全 PBI 分を 1 回の Bash で）。

```json
{"files_changed": 3, "files_new": 1, "files_deleted": 0, "modules": 1, "criteria": 5, "sensitive": false}
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

ゲートは `brief`（`novelty` / `coupling` / `ambiguity`、各 score 0〜2）。全 PBI 分を 1 回の Bash で呼ぶ（「Jevの呼び出し方」のループ）。

### 3.3 決定表（`select_model.py`）

全 PBI を 1 回で判定する。`docs/pbi/PBI-*.route.json` に結果を書き、標準出力には 1 PBI 1 行の表だけが出る。

```bash
python <このディレクトリ>/select_model.py --all docs/pbi [--skip opencode,agy]
```

| 条件（上から順に評価） | 段 | レビュー |
|---|---|---|
| `novelty` = 2 かつ `ambiguity` = 2（設計判断が要る） | opus | normal |
| `sensitive` が true | sonnet（Agent ツールだけ） | deep |
| `coupling` = 2 | sonnet | deep |
| `novelty`・`coupling`・`ambiguity` のどれかが 1 以上、または `files_changed` ≥ 4、または `modules` ≥ 2 | sonnet | normal |
| 上のどれでもない | haiku | normal |

Opus は設計判断とレビューに使う。opus の段（Agent の opus で実装）は設計判断が要る PBI だけにし、sensitive・coupling=2 は sonnet で実装して Opus のオーケストレーターが重点的に読む（8章の deep）。

段から、試す順の実行先（`route`）が決まる。

| 段 | 1. opencode | 2. Codex（`select_model.py` の `CODEX_UNTIL` まで） | 3. Antigravity（`agy`） | 4. Agent ツール |
|---|---|---|---|---|
| haiku | `opencode-go/deepseek-v4.1-flash` | `gpt-6-astra` effort `medium` | `claude-sonnet-5-5-low` | haiku、effort `max` |
| sonnet | `opencode-go/deepseek-v4.1-flash` | `gpt-6-astra` effort `high` | `claude-sonnet-5-5-high` | sonnet、effort `high` |
| sonnet（sensitive） | （使わない。外のサービスに出さない） | （使わない） | （使わない） | sonnet、effort `high` |
| opus | （使わない） | （使わない） | （使わない。ヘッドレスでコマンドが拒否される） | opus、effort 既定 |

- Jev の score は確率の最も高い段を使う。confidence が 0.7 未満なら 1 段下げて扱う。
- Jev が使えないときは score をすべて 1 として扱う（= sonnet 以上）。
- 期限の判定は実行した日の日付で行う（試すときは `--today YYYY-MM-DD`）。
- 出力（段、`route` の先頭、review）を PBI の「担当モデル」と STATUS.md に写す。オーケストレーターが上書きしたいときは、理由を STATUS.md のメモと jev-log に書く。

## 4. ゲート1b: PBIの品質の警告

PBI全文を Jev に渡し、ゲート `pbi_quality` を聞く（全 PBI 分を 1 回の Bash で）。合否には使わず、警告として承認一覧に出す。書き直しは警告が出たときに1回まで（任意）。

| キー | 警告の条件 |
|---|---|
| targets_named / off_limits_named / criteria_checkable / commands_runnable | 0.6 未満 |
| needs_outside_context | 0.4 より大きい |

## 5. 承認

PBIの一覧をユーザーに見せ、承認を得てから実行に入る。一覧には、ID、タイトル、担当（段・route の先頭・review）とその理由、依存、品質の警告を載せる。

## 6. 実行

依存のない PBI の集まりを「波」として起動し、波の全 PBI のレビューが終わってから次の波に進む。

- PBIごとに `route` の先頭から試す。runner が opencode・codex・agy なら、初めて使う前に `runners.md` の該当の節を読む。agent なら Agent ツール（`model` と `effort` は route の値。effort が null なら付けない）
- 実行先が使えないとき（`runners.md` の条件）は `route` の次へ進む。STATUS.md のモデルを書き換え、メモに理由を書く。PBI の失敗とは数えない
- **1 PBI 1 実装担当で使い捨てる**: 1 つの実装担当に別の PBI を続けて渡さない。差し戻しも新しく起動する（前回の文脈は報告ファイルと差し戻し文で渡す）。同じ実装担当に SendMessage で送るのは、7章の続行の指示だけ
- 差し戻しで起動し直すときは、そのとき使っている実行先・モデル・effort で渡す（昇格したときは 9.1）
- Agent ツールの `description` は必ず `PBI-<連番> <短い説明>` で始める（例: `PBI-003 config frontmatter`、`PBI-003 rework 1`）。統計はこの番号で PBI に結び付ける

### 6.1 依頼文

全実行先で同じ依頼文を使う。PBI の本文は書き写さず、パスで渡す。

```
作業ルート: <リポジトリ（worktree）の絶対パス>。ここをルートとして作業し、外には書き込まない
PBI: docs/pbi/PBI-<連番>.md
差し戻し: docs/pbi/PBI-<連番>.rework-<n>.md（前回の報告: docs/pbi/reports/<前回の報告名>.md）  ← 差し戻しのときだけ
報告名: <PBI-<連番> または PBI-<連番>.rework-<n>>
並列で動いているほかの PBI が触るファイル: <なし / パス>
進め方と報告の形: docs/pbi/worker.md を最初に読み、そのとおりにする
```

Agent ツールにはこの文をそのまま `prompt` に渡す。opencode・Codex・Antigravity には `runners.md` のとおりファイルに書いて渡す。

### 6.2 起動

- 同じファイルを触るPBIは並列にしない（実行先が違っても同じ）。設定ファイル（`eslint.config.mjs`・`tsconfig.json`・`vite.config.ts` など）を触る PBI どうしも並列にしない
- **実行先は 1 件ずつ別のバックグラウンドとして起動する**（Bash の `run_in_background` を PBI ごとに 1 回）。1 つのコマンドの中で `&` や `nohup` でまとめて起動すると、終わったときの通知が来ない。同時に動かすのは 5 件まで（同じ作業ツリーでビルドと開発サーバーが重なるため）。起動は 1 つのメッセージに並列のツール呼び出しとして並べる

### 6.3 波の区切りと引き継ぎ

波の全 PBI のレビュー（8章）と STATUS.md の更新が終わったら、次の波を起動する前に文脈の大きさを測る。

```bash
python <このディレクトリ>/agent_stats.py --ctx
```

`context` が 250,000 以上で、未完了の PBI が残っていれば、引き継ぐ（新しいセッションの起動直後は 7〜9 万。それを超えた分は、以後の全ターンで読み直される）。

1. `docs/pbi/HANDOFF.md` を 40 行以内で書く: 次に起動する波（PBI と route の先頭）、外した実行先と理由、オーケストレーターが上書きした判断、PBI ファイルに書いていない設計上の決定（あれば先に PBI か設計文書に書き足す）、レビューで気付いた後続の PBI への注意
2. jev-log に `{"event": "handoff", "context": <値>, "done": <完了数>, "remaining": <残り>}` を 1 行追記する
3. ユーザーに次の 2 行だけを伝えて止まる（rtk モードは有効のまま）: 「文脈が <値> トークンになったので引き継ぎます。`/clear` のあと `/pbi-orchestration-jev resume` で続きから再開します」

**再開（引数 `resume`）**: `docs/pbi/STATUS.md` と `docs/pbi/HANDOFF.md` を読み、ほかは要るときに要る部分だけ読む。0.5・1〜5 章はやり直さない。読み終えたら 6 章の次の波から続ける。

## 7. ゲート2: 報告の仕分け

実装担当の詳細ファイル `docs/pbi/reports/<報告名>.md` を Jev にそのまま渡し、ゲート `report` で仕分ける。Jev が使える限り、全PBIで必ず実行する。

| 選択肢 | 次の行動 |
|---|---|
| done_with_evidence | レビュー（8章）へ |
| status_only | 続行を指示する |
| blocked | 不足を補って続行を指示する |
| other | 4 行の報告と詳細ファイルの要る見出しを読み、自分で仕分ける |

confidence が 0.9 未満のときは自分で仕分ける。続行の指示は差し戻しに数えない。続行の指示はPBIごとに2回までとし、3回目が必要なら10章として扱う。

## 8. レビュー

実装担当の報告もJevの仕分けも鵜呑みにしない。

1. 検証コマンドを自分で実行する。1 PBI の全コマンドを 1 回の Bash で、番号ごとに出力をファイルへ書き、終了コードと末尾だけを出す
   ```bash
   mkdir -p docs/pbi/verify
   ( <コマンド1> ) > docs/pbi/verify/PBI-<連番>.1.log 2>&1; echo "1: exit=$?"; tail -n 5 docs/pbi/verify/PBI-<連番>.1.log
   ```
2. 受け入れ条件を1つずつ pass / fail で判定する
3. `git diff --stat` で大きさを見てから diff を読み、マージを止めるべき問題だけを挙げる。それぞれに、ファイル、行、なぜ問題か、再現方法を書く
   - review が deep（sensitive・coupling=2）の PBI は、diff を全部自分で読む。認証・権限・入力の検証・データの消え方・モジュール間の契約を重点的に見る
   - review が normal で diff が 300 行を超える PBI は、Agent ツールの opus に読ませる（description `PBI-<連番> review`）。依頼文は「`git diff -- <対象>` を読み、マージを止めるべき問題だけを `docs/pbi/reviews/PBI-<連番>.md` に書く（ファイル・行・なぜ・再現）。最後のメッセージは問題の件数と各 1 行だけ。レビュー系のスキルやサブエージェントは呼ばない」。結果は 4 の判断の材料にし、鵜呑みにしない

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
| 1 | まだ昇格していなければ昇格して差し戻す（昇格はPBIごとに1回まで）。昇格済みなら同じモデル |
| 2 | 差し戻さずに10章として止める（設計の問題） |

confidence が 0.7 未満なら 0 として扱う。

昇格は `select_model.py <facts> <brief> --tier sonnet [--skip ...]` で route を出し直して行う。

- opencode で動いていた PBI（haiku・sonnet の段とも）: `--skip opencode` を足し、sonnet の route の先頭へ移る
- それ以外で haiku の段: **今使っている実行先のまま**sonnet の行のモデル・effort に替える（Codex は `medium` → `high`、Antigravity は `claude-sonnet-5-5-low` → `-high`、Agent ツールは haiku `max` → sonnet `high`）
- それ以外で sonnet の段: 昇格はしない（同じモデル）

### 9.2 差し戻し文

`docs/pbi/PBI-<連番>.rework-<n>.md` に書き、6.1 の依頼文で新しく起動する。

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

すべてのPBIが終わったら、全体の `git diff --stat` を見て、PBI をまたぐ部分（共有の型・設定・呼び出し元）の diff を読み、検証コマンドを全体でもう一度実行する（出力は 8章と同じくファイルへ）。報告は次の3つの見出しで、この順に書く。

- **ユーザー待ち**: 上限到達、保留、品質の警告が残ったPBIと、それぞれに必要な判断
- **変更**: 完了したPBIと変更ファイル
- **発見**: 作業中に見つけた問題、モデル別の結果（一発合格 / 差し戻し1回 / 2回 / 昇格 / 上限到達。実行先（opencode / codex / agy / agent）別、段別にも分ける。sonnet の段の opencode の結果は別に書く）、route の次へ切り替えた件数と理由、オーケストレーターが決定表を上書きしたPBIとその結果、Jev を飛ばした箇所、引き継ぎの回数、12章の統計の要約、rtk の節約量（下記）

rtk モードを使っていたら、報告を書く前にリポジトリのルートで次を実行してから無効にする。`rtk gain -p` の要約（節約したトークン数と率、上位のコマンド）を「発見」に書く。この数はこのリポジトリで rtk を通した全期間の累計（他のセッション・Codex・opencode を含む）なので、そう書き添える。

```bash
rtk gain -p | head -n 20
node <このディレクトリ>/rtk/rtk-mode.mjs off
```

## 12. 統計（トークンとキャッシュ）

完了報告の前に、このセッションの統計を取る（読むだけで何も変えない）。引き継いだときは、引き継ぐ前のセッションも `--session <ID>` で取る。

```bash
python <このディレクトリ>/agent_stats.py --out docs/pbi/stats | head -n 40
```

- 要約の先頭の「エージェント別の読みとターン数」に、オーケストレーターとサブエージェントごとのリクエスト数・キャッシュ読み・割合・1 リクエスト平均・最大の文脈が出る。オーケストレーターの割合と、読みの多いサブエージェントの上位 3 件を「発見」に書く
- キャッシュの作り直し: 直前までにキャッシュされた量の半分未満しか読めず、新しく書いたリクエスト。間隔が TTL を超えていれば「TTL 切れ」、5m TTL で切れて 1 時間以内なら「1h TTL なら防げた」に数える
- 過去分の見直しは `--all`（PBI のサブエージェントを含む全セッション）や `--session <ID>` で行う
- `docs/pbi/jev-log.jsonl` の完了時の行に、PBI ごとのモデル・トークン（入力+書き / 出力 / 読み）・作り直しの回数を足す

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
