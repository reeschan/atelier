# 付録: 作業メモリ (shared-memory) の詳細

SKILL.md 0.8 の詳細。作業キーを決めるとき、ref を書くとき、引き継ぐとき、終わるときに読む。見出しの番号は SKILL.md v12.1 のもの。

## 0.8 作業メモリ（shared-memory）

設計者（オーケストレーター）は全体のメモリを幅広く見て PBI を作る。実装担当は PBI とその参照欄の ref だけを見て、PBI どおりに成果物を出す。作者の実測 (2026-10) では memory の呼び出し 32 回のうち 15 回が実装担当の search で、PBI 以上の情報は得られていなかった。

shared-memory の MCP（`mcp__shared-memory__*`）が使えないセッションでは、この節を飛ばす（参照欄は空、昇格もしない）。`write` が禁止されているセッション（検証など）では、ref の書き込みと昇格を飛ばし、PBI の「補足」に必要な事実を書く。どちらも完了報告の「発見」に書く。

| 用語 | 意味 |
|---|---|
| work | ユーザーの 1 つの依頼。引き継ぎでセッションが変わっても同じ work。番号はリポジトリごとの連番 `W-001` |
| 作業キー | `<repo>:<branch>:<work-num>` |
| 作業空間 | 作業専用の scope `project:work.<repo>.<branch>.<work-num>`。ブランチ名の `/` は `-` に、英数字と `. _ -` 以外も `-` に置き換える |
| ref | 設計者が作業空間に書く、1 件 1 事実のメモリ |

**始めるとき**
1. 作業キーを決める。repo はリポジトリのディレクトリ名、branch は今のブランチ。番号は、`repo:<repo>` と作業空間の命名から既存の `W-` を探し、無ければ `W-001`。引き継ぎから再開するときは STATUS.md の作業キーをそのまま使う
2. 作業キーと作業空間の scope を STATUS.md の先頭に書く（1章）

**リポジトリの知識は CLAUDE.md から**
- リポジトリの安定した知識（構成・規約・コマンド・落とし穴）は、階層化した CLAUDE.md にあるものとして扱う。直下の CLAUDE.md は起動時に読み込まれ、フォルダの CLAUDE.md はそのフォルダのファイルを読むときに読み込まれる（実装担当にも同じく読み込まれる）
- CLAUDE.md にある事実は、ref にも PBI の補足にも書き写さない
- リポジトリに CLAUDE.md が無いときは、下の search を v12 と同じく広めに使ってよい

**設計者が search するとき**
- search は CLAUDE.md に無いもの（過去の判断の理由、ほかのリポジトリで分かった落とし穴）を探す補助にとどめる。`repo:<repo>` と `global` を、scope を付けて 1 回ずつまで。scope を付けない search はしない（ほかの作業・ほかのブランチの ref が混ざる）
- 作業空間の scope の search は、引き継ぎから再開するときだけにする
- 例外: 引き継ぎから再開するときに作業ディレクトリに `docs/pbi/STATUS.md` が無く、引数にパスも無ければ、`tags: ["handoff"]`・`kind: "episodic"` を付けて scope なしで 1 回だけ search してよい（作業空間の scope がまだ分からないため。手順は `appendix/handoff.md` の「再開」）

**ref を書くとき（設計者だけ）**
- ref にするのは、2 つ以上の PBI・引き継ぎ・レビューの注意で使う事実だけ。1 つの PBI にしか関わらない事実は ref にせず、その PBI の「補足」に書く
- 書き込みは並列のツール呼び出しで 1 ターンにまとめる（PBI の起票の前に 1 回。レビューで分かった注意は波ごとに 1 回）。write の直後に `get` で読み直さない。返ってきた id をそのまま参照欄に載せる
- 1 件 1 事実。`write` の `scope` は作業空間、`tags` に種類（`ref:decision` 設計判断 / `ref:convention` 規約・落とし穴の抜粋 / `ref:summary` 前の PBI の成果の要約 / `ref:review` レビューで分かった注意）と `work:<branch>/<work-num>` を付ける。kind は semantic、provenance は確かめた事実なら agent_verified、推測なら agent_inferred
- 規約・落とし穴の抜粋には、出典（元の memory id・ファイル）を本文に書く
- 作業中に決めたこと（`ref:decision`）と、レビューで後続の PBI に効く注意（`ref:review`）も、後続の PBI に効くものだけを波ごとにまとめて書き足し、後続の PBI の参照欄に載せる
- 書いた ref の id は、PBI の「参照」欄（2章）に載せる。ref の中身は PBI 本文に書き写さない（外部の実行先は例外。`runners.md`）

**実装担当とレビュー**
- Agent ツールの実装担当とレビューは `subagent_type: "pbi-worker"` で起動する（6章・8章）。この定義は memory のツールのうち `get` だけを持ち、`search` と `write` を持たない（定義は `agents/pbi-worker.md`。`~/.claude/agents/pbi-worker.md` に置く）
- `pbi-worker` が見つからないセッションでは general-purpose で起動し、STATUS.md の上に「pbi-worker なし（理由）」と書く。`worker.md` の禁止の指示だけで動かす

**引き継ぐとき（6.3）**
- HANDOFF.md に加えて、作業空間に引き継ぎのメモリ（kind episodic、tags `handoff` と `work:<branch>/<work-num>`）を書く。2 回目以降は前の引き継ぎのメモリを `supersedes` に指定し、常に最新の 1 件にする
- 引き継ぎのメモリには、タイトルにリポジトリ名と作業キーの番号を入れ、本文の先頭に必ず次の 3 つを書く: 作業キー、リポジトリの絶対パス（ワークツリーならその作業ルート）、ブランチ。フォルダを選ばずに始めたセッションの `resume` は、このパスを頼りに作業リポジトリを探す（`appendix/handoff.md` の「再開」）

**終わるとき（11章）**
- 次の 3 つをすべて満たす ref を最大 3 件選んで昇格させる: 次の作業でも使える、コードを読んでも分からない、検証済み
- 昇格先はリポジトリに固有なら `repo:<repo>`、依らなければ `global`。新しいメモリとして書き、元の ref を `supersedes` に指定し、`work:<branch>/<work-num>` のタグを付ける。provenance は agent_verified
- 承認のためには止まらない。完了報告の「発見」に、昇格したメモリの id とタイトルを並べる
- 作業空間は閉じない（ほかの作業からは見えず、30 日アクセスがなければ search に出なくなる。id の `get` では後から辿れる）
