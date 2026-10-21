---
name: pbi-orchestration-jev
description: 複数ファイルにまたがる実装を、PBI起票・モデル振り分け・サブエージェント委任・レビューで進める。Jevで起票内容と報告を点検する。設計者（オーケストレーター）は全体を見てPBIを作り、実装担当（pbi-worker）はPBIと参照の ref だけで作る。機械的な手順は prepare.py・verify.py に任せて設計者の文脈を細く保ち、opencode / Codex / Antigravity / Claude Code のサブエージェントに振り分ける。差し戻しは2回までで、超えたらユーザー判断。
---

# PBIオーケストレーション（Jevゲート付き）

1ファイルで済む小さな修正にはこのスキルを使わず、直接やる。この本文は毎リクエストで読み直されるので、場面ごとの詳細は `appendix/` に分けてある。その場面になったら読む。

| 場面 | 読むもの |
|---|---|
| 引数が `resume`（`resume <リポジトリのパス>` を含む） / 6.3 で文脈が 150,000 以上 | `appendix/handoff.md` |
| 引数が `templates`（振り分けテンプレートを画面で決める） | `appendix/templates.md` |
| 作業キーを決める・ref を書く・引き継ぐ・終わる | `appendix/memory.md` |
| 差し戻す・止まる条件に当たった | `appendix/rework.md` |
| Jev を直接呼ぶ（報告の仕分け・差し戻しのゲート） | `appendix/jev.md` |
| すべての PBI が終わった | `appendix/finish.md` |
| 優先テンプレート・適用を扱う (開始時の同期、チャットでの適用・作成、期限切れの確認、Web ページ) | `appendix/priority-templates.md` |
| 実行先が opencode・codex・agy・dsh | `runners.md` |
| PBI を起票する前 | `verify-commands.md` |

## 役割

| 担当 | やること | やらないこと |
|---|---|---|
| 設計者（メインセッション。オーケストレーター） | 設計、全体のメモリの search、ref の記録、PBI起票、facts・brief の作成、レビュー、差し戻し、完了時の昇格 | 実装、モデルを勘で決めること |
| Opus のサブエージェント（設計者が Opus でないときだけ） | 設計判断の見直しと、deep・大きな diff のレビュー（下の「設計者のモデル」） | 実装 |
| 実装担当（Agent ツールの `pbi-worker` / opencode / Codex / Antigravity / dsh） | PBIと参照欄の ref だけを材料に、PBIの実装と検証（`worker.md`） | PBIの範囲外の変更、メモリの search・write |
| `prepare.py` | Jev のゲート 1（brief・pbi_quality）、jev-log の記録、`select_model.py` の判定を 1 回で行い、表だけを出す | — |
| `verify.py` | PBI の検証コマンドを実行し、番号ごとの終了コードと末尾だけを表で出す（終了コードは `docs/pbi/verify/<報告名>.json` にも書く） | 合否の判定 |
| `record_run.py` | 1 回の実行（報告名 × route の何番目）を `docs/pbi/runs.jsonl` に 1 行で記録し（トークン・コスト・時間・検証はログから読む）、生ログを保全先にコピーする | 合否の判定・評価 |
| `model_report.py` | runs.jsonl をモデル別に集計する（保全先の全リポジトリも可）。仮の評価 A〜D も出す | 振り分けを変えること |

合否は検証コマンドの実行結果で決める。回数は `docs/pbi/STATUS.md` で決める。モデルは `select_model.py` の出力で決める。

### 設計者のモデル

設計者は起動時のモデル（`--model`）で動く。Opus でないとき（例: Sonnet の effort max）は、次の 2 つを Opus のサブエージェント（`subagent_type: "pbi-worker"`、`model: "opus"`）に任せる。それ以外は設計者が自分で行う。
- **設計判断の見直し**: `prepare.py` の表で段が opus になった PBI は、実装に回す前に見直させる。description は `PBI-<連番>.design`。依頼文は「PBI ファイル（と参照欄の ref）を読み、設計上の問題・抜け・あいまいさだけを `docs/pbi/reviews/PBI-<連番>.design.md` に書く。最後のメッセージは件数と各 1 行だけ」。結果を PBI に反映してから実装に回す
- **レビュー**: review が deep の PBI と、diff が 300 行を超える PBI（8章の 3）

## 0. 止まる場面

止まってユーザーを待つのは次の4つだけ。
1. PBI一覧の承認（5章）
2. 差し戻し上限への到達、または修正の規模が「設計の見直し」と判定されたとき（`appendix/rework.md`）
3. 破壊的な操作の前（データ削除、force push、リポジトリ外への変更。ただし shared-memory への ref・引き継ぎ・昇格の書き込みは除く）
4. 波の区切りで文脈が大きくなったときの引き継ぎ（`appendix/handoff.md`）

それ以外では報告のために止まらない。状況メモは次の行動と同じメッセージに 1〜2 行で書いて進める。

## 0.7 文脈を細く保つ

設計者の文脈に入ったものは、以後のすべてのリクエストで読み直される。作者の実測 (2026-10) では、費用の約 85% が設計者で、文脈は起動直後の約 4.7 万から 10〜13 万に育ち、1 件あたり 20〜29 回読み直されていた。
- **報告は数行で受け取る**: どのサブエージェントにも、詳細はファイルに書かせ、最後のメッセージは数行とパスだけにさせる（実装担当は `worker.md` の 5 行、レビューは件数と各 1 行、調べ物は「結論を `docs/pbi/notes/<名前>.md` に書き、5 行以内の要約とパスだけを返す」）。詳細ファイルは Jev にそのまま渡し、自分で読むのは要る見出しだけ
- **機械的な手順はスクリプトで**: Jev のゲート 1 と担当の判定は `prepare.py`、検証は `verify.py`。どちらも表だけを受け取る
- **長くなる出力はファイルに書く**: そのほかのテスト・ビルド・`git diff` も `docs/pbi/verify/` にリダイレクトし、表示は終了コードと末尾 20 行まで
- **Read は大きさを見てから**: 中身の分からないファイルは `wc -lc` で大きさを見て、500 行を超えるなら Grep か offset/limit で読む
- **書くのは 1 回だけ**: PBI 本文は 1 回だけ書き、依頼文にはパスを書く。facts・brief は全 PBI 分を並列の Write でまとめて書く
- **複数行は Write ツールで書く**: Bash の heredoc で本文を書かない。Windows の Bash ツールは `$( )` の中の `<` と `$'…'` が同じコマンドにあると壊れる（`verify-commands.md`）
- **ターンをまとめる**: 独立した操作は並列のツール呼び出しか 1 回の Bash にまとめる

## 0.8 作業メモリ（要点。詳細は `appendix/memory.md`。任意）

shared-memory（`search`・`get`・`write` を持つ共有メモリの MCP。ツール名は `mcp__shared-memory__*`）があるときだけ行う。


- 設計者はメモリを幅広く見て PBI を作る。実装担当は PBI と参照欄の ref だけを見る（`pbi-worker` は memory の `get` だけを持つ）
- リポジトリの安定した知識は階層化した CLAUDE.md にあるものとして扱う。CLAUDE.md の事実は ref にも補足にも写さない
- 設計者の search は、CLAUDE.md に無いものを探す補助として `repo:<repo>` と `global` を scope 付きで 1 回ずつまで。scope なしの search はしない（例外は `resume` で作業リポジトリが分からないときの tags `handoff` の 1 回だけ。`appendix/handoff.md`）
- ref は 2 つ以上の PBI・引き継ぎ・レビューの注意で使う事実だけを、作業空間（`project:work.<repo>.<branch>.<work-num>`）に、並列で 1 ターンにまとめて書く。1 つの PBI だけの事実は PBI の補足へ。write の直後に get しない
- 作業キーの決め方、ref の書式、引き継ぎ、完了時の昇格（最大 3 件）は `appendix/memory.md`
- shared-memory が無い、または write が禁止されているセッションでは、ref と昇格を飛ばして補足に書き、完了報告の「発見」に書く

## 1. 状態ファイル

最初に `docs/pbi/STATUS.md` を作り、状態が変わるたびに Edit で該当行だけ更新する。状態と回数は必ずこのファイルから読む。

```markdown
スキルの版: <版 (v21 など) と公開版のコミット>
作業キー: <repo>:<branch>:W-001 / 作業空間: project:work.<repo>.<branch>.W-001
外した実行先: <--skip の値と理由 / なし>

| PBI | モデル | 状態 | 差し戻し | 昇格 | 続行指示 | メモ |
|---|---|---|---|---|---|---|
| PBI-001 | agent sonnet high（sonnet / normal） | 実行中 | 1/2 | 0/1 | 0/2 | 条件2がfail |
```

「スキルの版」の行は、版をまたいで resume したことに気づけるように書く。状態は「未着手 / 実行中 / レビュー中 / 完了 / 保留 / 上限到達」のいずれか。作業中に見つけた追加作業は新しいPBIとして行を足す。

## 2. 分解と起票

- 開始時に Web と同期する (`appendix/priority-templates.md` の「開始時の同期」)

既存コードと 0.8 の範囲のメモリを調べて設計を決め、PBIに分解する。起票の前に `verify-commands.md` を読む。最初に、このディレクトリの `worker.md` を `docs/pbi/worker.md` に、`unchanged.py`（触らない範囲の検証。`verify-commands.md`）を `docs/pbi/tools/unchanged.py` にコピーする（opencode はリポジトリ外を読めないため）。1 PBI は小さく（変更ファイル 3 以下を目安に）切り、`docs/pbi/PBI-<連番>.md` に1件1ファイルで書く。実装担当は設計時の文脈を持たないので、PBIと参照欄の ref だけで作業できるように書く。

```markdown
# PBI-<連番>: <タイトル>

- 担当モデル: <prepare.py の表から>
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
1. `<リポジトリのルートからそのまま実行できる 1 行のコマンド>`
2. `<…>`

## 参照
<作業空間の ref の id と 1 行の要旨。無ければ「なし」>

## 補足
<従うべき既存の実装パターン、参照するファイル、落とし穴、この PBI だけに要る事実>
```

- 検証コマンドは 1 行に 1 つ、`番号. \`コマンド\`` の形で書く（`verify.py` がこの形を読む）。説明や期待値は受け入れ条件に書く
- 「参照」には、その PBI に要る ref だけを載せる。載せない ref を実装担当が探しに行くことはない

## 3. 担当を決める（facts・brief → `prepare.py`）

PBI ごとに、次の 2 つを並列の Write でまとめて書く。

`docs/pbi/PBI-<連番>.facts.json`:
```json
{"files_changed": 3, "files_new": 1, "files_deleted": 0, "modules": 1, "criteria": 5, "sensitive": false}
```
- `files_changed`: 変更・新規・削除を合わせたファイル数（テストを含む）。`modules`: 変更するファイルが属する機能単位の数。`sensitive`: 認証・権限・DB スキーマ・ユーザーデータの削除・秘密情報のどれかに触れるか（リポジトリ内のファイル削除は含めない）

`docs/pbi/PBI-<連番>.brief.md`（8 行以内。PBI 全文は Jev に送らない）:
```
目的: <1文>
変更の種類: <既存パターンの置き換え / 既存パターンの追加 / 新しい仕組み>
新しい抽象: <なし / 型・関数の追加 / モジュール間の新しい契約>
外部とのやりとり: <なし / HTTP n 本 / ストレージ / …>
守るべき不変条件: <なし / 具体的に>
判断が要る点: <なし / 具体的に>
```

書き終えたら、1 回だけ実行する。

```bash
python <このディレクトリ>/prepare.py docs/pbi [--skip opencode,codex,agy,dsh] [--only PBI-<連番> --tier <段>]
```

出力の表（段・route の先頭・review・補正後の値・理由・優先テンプレート・外した手順・品質の警告）を、PBI の「担当モデル」と STATUS.md に写す。PBI を書き直したら `--only PBI-<連番>` を付けて、その PBI だけ判定し直す。段を上げるときは `--only PBI-<連番> --tier <段>` で route.json を書き直す（8章）。
- 段の決め方は `select_model.py` の docstring にあり、表の「理由」の列に出る。Jev が無いときは facts だけで決める
- 適用中の優先テンプレートは決定表のあとに、振り分けテンプレート (routing.json) より先に当たる。sensitive には当たらず、最後は routing.json のその段の agent。prepare.py の先頭の行と列「優先テンプレート」を承認一覧に写す
- 品質の警告は合否に使わず、承認一覧に出す。書き直しは警告が出たときに 1 回まで（任意）
- 設計者が決定表を上書きするときは、理由を STATUS.md のメモと jev-log に書く

## 5. 承認

PBIの一覧をユーザーに見せ、承認を得てから実行に入る。一覧には、ID、タイトル、担当（段・route の先頭・review）とその理由、外した手順、依存、品質の警告を載せる。
- 承認一覧に優先テンプレートの行を載せる。優先テンプレートが opus の段を上書きしているときは、設計判断が要る PBI まで安いモデルに回しているおそれがある、と注意を添える。期限切れの ask があれば、承認と一緒に「延長 / やめる / 期限なし」を聞き (`appendix/priority-templates.md` の「期限切れの確認」)、答えを反映してから prepare.py を `--only` なしでもう一度実行する。同期できなかったときは「Web と同期できず、<synced_at> 時点の写しを使用」と書く

## 6. 実行

依存のない PBI の集まりを「波」として起動し、波の全 PBI のレビューが終わってから次の波に進む。
- PBIごとに `route` の先頭から試す。agent なら Agent ツールを `subagent_type: "pbi-worker"` で起動する（`model` と `effort` は route の値。effort が null なら付けない。pbi-worker が無ければ general-purpose で起動し、STATUS.md に書く）。opencode・codex・agy・dsh なら `runners.md` を読む
- 実行先が使えないとき（`runners.md` の条件）は、次へ進む前に `record_run.py <報告名> --attempt <route の何番目> --outcome 使えず` で記録し（生ログが次の実行で上書きされる前に保全するため）、`route` の次へ進み、STATUS.md のモデルとメモを書き換える。PBI の失敗とは数えない
- **1 PBI 1 実装担当で使い捨てる**。差し戻しも新しく起動する。同じ実装担当に SendMessage で送るのは、7章の続行の指示だけ
- 実装担当の Agent ツールの `description` は必ず報告名と空白で始める（`PBI-007 <短い説明>`、差し戻しは `PBI-007.rework-1 <短い説明>`）。record_run.py と agent_stats.py は、この文字列で会話記録を実行に結び付ける。設計者のレビュー・設計見直しは `PBI-<連番>.review` / `PBI-<連番>.design` にして、実装と分ける
- 同じファイル・設定ファイルを触る PBI は並列にしない。同時に動かすのは 5 件まで。実行先は 1 件ずつ別のバックグラウンドとして起動する（1 つのコマンドの中で `&` でまとめない）

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

### 6.3 波の区切り

波の全 PBI のレビューと STATUS.md の更新が終わったら、`python <このディレクトリ>/agent_stats.py --ctx` で文脈の大きさを測る。150,000 以上なら `appendix/handoff.md` を読み、そのとおりにする（rtk モードの有効化、250,000 以上なら引き継ぎ）。

## 7. 報告の仕分け（ゲート2）

実装担当の報告の先頭の「状態」の 1 行と、検証コマンドの終了コード（`verify.py` の表）で仕分ける。Jev のゲート `report` に聞くのは、2 つが食い違うときだけ（DONE なのに検証が fail、BLOCKED なのに検証が全部 pass など。呼び方は `appendix/jev.md`）。
- DONE → 8章へ
- DONE_WITH_CONCERNS → 8章へ進み、詳細ファイルの懸念を読む
- NEEDS_CONTEXT → 不足を補って続行を指示する
- BLOCKED → 原因を読み、続行か差し戻しか止まるかを決める（`appendix/rework.md`）
- 続行の指示は差し戻しに数えない。PBI ごとに 2 回まで。3 回目が要るなら止まる（`appendix/rework.md`）

## 8. レビュー

実装担当の報告もJevの仕分けも鵜呑みにしない。
1. 検証コマンドを `verify.py` で実行し、表だけを見る。出力が要る番号だけ、表のログを Grep・tail で読む
   ```bash
   python <このディレクトリ>/verify.py PBI-<連番> --name <報告名> [--only 2,4]
   ```
   `verify.py` がコマンドを読めなかったとき（終了コード 2）は、1 回の Bash で番号ごとにログへリダイレクトして実行する
2. 受け入れ条件を1つずつ pass / fail で判定する
3. `git diff --stat` で大きさを見てから diff を読み、マージを止めるべき問題だけを挙げる（ファイル、行、なぜ、再現方法）
   - review が deep の PBI は diff を全部読む（認証・権限・入力の検証・データの消え方・モジュール間の契約を重点に）
   - diff が 300 行を超える PBI と、設計者が Opus でないときの deep の PBI は、Opus の `pbi-worker` に読ませる（description `PBI-<連番>.review`）。依頼文は「PBI ファイルと参照欄の ref（`get`）を前提に、`git diff -- <対象>` を読み、マージを止めるべき問題だけを `docs/pbi/reviews/PBI-<連番>.md` に書く（ファイル・行・なぜ・再現）。最後のメッセージは問題の件数と各 1 行だけ。レビュー系のスキルやサブエージェントは呼ばない」。結果は判断の材料にし、鵜呑みにしない

合否（合格 / 差し戻し / 昇格 / 保留）を決めたら、すぐに 1 回記録する。判断で渡すのは結果とレビュー指摘の件数（重大,中,軽微）だけで、ほかはログから読まれる。段を上げるときは `prepare.py --only PBI-<連番> --tier <段>` で route.json を書き直す。record_run.py は書き直された route.json を読むので、渡すのは結果と指摘の件数だけ。標準出力の 1 行に「保全に失敗」と出ても作業は続け、完了報告の「発見」に書く。
```bash
python <このディレクトリ>/record_run.py <報告名> --attempt <route の何番目> --outcome 合格 --findings 0,1,2
```

検証コマンドが PBI の書き方の不備（設計者の誤り）で fail したら、差し戻さずに検証コマンドを直して記録する。好みや書き方の違いでは差し戻さない。受け入れ条件の fail、触らない範囲の侵害、再現できる不具合のいずれかがあるときだけ差し戻す（`appendix/rework.md`）。

すべての PBI が終わったら `appendix/finish.md` を読み、全体の確認・昇格・統計・完了報告を行う。
