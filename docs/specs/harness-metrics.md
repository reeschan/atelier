# ハーネスのための指標とログの送り先 (harness-log)

2026-11-11 にユーザーと design-flow で詰めた。atelier を改善するループ (ハーネス) に使う指標を見直し、集めたログを private のリポジトリ (herness-log) に送る。今ある記録の棚卸しは [harness-metrics-inventory.md](../notes/harness-metrics-inventory.md) にある。用語は `skills/atelier/CONTEXT.md` に従う。

## Problem Statement

- atelier を改善するには、どこに費用がかかり、どこで差し戻され、どこでユーザーを待たせ、どこで後ろ盾に落ちたかを、run をまたいで比べる必要がある。今の記録は 10 か所以上に散らばり、結び付けるキーがそろっていない (conductor の run_id、kiln の run_id、work_key、design_id、session_id)。
- 工程ごとの時刻、問いを出した時刻と種類、出番ごとの費用、fallback の数が残らない。#40・#41 で増えた問い (使えない runner・OMM・recheck・不明点・Approve の ask) の答えは、多くが自由文でしか残らない。
- 記録には本文・自由文・絶対パスが混ざっているので、そのままでは外に集められない。
- 数字だけを集めると、タスクの難しさやジャンルの偏りが分からない。易しい run が続いて合格率が上がったのか、atelier が良くなったのかを区別できない。
- 集める先が決まっていない。atelier 以外のスキル (design-flow・review-flow など) も、いずれ同じ先に集めたい。

## Solution

- 改善の判断に使う指標を 5 つの軸 (費用・品質・人の手間・頑健さ・所要時間) に絞る。それを **イベント** (1 行 1 件の JSON) として、atelier の各スクリプトから出す。
- イベントは、どのスキルでも使える共通の **送り手** `harness-log` を通す。送り手は、スキルが渡した **許可リスト** (スキーマのファイル) にある項目だけを通す。そのうえで手元の **スプール** にため、出番の終わりと run の終わりに herness-log へ commit と push をする。
- タスクの形は、**タスクの特徴** (測った値と決まった語彙のタグ) と **アンカーとの近さ** (埋め込みで計算する) で表す。どちらも本文を外に出さずに済む。
- 送るのは、ユーザーが中身を見て `enable` してからにする。設定が無ければ何も送らない (atelier は公開リポジトリなので、ほかの人が使っても何も送られない)。

```mermaid
flowchart LR
  subgraph atelier[atelier のスクリプト]
    C[conductor.py] --> E[events.py]
    R[record_run.py] --> E
    K[kiln_log.py] --> E
    RR[role_results.py] --> E
    DR[design_record.py] --> E
    D[設計者のコマンド<br/>events.py stage / profile / answer] --> E
  end
  E -- "許可リストで点検" --> L[(docs/pbi/events.jsonl<br/>手元の記録)]
  E -- "emit" --> H[harness-log]
  H --> S[(スプール<br/>~/.claude/harness-log/spool)]
  S -- "flush: 出番の終わり・run の終わり<br/>(裏で走る・失敗しても止めない)" --> G[(herness-log<br/>phase/skill/YYYY-MM/run_id.jsonl)]
  E -- "埋め込み" --> O[SSH 先の Ollama<br/>qwen3-embedding:0.6b]
  E -- "ベクトル" --> V[(保全先 vectors.jsonl<br/>手元だけ)]
```

## User Stories

1. As a atelier の持ち主, I want 1 回の run のイベントが 1 つのファイルにまとまっていてほしい, so that run を頭から追える
2. As a atelier の持ち主, I want すべてのイベントに run_id・seq・stage・pbi が付いていてほしい, so that 散らばった記録を同じキーで結べる
3. As a atelier の持ち主, I want run・工程・役割・PBI・モデルごとのトークンと USD を知りたい, so that どの役割に安いモデルを当てるかを決められる
4. As a atelier の持ち主, I want 出番ごとの費用を、設計者・レビュー担当・テスト担当それぞれについて知りたい, so that 今は run 全体の合計でしか見えない役割の費用を比べられる
5. As a atelier の持ち主, I want 費用を測れなかった出番を「測れなかった」と区別したい, so that 0 円と測れなかったことを取り違えない
6. As a atelier の持ち主, I want PBI ごとの初回合格率を知りたい, so that モデルや起票の書き方の良し悪しを比べられる
7. As a atelier の持ち主, I want 差し戻しの原因を決まった分類 (spec・convention・scope・memory・model・env) で数えたい, so that 何を直せば差し戻しが減るかが分かる
8. As a atelier の持ち主, I want 原因が付いていない差し戻しの数も知りたい, so that 記録の抜けそのものを減らせる
9. As a atelier の持ち主, I want レビューの findings を重さごとの数で知りたい, so that レビュー担当がどのくらい見つけているかが分かる
10. As a atelier の持ち主, I want テストの失敗を「意図した変更」「壊した動き」「不明」の数で知りたい, so that テスト担当の仕分けの質が分かる
11. As a atelier の持ち主, I want ユーザーに出した問いの数を種類ごとに知りたい, so that 減らせる問いを見つけられる
12. As a atelier の持ち主, I want 問いを出してから答えが来るまでの待ち時間を知りたい, so that ユーザーを待たせている場所が分かる
13. As a atelier の持ち主, I want 使えない runner・OMM・recheck・不明点・Approve の ask・設計の承認を、種類として区別したい, so that #40・#41 で足した問いの効き目を測れる
14. As a atelier の持ち主, I want 問いに対してどれが選ばれたかを列挙値で残したい, so that 既定値を変える判断ができる (いつも cache が選ばれるなら既定を cache にする)
15. As a atelier の持ち主, I want 工程が run・skip・fallback・off のどれになったかと、それを決めたのが誰か (ユーザー・既定・自動・後ろ盾) を残したい, so that 後ろ盾に落ちた率が出せる
16. As a atelier の持ち主, I want 設計者の定義が無くて general-purpose で代わりに起こしたことを残したい, so that 入れ忘れの影響が見える
17. As a atelier の持ち主, I want 役割の値とその出所 (run・repo・home・default) を出番ごとに残したい, so that どの設定がどの結果に効いたかを比べられる
18. As a atelier の持ち主, I want 出番と工程の実時間を知りたい, so that 遅い工程が分かる
19. As a atelier の持ち主, I want 文脈の圧縮 (kiln) が起きた境界と圧縮前のトークンを、同じ run_id で残したい, so that 圧縮と失敗の関係を見られる
20. As a atelier の持ち主, I want タスクの大きさ・広がり・文脈の重さを段階に丸めて残したい, so that 難しさの違うタスクを分けて比べられる
21. As a atelier の持ち主, I want タスクの種類と領域を決まった語彙で残したい, so that ジャンルごとに比べられる
22. As a atelier の持ち主, I want 設計者の難しさの見立てと、実際の差し戻し数を並べたい, so that 見立てが当たっているかを測れる
23. As a atelier の持ち主, I want ジャンル × 大きさごとの件数の表 (coverage) を見たい, so that 試していない区画を意図して試せる
24. As a atelier の持ち主, I want 各 PBI が代表のタスク (アンカー) のどれに近いかを知りたい, so that 語彙のタグより細かく似たタスクを集められる
25. As a atelier の持ち主, I want 埋め込みのベクトルを手元にだけ置きたい, so that 本文に近い情報を外に出さずに済む
26. As a atelier の持ち主, I want 埋め込めなかった PBI を後から埋め込み直したい, so that SSH 先が寝ていた日の分も欠けずに済む
27. As a atelier の持ち主, I want 本文が要るときは、手元の cases.jsonl を case_id で引きたい, so that 本文を外に出さずに失敗の中身を読める
28. As a atelier の持ち主, I want 送る中身が許可リストで決まっていてほしい, so that 自由文・本文・パス・秘密が混ざらない
29. As a atelier の持ち主, I want 許可リストにない項目が落とされたことを、項目名だけで知りたい, so that 許可リストの直し忘れに気付ける
30. As a atelier の持ち主, I want 自分のリポジトリ (`reeschan/*`) の名前はそのまま、ほかのリポジトリとブランチは鍵付きのハッシュで残したい, so that 仕事のリポジトリの名前を出さずに、同じリポジトリの run を結べる
31. As a atelier の持ち主, I want 送る前に、スプールにたまった中身をそのまま見たい, so that 何が送られるかを確かめてから送り始められる
32. As a atelier の持ち主, I want `enable` するまでは送られないようにしたい, so that 承認の前に外へ出ない
33. As a atelier の持ち主, I want いつでも `disable` で送るのを止めたい, so that 止めている間もスプールにたまり、再開したときにまとめて送れる
34. As a atelier の持ち主, I want 送れなかったときに run が止まらないようにしたい, so that ネットワークや GitHub の不調で開発が止まらない
35. As a atelier の持ち主, I want 送れなかった分は次の flush で送り直されてほしい, so that 欠けが出ない
36. As a atelier の持ち主, I want 出番が終わるたびに送られてほしい, so that 長い run の途中でも様子が見える
37. As a atelier の持ち主, I want 1 回の flush が 1 つのコミットになってほしい, so that コミットの数が出番の数にとどまる
38. As a atelier の持ち主, I want ログが `<開発段階>/<スキル>/<年月>/<run_id>.jsonl` に並んでほしい, so that 段階ごと・スキルごとに集められる
39. As a 別のスキル (design-flow・review-flow など) の作者, I want 同じ送り手に、自分の許可リストと段階を渡して送りたい, so that 送る仕組みを作り直さずに済む
40. As a atelier をほかの人が使うとき, I want 設定が無ければ何も送られず、何も止まらないでほしい, so that 公開のスキルが勝手に外へ送らない
41. As a ハーネスの作者, I want 各行に外枠の版 (`harness.event/1`) と中身の版 (`atelier.event/1`) が付いていてほしい, so that 版が変わっても読み分けられる
42. As a ハーネスの作者, I want herness-log の直下に、項目の一覧 (SCHEMA.md) がほしい, so that ログだけを見て意味が分かる
43. As a atelier の持ち主, I want 手元の `docs/pbi/events.jsonl` にも同じイベントが残ってほしい, so that 送るのを止めていても、手元で集計できる

## Implementation Decisions

### 指標の軸と、使う判断

| 軸 | 指標 | 使う判断 | 出所のイベント |
|---|---|---|---|
| 費用 | run・工程・役割・PBI・モデルごとのトークン 4 種と USD。測れなかった出番の数 | 役割ごとのモデルの当て方、安い runner の採否 | `episode.cost`・`pbi.attempted` |
| 品質 | 初回合格率、差し戻しの回数と原因の分類、原因なしの差し戻しの数、findings の重さごとの数、テストの失敗の分類 | 起票の書き方、モデルの段、レビューの観点 | `pbi.attempted`・`review.judged`・`test.judged` |
| 人の手間 | 問いの数を種類別に、待ち時間、選ばれた答え | 問いを減らす、既定値を変える | `question.asked`・`question.answered`・`stage.decided` |
| 頑健さ | fallback・skip・使えない runner・出番のやり直し・設計者の代替起動の率 | 後ろ盾の要否、runner の入れ替え | `stage.decided`・`pbi.attempted`・`episode.ended` |
| 所要時間 | 出番と工程の実時間、PBI の attempt ごとの秒 | 遅い工程の見直し | `episode.started`/`ended`・`pbi.attempted` |

難しさとジャンルの補いには `task.profiled` を使う。どの指標も、`task.profiled` の段階とタグで層に分けて比べる。

### イベントの外枠 (harness.event/1)

送り手が付ける。スキルは `data` と、外枠のうち `run_id`・`seq`・`stage`・`pbi`・`type` を渡す。

| 項目 | 意味 |
|---|---|
| `schema` | `harness.event/1` |
| `ts` | UTC の ISO 8601 (秒まで) |
| `phase` | 開発段階。`design`・`implement`・`review`・`test`・`release`・`ops`・`other` のどれか |
| `skill`・`skill_version` | 送ったスキルとその版 (atelier は `atelier`、版は CHANGELOG の先頭の版) |
| `data_schema` | `data` の版 (atelier は `atelier.event/1`) |
| `repo` | 許可リスト (`show_repos`、glob) に合えば `owner/name`、合わなければ `h:<12 桁>`。git の remote が無ければ `none` |
| `branch` | 常に `h:<12 桁>` |
| `host` | 端末の名前の `h:<12 桁>` |
| `run_id`・`seq`・`stage`・`pbi` | 結び付けキー。無ければ null |
| `type` | イベントの種類 |
| `data` | 許可リストで点検した中身 |
| `dropped` | 許可リストに無くて落とした項目の名前 (値は入れない)。無ければ省く |

- ハッシュは HMAC-SHA256 で、鍵は `init` が作る乱数。先頭 12 桁を使う。鍵の無い sha256 では、名前の候補を試せば元に戻せてしまうため。
- `run_id` が無いイベント (conductor を通さずに動かしたとき) は、`adhoc-<UTC の日付>` のファイルにまとめる。

### atelier のイベント (atelier.event/1)

許可リストは `skills/atelier/harness.schema.json` に置き、送り手と `events.py` が同じファイルを読む。値の型は、整数・数・真偽・列挙・短い識別子 (英数字と `._:/@-`、64 字まで) と、それらの入れ子だけにする。自由文の型は作らない。

| type | 出す人・いつ | data の項目 |
|---|---|---|
| `run.started` | conductor の start | `workflow`・`launch`・`roles_from` (役割ごとの出所) |
| `run.ended` | conductor が finished・aborted にしたとき | `result` (finished/aborted)・`episodes`・`waves`・`pbis`・`seconds` |
| `episode.started` | conductor の next・run (起動したとき) | `episode`・`wave`・`launcher` (agent/headless)・`designer` (installed/fallback)・`roles` (役割ごとに model・effort・from) |
| `episode.ended` | conductor の done (結果を受け取ったとき) | `episode`・`status`・`reason` (列挙)・`seconds`・`failures`・`next_episode` |
| `episode.cost` | 出番の後片付け (裏で走る) | `episode`・`method` (transcript/headless/unmeasured)・役割ごとの `requests`・`input`・`output`・`cache_read`・`cache_write`・`usd` |
| `stage.decided` | 設計者のコマンド・lint_run approve | `component`・`decision` (run/skip/fallback/off/repo/cache/report-only/fix/next-runner/stop)・`by` (user/default/auto/fallback)・`reason` (列挙) |
| `pbi.attempted` | record_run | `attempt`・`step`・`rework`・`runner`・`model`・`tier`・`effort`・`outcome` (pass/sent-back/promoted/unusable/held)・`causes`・`findings` (重さごとの数)・`unchanged`・`seconds`・トークン 4 種・`usd`・`case_id` |
| `review.judged` | role_results check (review の結果) | `report`・`recommendation`・`findings` (重さごとの数)・`criteria` (pass/fail/unknown の数) |
| `test.judged` | role_results check (test-result) | `kind` (wave/survey/final)・`passed`・`failed`・`failing` (intended/regression/unknown の数) |
| `question.asked` | conductor (出番の結果か進行役の問いを受けたとき) | `kind` (approve/rework-limit/destructive/send-approval/decision/handoff-loop/launch-failure)・`topic` (runner-unavailable/omm/recheck/unknown/approve-ask/design-approval/headless/other)・`items` |
| `question.answered` | conductor の answer・設計者のコマンド (inline の問い) | `kind`・`topic`・`choice` (列挙。分からなければ null)・`wait_s`・`revise` |
| `task.profiled` | 設計者のコマンド (plan の確定時は PBI ごと、finish のときは run 全体)。PBI の大きさは finish の backfill が測り直す | 測った値・タグ・近さ (次の節) |
| `context.compacted` | kiln_log | `boundary`・`role`・`phase`・`regime`・`tokens_before`・`action` |

- 問いの `topic` は、出番の結果の question に任意の項目として足す (無ければ `other`)。設計者の付録で、使えない runner・OMM・recheck・不明点の問いに付ける値を決める。
- 答えの `choice` は、スクリプトで分かるときだけ埋める (Approve の返信なら `parse_choices`)。選択の中身は、多くの場合 `stage.decided` (OMM の repo・cache・skip、recheck の skip・report-only、runner の next-runner・stop) で残る。両方を `run_id`・`seq` で結ぶ。
- 役割の値の出所は、`episode.started` の `roles` と `run.started` の `roles_from` に入れる。値が不正で読み飛ばした warn も、`roles` の出所が `default` になることで分かる。
- 今ある記録 (runs.jsonl・run.json など) は変えない。イベントは足すだけ。kiln の記録には conductor の run_id を `atelier_run_id` として足す。

### タスクの特徴 (task.profiled)

- **測った値** (スクリプトが計算する。段階に丸め、細かい数は送らない):
  - 大きさ: 変更したファイルの数・行の数 (`git diff --numstat` の base_commit から)、PBI の数、wave の数、PBI の入力の文字数
  - 広がり: 触ったトップレベルのフォルダの数、言語の数、テストのファイルを触ったか
  - 文脈の重さ: リポジトリのファイル数 (`git ls-files`)、CLAUDE.md と規約の行数、refs と memory の数
  - 技術: 言語 (拡張子の表から)、フレームワーク (`package.json`・`pyproject.toml` などの依存名を、taxonomy の許可リストと突き合わせる)
  - 段階の境目は `taxonomy.json` に置く (例えばファイル数は 1・2–3・4–8・9–20・21+)
- **タグ** (設計者が選ぶ。語彙の外は弾く):
  - `task_type`: feature・bugfix・refactor・test・docs・infra・migration・perf
  - `domain`: frontend・backend・cli・library・data・infra・docs・skill
  - `difficulty_hint`: low・mid・high
- **アンカーとの近さ**: PBI の目的と受け入れ条件の節 (run 全体では goal) を、SSH 先の Ollama (`qwen3-embedding:0.6b`、shared-memory と同じトンネル) で埋め込む。`taxonomy.json` のアンカー (代表のタスクの文) とのコサイン類似度の上位 3 つを、`anchor_id` と近さ (小数 2 桁) で送る。アンカーのベクトルは手元にキャッシュする (taxonomy の版とモデルをキーにする)。
- ベクトルは保全先 (`~/.claude/pbi-runs/<repo>/<branch>__W-n/vectors.jsonl`) にだけ書き、`case_id`・モデル・次元を添える。herness-log には送らない (埋め込みからは元の文がある程度戻せるため)。
- 埋め込めなかったときは、近さを null にして続ける。`events.py backfill` が、近さが null の PBI を cases.jsonl から埋め込み直し、`task.profiled` を出し直す。backfill はそれに加えて、PBI ごとの最新の `task.profiled` について `## Targets` から大きさを測り直し、段階の値が変わったら `task.profiled` を出し直す (PBI のファイルが無ければ stderr に 1 行出して飛ばす。近さと大きさが両方変わるときは 1 行にまとめる)。本文が atelier の側にあるので、backfill は送り手ではなく atelier に置く (合意のときの `harness-log backfill` から場所だけ変えた)。
- `taxonomy.json` には版 (`taxonomy_version`) を付ける。語彙・段階の境目・アンカーを変えたら版を上げ、`task.profiled` に版を入れる。

### 送り手 harness-log

- 置き場所は `skills/harness-log/`。中身は、小さな Python の CLI (`harness_log.py`) と、短い SKILL.md (いつ使うか)。標準ライブラリと git だけを使う。atelier は送り手を import で使い、見つからなければ手元の events.jsonl にだけ書く。
- 設定は `~/.claude/harness-log.json` に置く (環境変数 `HARNESS_LOG_CONFIG` で場所を変えられる)。項目は `mode`・`remote`・`enabled`・`key`・`show_repos`・`clone_dir`・`spool_dir`・`embedding` (`url`・`model`)。設定が無ければ、emit も flush も何もしない。
- 記録のしかた (`mode`) は 3 つ。どれにするかは、初めて使うときにユーザーが決める (2026-11-12 の追記。下の「初めて使うとき」)。
  - `remote`: private のリポジトリに送る。`enable` までは送らない (今までどおり)。
  - `local`: 送り先を決めずに、手元のスプールにだけためる。flush は何もしない。あとで `init --remote` で送り先を足すと、鍵とスプールはそのままで `remote` になり、ためた分も `enable` の後の flush で送られる。
  - `off`: 何も記録しない (emit も flush も何もしない)。設定があるので、二度と聞かない。
- 操作:
  - `init --remote <URL>` / `init --local` / `init --off`: 設定を `enabled: false` で作り、鍵を作る。`--remote` は clone も用意する。どれも送らない。`local` の設定に `init --remote` を打つと、鍵とスプールを残したまま送り先を足す。
  - `status`: 今の記録のしかたを 1 行で出す (`none`・`off`・`local`・`remote enabled|disabled`、スプールの行の数)。
  - `emit` (関数としても使える): 許可リストで点検し、外枠を付け、スプールの `<phase>/<skill>/<YYYY-MM>/<run_id>.jsonl` に追記する。
  - `flush`: `enabled` のときだけ動く。ロックを取り、`fetch` して clone を `reset --hard origin/<branch>` でリモートに合わせ、スプールの行を clone の同じパスに追記する。`coverage.json` と `SCHEMA.md` を作り直して、1 回のコミットにする。スプールの行は push に成功したときだけ消す。push に失敗したら行はスプールに残り、`errors.log` に 1 行書き、次の flush で clone を合わせ直してから送り直す (clone 側の未 push のコミットは捨てる。正はスプール)。
  - `preview`: スプールの行と、まだ push していないコミットを、そのまま表示する。
  - `enable`・`disable`: 送るかどうかを切り替える。止めている間もスプールには書く。
- flush はどこでも失敗を外に投げない。標準エラーに 1 行出し、終了コード 0 で返す (呼び出し側の run を止めないため)。
- herness-log の並び:
  - `<phase>/<skill>/<YYYY-MM>/<run_id>.jsonl`: イベント
  - `<phase>/<skill>/coverage.json`: `task.profiled` の run 全体の行を、`task_type` × `domain` × 大きさの段階で数えた表
  - `SCHEMA.md`: 外枠の項目と、スキルごとの許可リストの写し

### atelier への組み込み

- `skills/atelier/events.py` を足す。役割は 3 つ。今の run の `run_id`・`seq` を run.json から読むこと、許可リストで点検して手元の `docs/pbi/events.jsonl` に書き、送り手に渡すこと、設計者が打つコマンド (`stage`・`profile`・`answer`・`backfill`) を受けること。どの失敗も呼び出し側を止めない。
- conductor は start・next・done・answer・abort でイベントを出す。出番が終わるたびと run の終わりに、`events.py settle` を裏で起こす (出番の費用を `agent_stats` で集めて `episode.cost` を出し、そのあと flush する)。run.json の `question` に `asked_at` を足す。
- record_run・role_results (check)・kiln_log・design_record (check-reply)・lint_run (approve) は、それぞれ自分の書き込みの横でイベントを出す。
- 設計者の付録に、いつどのコマンドを打つかを書く (stage record を書くとき・plan を確定したとき・inline で問いに答えを受けたとき・finish のとき)。本文には 1 行の道しるべだけを足す。

### 初めて使うとき (2026-11-12 の追記)

- conductor の `start` は、送り手が見つかって設定がまだ無いとき (`status` が `none`)、`ask harness-log:` の行を 1 行出す。run は止めない。
- 呼び出し側はその行を見たら、ユーザーに 1 回だけ聞く: private のリポジトリの URL を設定する / 送らずに手元にだけためる / 記録しない。答えに合わせて `init --remote <URL>`・`init --local`・`init --off` を打つ。どれを選んでも設定ができるので、次からは聞かない。
- 答えるまでの間 (設定が無い間) のイベントは、今までどおり手元の `docs/pbi/events.jsonl` にだけ残る。

### 送り始めの流れ

4 つの領域が並んで動く。同じ名前のきっかけ (`enable を打つ`) は、そのきっかけを持つ領域を同時に動かす。

```mermaid
stateDiagram-v2
  state "送り手と run" as All {
    state "送る設定" as Config {
      state "設定なし" as NoConfig
      state "記録しない" as Off
      state "手元だけ" as LocalOnly
      state "準備済み" as Prepared
      state "送っている" as Sending
      state "止めている" as Paused
      [*] --> NoConfig
      NoConfig --> Off: ユーザーが「記録しない」を選ぶ
      NoConfig --> LocalOnly: ユーザーが「手元にだけためる」を選ぶ
      NoConfig --> Prepared: ユーザーが private のリポジトリを決める
      LocalOnly --> Prepared: あとで private のリポジトリを決める
      Prepared --> Sending: ユーザーが中身を見て enable を打つ
      Sending --> Paused: disable を打つ
      Paused --> Sending: ユーザーが中身を見て enable を打つ
    }
    --
    state "承認" as Approval {
      state "未承認" as NotApproved
      state "承認済み" as Approved
      [*] --> NotApproved
      NotApproved --> Approved: ユーザーが中身を見て enable を打つ [送り先がある]
    }
    --
    state "ある 1 行" as Line {
      state "出ていない" as NotEmitted
      state "スプールにある" as InSpool
      state "手元のコミットにある" as Committed
      state "herness-log に届いた" as Delivered
      [*] --> NotEmitted
      NotEmitted --> InSpool: イベントが出る [記録する設定]
      InSpool --> Committed: 出番か run の終わりに flush がコミットする [送っている]
      Committed --> Delivered: push に成功する [送っている]
      Committed --> Committed: push に失敗する (次の flush で送り直す)
    }
    --
    state "atelier の run" as Run {
      state "進んでいる" as Going
      state "終わった" as Ended
      [*] --> Going
      Going --> Ended: run が終わる
    }
  }
  %% never 未承認 & herness-log に届いた -- enable を打つ前に、イベントが herness-log に届く
  %% never 未承認 & 手元のコミットにある -- enable を打つ前に、送るためのコミットができる
  %% never 設定なし & スプールにある -- ユーザーが選ぶ前に、イベントが手元にたまる
  %% never 記録しない & スプールにある -- 「記録しない」を選んだのに、イベントが手元にたまる
  %% never 手元だけ & 手元のコミットにある -- 「手元にだけ」なのに、送るためのコミットができる
```

- 2026-11-12 に「手元だけ」と「記録しない」を足して、もう一度 `/statechart` (from-chart) で検査した。5 つの規則はどれも成り立った (28 状態・60 遷移、規則を破る状態は 0)。

- 2026-11-11 に pstack の `/statechart` (from-chart) で、英語のラベルの写しを検査した (TLA+ の文字列は ASCII に限られるため)。3 つの規則はどれも成り立った (22 状態・47 遷移、規則を破る状態は 0)。検査の途中で、設定が無いのに `enable` で承認済みになれる穴が見つかったので、承認の遷移に「設定がある」の条件を足した。
- 図の push の失敗は「ある 1 行」の領域だけを動かし、「atelier の run」の領域には何もしない。送れなかったことで run が止まらない、という決まりは、この形で表している。
- 図に出てこない決まりは、テストで確かめる。許可リストに無い項目の値がスプールにも herness-log にも入らないこと、一度コミットに入れた行が二度送られないこと、の 2 つ。

## Testing Decisions

- 良いテストは、外から見える振る舞いだけを確かめる。CLI に渡したもの・書かれたファイル・git のリポジトリに入ったものを見る。関数の中の手順は見ない。リポジトリの今のテストと同じく、`unittest` で、一時ディレクトリと `subprocess` か `main(argv)` を使う。
- **合意済みの seam**:
  1. **送り手の CLI と関数** (`skills/harness-log`): 設定を一時ファイルにし、送り先を手元の bare リポジトリにして、`init`・`emit`・`preview`・`enable`・`flush`・`disable` を通しで確かめる。許可リストの外の項目が落ちること、`enabled: false` では bare リポジトリに何も入らないこと、push の失敗 (送り先を消す) で終了コード 0 とスプールの保持、次の flush での送り直し、二重に送らないこと、ハッシュと `show_repos` を確かめる。
  2. **conductor の CLI** (`test_conductor.py` の今の作り): start → next → done → answer → finish を通し、`docs/pbi/events.jsonl` に出た行 (種類・run_id・seq・待ち時間) を確かめる。送り手の設定が無いときに、何も送らず run が今までどおり進むことも確かめる。
  3. **events.py の CLI**: `stage`・`profile`・`answer` の語彙の点検、`profile` の測った値 (一時の git リポジトリで)、埋め込みを偽の HTTP サーバーで返したときの近さ、つながらないときの null、`backfill`。
  4. 記録を書く各スクリプト (record_run・role_results・kiln_log・design_record・lint_run) は、今のテストに「イベントの行が 1 つ出る」ことを足すだけにする。
- 先例: `test_conductor.py` (CLI を通しで回す)、`test_record_run.py` (一時の docs/pbi と偽の会話記録)、`test_omm_cache.py` (home の外のキャッシュ)。

## Out of Scope

- session-log (会話の全文を Proton Drive に残すスキル) から herness-log へ送ること。
- session-log の Jev 列が atelier の使用を数えていないことの修正。
- 名前がぶつかっている所の改名 (kiln の run_id と conductor の run_id、統計の `stats/roles.json` と設定の `roles.json`)。
- design-flow・review-flow からの送信 (送り手は共通にしておき、後で各スキルが許可リストを持って使う)。
- 埋め込みのベクトルを SSH 先の OpenSearch に置くこと (件数が増えて手元の近傍検索が遅くなったら検討する)。
- herness-log の中身を集計・可視化する側 (harness2 などのハーネス)。
- `~/.claude/skills/atelier` (今は v7.3) への反映。リポジトリで実装とテストを終えたあと、`skill-versioning` で版を足す。

## Further Notes

- 送り先の URL はリポジトリに書かず、ホームの設定にだけ置く。
- herness-log への push と、atelier への push・PR は、どちらもユーザーが頼んだときだけ行う。送り始め (`enable`) は、`preview` の見本をユーザーが見てから行う。
- 送り手は atelier の用語を知らない (ハーネスと Jev の境界の決まりと同じく、スキル固有の項目はスキルの許可リストにだけ置く)。

## 追記: hillclimb と analyzer 連携 (2026-11-11)

2026-11-11 夜に Grok と詰めた追記。上の節は変えず、差分だけをここに足す。外の集計の側 (harness2-analyzer) の設計は、Grok の box の `/workspace/harness2-analyzer/design.md` にある (Out of Scope の「herness-log の中身を集計・可視化する側」を埋めるもの)。決まったこと:

- 本文 (trajectory・transcript・review の why) は、手元の LLM と埋め込みでだけ読む。外へは送らない。
- 埋め込みの既定は、上の節と同じく SSH 先の Ollama `qwen3-embedding:0.6b`。サーバーは設定で差し替えられる。
- harness2 の側は、別のリポジトリ harness2-analyzer にする。共通のスキーマ `harness.unit/1` (1 試行 = 1 行。本文は入れない) で、atelier の `pbi.attempted` を読み込む。

### 2. 改訂の方針

1. **analyzer がそのまま食べられる形にする**。`pbi.attempted` の 1 行を、そのまま `harness.unit/1`（analyzer 設計 §4.2）に写せるようにする。足りない結び付けキー（split、change_id、trial、signature）を外枠か data に足す。
2. **hillclimb を一級にする**。分割（split）、1 変更（change）、試行（trial）、判定（round）、根因（triage）を、イベントと記録の名前として持つ。
3. **本文は今までどおり外に出さない**。シグネチャ、クラスタ ID、距離の段階は識別子なので送ってよい。ファセットの文とベクトルは手元にだけ置く。

### 3. 差分（追加・変更）

#### 3.1 外枠 `harness.event/1` に足すキー（すべて任意、null 可）
| キー | 型 | 意味 |
|---|---|---|
| `split` | 列挙 `train/heldout/regression/unsplit` | この run または PBI が、どの分割に属するか |
| `split_set_id` | id | 分割の版 |
| `change_id` | id | このとき有効な変更（§3.3） |
| `config_fp` | id | 構成の指紋 = sha12(skill_version, worker.md の sha, designer.md の sha, roles の解決値, workflow 名, route の既定) |
| `trial` | int | 同じ条件での試行番号（outer harness で replay するとき） |
| `unit_id` | id | `at:<run_id>:<pbi>:<attempt>`（design.md と共通） |

→ 送り手 `harness-log` は atelier の用語を知らない、という決まりに合わせ、上のキーは **どのスキルにも共通の外枠**として持つ（design-flow や review-flow も同じキーを使える）。

#### 3.2 split（分割）
- 何を分けるか: atelier の評価単位は **case**（`atelier.case/1`、1 PBI）。outer harness（harness2 など）は cases を replay する。分割はそこに付ける。
- manifest は home に置く（`~/.config/pbi-orchestration-jev/splits/<name>.json`）。リポジトリには置かない。中身は `{split_set_id, seed, strata: [task_type, domain, size_bucket], heldout: [case_id], frozen: true}`。層を分けるのに、task.profiled の語彙と段階を使う。
- 新しい `events.py split assign` で、case が確定したとき（finish）に、決まった乱数（`hash(case_id, seed)`）で振り分ける。一度振ったものは変えない。`split.assigned` イベントを出す（`case_id`、`split`、`split_set_id`）。
- **held-out を守る**:
  - `export_cases.py --view improver` を足す。held-out の case は、`id`、`size`、`result.passed`（集計用）だけにする。`task`、`harness.memory`、`private` は落とす。今の出力（全部入り）は `--view owner` とし、既定を owner にして今の動きを保つ。
  - 改善を担当する者（人か、outer harness の改善役）には、improver の見え方だけを渡す。手順は cases.md の付録に書く。
  - held-out の失敗を個別に読んだら、その case を held-out から外して `regression` に移す（`split.retired` イベント）。見てしまった held-out は、もう held-out ではない。

#### 3.3 change（1 ラウンドに 1 変更）
- 新しい記録 `changes.jsonl`（`harness.change/1`、analyzer 設計 §6.2 と同じ形）を、保全先（`~/.claude/pbi-runs/changes.jsonl`）に置く。
- `kind` は atelier では次のとおり: `worker-prompt`（worker.md）、`designer-prompt`（designer.md と agents）、`appendix`、`routing`（route と tier）、**`model`（roles と route のモデル。モデルを変えるのも 1 変更）**、`workflow`、`tool`（スクリプト）、`grader`（reviewer、tester の定義、verify の規則）。
- 新しい `events.py change apply --kind K --target T --hypothesis LINEAGE`: `change.applied` イベントを出し、以後のイベントに `change_id` を付ける。skill_version を上げる操作（`skill-versioning`）の横で打つ。
- 2 つ以上の kind が同じ config_fp の変化に入っていたら、`change.applied.multi: true` とし、判定から外す。
- `prompt.worker`（今の runs.jsonl の項目）は残す。`config_fp` はそれを含む上位の指紋である。

#### 3.4 1 段目のシグネチャを `pbi.attempted` に足す
- `data.signature: {sig_id, parts}`。parts は列挙だけで組む: `outcome`、`causes`（並べ替え済み）、`verify_failed_n` の段階、`findings`（critical/major/minor の段階）、`unchanged`、`memory_violation`、`test_failing`（intended/regression/unknown の組）、`fallback_any`、`runner`、`tier`。
- 自由文は使わないので、許可リストを通り、herness-log に送れる。analyzer は、これを analyzer 設計 §5.1 の 1 段目としてそのまま使う。

#### 3.5 原因（cause）を根因に写す
| atelier の `--cause` | 根因（共通） | 備考 |
|---|---|---|
| `spec` | `task` | 課題（PBI）があいまい、条件が足りない |
| `convention`、`scope`、`memory` | `harness` | 指示、柵、refs で直せる |
| `model` | `agent` | |
| `env` | `env` | |
| **新しく `grader`** | `grader` | レビュー担当やテスト担当の誤り、verify の規則の誤り（差し戻しが誤りだった、など） |
| **新しく `flaky`** | `noise` | やり直したら通った。試行で合否が割れた |

- `CAUSES` に `grader` と `flaky` を足す（`record_run.py` の列挙と、cases.md の表）。
- 原因の付いていない差し戻しの数は、今の設計どおり数える（記録の抜けとして）。

#### 3.6 2 段目: 失敗の埋め込みとクラスタの割当
- 今の埋め込みは「タスクの形」（目的と受け入れ条件 → アンカーとの近さ）だけである。これに **失敗の空間**（`kind: failure`）を足す。
  - 埋め込む文はテンプレートで組む: `causes | findings の重さ | review の why の要約 | test の failing の分類 | rework note の Expected の要約`。why と Expected は自由文である。D1 により、**手元の LLM と埋め込み（ローカルか自前のサーバー）でだけ**読んで組む。外には送らない。
  - ベクトルは今と同じく保全先の `vectors.jsonl` に置く（`space_id`、`kind`、`case_id`、`unit_id` を添える）。`space_id` は analyzer 設計 §7.2 と同じ式で出す（モデルの digest とテンプレートの版を含める）。
- クラスタを作るのは analyzer（train だけで fit）である。atelier の側は、割当を受け取って `cluster.assigned` イベント（`unit_id`、`clusterset_id`、`cluster_id`、`lineage_id`、`dist_bucket`、`how`）を出すだけにする。識別子だけなので送ってよい。
- 埋め込みサーバーの設定は、`harness-log.json` の `embedding`（`url`、`model`）を、analyzer 設計 §7.1 の形（provider、dims、normalize、prefix、timeout）に広げる。analyzer と同じファイルを読めるようにする。既定は今の設計どおり、SSH 先の Ollama `qwen3-embedding:0.6b`（D2）。

#### 3.7 試行と判定と根因の仕分けのイベント
| type | 出す人・いつ | data |
|---|---|---|
| `split.assigned` / `split.retired` | events.py | `case_id`、`split`、`split_set_id`、`reason`（列挙） |
| `change.applied` | events.py change apply | `change_id`、`parent`、`kind`、`target`（id）、`hypothesis`（lineage_id）、`multi` |
| `change.checked` | events.py leakcheck | `change_id`、`verdict`（pass/warn/block）、`overlap_bucket`、`vs`（heldout/failure/rubric） |
| `round.judged` | analyzer から返す（取り込み） | `change_id`、`base_change_id`、`verdict`（accept/overfit_suspect/regress/noise/invalid）、`d_train`、`d_heldout`（段階）、`ci_lo`、`ci_hi`（小数 2 桁）、`trials` |
| `triage.labeled` | 人か analyzer | `lineage_id`、`root_cause`、`by`（auto/human） |
| `cluster.assigned` | §3.6 | |

- **失敗の本文を指示に貼ったかの検出**（leakcheck）: worker.md、designer.md、appendix の diff を、cases の `private.rework`、held-out の case の `task`、review の why と比べる（8-gram の Jaccard、最長一致の長さ、PBI に固有の識別子が入っているか）。held-out と rubric に重なれば block、train の失敗に重なれば warn にする。結果は `change.checked` で残し、本文は残さない。adopt の PR の前に打つ手順を、cases.md に足す。
- **停滞**: `round.judged` が accept にならない回数が K（既定は 3）続いたら、設計者の finish か outer harness が `triage` を促す。grader 側の誤り（レビューの指摘と verify の不一致、テスト担当の cause=unknown の多さ、どのモデルでも同じ形で落ちること）を先に疑う。

#### 3.8 状態遷移（変更 1 つの一生）
```mermaid
stateDiagram-v2
  [*] --> Proposed: change apply
  Proposed --> Blocked: leakcheck block
  Proposed --> Running: train/held-out を replay (trial N)
  Running --> Judged: analyzer round
  state Judged {
    [*] --> J
    J --> Accept: held-out ↑
    J --> Overfit: train ↑ & held-out 横ばい
    J --> Regress: held-out ↓
    J --> Noise: どちらも ε 内
  }
  Accept --> Adopted: 人が PR で採る
  Overfit --> RolledBack: 差し戻し候補 → 人が確認
  Regress --> RolledBack
  Noise --> Running: 試行を足す
  RolledBack --> [*]
  Adopted --> [*]
  %% never: held-out の case 本文が improver の見え方に出る
  %% never: multi な変更が Accept になる
```

### 4. analyzer への取り込み（atelier → harness.unit/1）

| unit の項目 | atelier の出所 |
|---|---|
| `unit_id` | `at:<run_id>:<pbi>:<attempt>`（events.jsonl の外枠） |
| `task_id` | `case_id`（`<repo>:<work_key>:<PBI>`。外に送る形では repo と branch がハッシュになる） |
| `group` | `task.profiled` の `task_type` × `domain` × 大きさの段階 |
| `split`、`split_set_id`、`change_id`、`trial` | 外枠（§3.1） |
| `condition` | `pbi.attempted` の runner、model、effort、tier と、外枠の `config_fp` |
| `outcome` | `pbi.attempted`（outcome、verify、usd、seconds、tokens）。passed = 合格 |
| `signature` | `pbi.attempted.signature`（§3.4） |
| `embedding_ref` | 手元の `vectors.jsonl`（kind=failure） |
| `root_cause` | causes の写像（§3.5） |

- 読む先は 2 つある。手元の `docs/pbi/events.jsonl` と保全先（ベクトルや本文を使える）、または herness-log の clone（識別子だけ。複数のリポジトリをまたいで集計できる）。
- harness2 から atelier の cases を replay したときは、harness2 の trace に出る unit の `task_id` が atelier の `case_id` と一致する。そのため、atelier で実際に動いたときの結果と、replay したときの結果を同じ課題として並べられる。

### 5. テストの追加（既存の seam の流儀に合わせる）
- events.py: `split assign` が決定的であること、`--view improver` に held-out の本文が出ないこと、`change apply` で以後のイベントに change_id が付くこと、`leakcheck` の block、warn、pass（偽の diff で確かめる）。
- record_run: `--cause grader,flaky` を受けること、`signature` が許可リストを通ること。
- 送り手: 外枠の新しいキーが共通の許可リストにあること。

#### 残っている問い (既定を置いた)

- held-out の manifest は home (`~/.config/pbi-orchestration-jev/splits/`) に置く。
- `cause` に `grader` と `flaky` を足す。
- replay するのは harness2 だけにする。
