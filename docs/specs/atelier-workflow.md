# 仕様: Atelier (工程とコンポーネントで組む開発フロー)

- 状態: 方向は合意済み (2026-10-24、プロジェクトのスレッドで利用者と合意)。名前は Atelier (`atelier`)
- 対象: このリポジトリのスキル pbi-orchestration-jev 全体。最後の段で名前を Atelier に変える
- 実装: 5 つの PR に分ける。この仕様は PR 1 と一緒に入れる
- 設計案のドキュメント: https://claude.ai/artifact/HjLpEujiueU7A4PacK4PSo

## Problem Statement

pbi-orchestration-jev は、PBI に分けて安いモデルに振り分ける仕組みとして育った。いまは開発全体 (調べる・設計する・分ける・作る・確かめる・見直す・直す) を回すスキルとして使われている。ただ、工程ごとに何を使うかが本文と appendix に直書きされている。

- 設計とレビューにはアダプター (appendix/adapters.md) があるが、決まったフェーズ 3 つと、Skill ツールで呼べるスキルに限られる。pstack のように、モデルから呼べない設定 (`disable-model-invocation: true`) のスキルは使えない。
- 「安いモデルに振り分ける」(Jev の振り分け) と「強いモデルに任せる」(pstack 流) は、どちらも開発の進め方の 1 つにすぎない。なのに前者だけが本体に組み込まれていて、後者を選ぶ手段が無い。
- どの工程で何が動くかを、利用者が一覧で見たり組み替えたりする手段が無い。Template Studio が扱えるのは、振り分けと優先テンプレートだけである。
- 名前が外部の点検 API (Jev) と PBI という形式に縛られていて、汎用の開発フローの道具に見えない。

## Solution

開発フローを**テンプレート** (AI Workflow Template) として定義する。テンプレートは**工程**の並びと、各工程に差す**コンポーネント**の優先順を持つ。

- **工程**は 9 つで固定する: 理解 (understand)、設計 (design)、分解 (plan)、振り分け (route)、開発 (build)、検証 (verify)、レビュー (review)、修正 (fix)、完了 (finish)。工程と工程の間には**関所** (gate) を挟める。最初の関所は Jev のゲート 1。
- **コンポーネント**は、どの工程を担い、どう呼び、何が要り、共有メモリをどこまで使うかを書いた定義ファイル (`components/<id>.json`) である。呼び方は 4 種類: `skill` (Skill ツール)、`read-skill` (SKILL.md を読んで従う。pstack はこれ)、`script` (同梱の Python)、`builtin` (エンジンの組み込みの手順)。
- コンポーネント同士は直接呼び合わない。工程ごとに決めた**成果物** (`docs/pbi/` のファイル) でだけつながる。そのため差し替えても前後の工程は変わらない。
- 各工程の最後の候補は必ず後ろ盾 (`builtin` か、`requires` の無い `script`) にする。必須は Claude Code だけ、という汎用化の原則 (generic-reorg.md) を工程ごとに守るためである。
- テンプレートの解決順は振り分けテンプレートと同じで、作業リポジトリ (`docs/pbi/workflow.json`) → home (`workflows/<名前>.json`) → 同梱 (`workflows/<名前>.json`)。
- 同梱するプリセット (このスキルの部品と Claude Code の built-in だけを使う。下の「同梱は自作のものだけ」):
  - `jev-routed`: 今の動きそのもの。既定。振り分けが同梱の既定 (全段 agent) のままなら「全部 agent」で動く
  - `high-model`: 振り分けは全段 opus。ほかは jev-routed と同じ組み込みの手順
- 他の人のスキル (mattpocock-skills・pstack・yomiyasu など) は同梱せず、部品の定義も置かない。利用者がプラグインのコマンドで入れ、`component.py add` で外部部品として home に登録する (下の「外部部品」)。
- **開発工程はどのテンプレートでも、このスキルの実装担当 (`worker.md`) のまま**にする。pstack の poteto-agent は pstack の進め方全体を読み込み、「PBI と参照だけを材料に作り、5 行で報告する」実装担当の決まりとぶつかるので使わない。高位モデル中心型との違いは、振り分けで opus に寄せることだけである。
- pstack は同梱しない。利用者が入れたプラグインを `read-skill` で参照する。入っていなければ、その工程は次の候補 (最後は builtin) で進む。
- 共有メモリの使い方はコンポーネントごとに `full` / `refs-only` / `none` で宣言する。設計者のセッションで動くもの (pstack のサブエージェントを含む) は `full`。実装担当は、agent が `refs-only` (今の pbi-worker と同じ)、外部の実行先は既定で `none`。テンプレートの `build.external_memory` で `refs-only` に広げられる。
- Template Studio に「フロー」タブを足し、コンポーネントの一覧から工程ごとに組めるようにする。
- 最後に名前を Atelier に変える。Jev の名前は「Jev 振り分け」「Jev ゲート」というコンポーネント名に残す。

## User Stories

1. As a 利用者, I want 開発フローの工程と、各工程で何が動くかを 1 つのテンプレートで見られること, so that 何が起きるかを作業の前に分かる
2. As a 利用者, I want テンプレートを替えるだけで、安いモデルへの振り分けと強いモデル中心の進め方を切り替えられること, so that 作業の性質に合わせられる
3. As a 利用者, I want pstack を入れていれば、その部品を工程に差せること, so that 良い調べ方や見直し方を Jev の流れの中で使える
4. As a 利用者, I want pstack を入れていなくても、どのテンプレートも最後まで動くこと, so that 入れていない環境で壊れない
5. As a 利用者, I want 承認一覧に、工程ごとに使うコンポーネントと、入っていなくて組み込みに戻したものが 1 行ずつ出ること, so that 想定と違う動きに気づける
6. As a 利用者, I want 実装担当が共有メモリをどこまで使うかを、テンプレートで決められること, so that 再現性と便利さの釣り合いを自分で選べる
7. As a 利用者, I want 開発工程の実装担当がどのテンプレートでも同じ決まりで動くこと, so that 今の開発の進め方が崩れない
8. As a 利用者, I want Template Studio でコンポーネントを並べてテンプレートを作れること, so that JSON を手で書かなくてよい
9. As a 利用者, I want 名前が変わっても、進行中の作業 (`docs/pbi/`) をそのまま resume できること, so that 移行で作業が止まらない
10. As a 保守する人, I want コンポーネントの定義と工程の成果物の形がファイルで決まっていること, so that 新しい部品を足すときに本文を読み解かなくてよい
11. As a 保守する人, I want テンプレートとコンポーネントの誤り (知らない工程、工程に合わない部品、最後が builtin でない、実行先にメモリを広げすぎ) をスクリプトで検出できること, so that 壊れたテンプレートで作業が始まらない

## Implementation Decisions

### 工程と成果物

| 工程 | 受け取るもの | 出すもの |
|---|---|---|
| understand | 依頼、リポジトリ | `docs/pbi/notes/<slug>.<種類>.md` |
| design | 依頼、notes | `docs/specs/<slug>.md` (見出しは adapters.md の 7 つ) |
| plan | spec | `PBI-<n>.md`・`PBI-<n>.facts.json`・`PBI-<n>.brief.md` |
| route | PBI・facts・brief | `PBI-<n>.route.json` |
| build | PBI・route | 変更と報告 `reports/PBI-<n>.md` |
| verify | PBI の検証コマンド | `verify/<報告名>.json` |
| review | diff・spec・報告 | `reviews/<名前>.md` (1 行目に件数) |
| fix | 指摘・検証結果 | `PBI-<n>.rework-<k>.md` |
| finish | すべて | 完了報告 |

- 置き場は今の `docs/pbi/` のまま (名前を変えると進行中の作業と resume が壊れる)。
- 関所は工程の後ろに付ける (`after`)。関所のコンポーネントは stage に `gate` を持つ。

### コンポーネントの定義 (`components/<id>.json`)

| キー | 中身 |
|---|---|
| `id` | `<工程>.<名前>` の形。ファイル名と同じ |
| `stages` | 担う工程 (複数可。関所は `gate`) |
| `title` | `ja` と `en` |
| `invoke` | `{"kind": "skill", "name": ..., "args"?: ...}` / `{"kind": "read-skill", "plugin": ..., "path": ...}` / `{"kind": "script", "path": ...}` / `{"kind": "builtin"}` |
| `doc` | その部品を動かす手順を書いた、スキルの中のファイル (PR 2 で全部の部品の必須のキーにした。builtin の手順もここ) |
| `where` | `main` (設計者のセッション) / `subagent-readonly` / `subagent` / `runner` (実装担当の実行先) |
| `memory` | `full` / `refs-only` / `none` |
| `requires` | `skills`・`plugins`・`cli`・`env` の名前の配列 (どれも任意) |
| `when` | `always` / `clearly-complex` / `ask-or-decide` (設計のアダプターの判定) / `adapter-gate` (adapter_gate.py の判定) / `tier-opus` / `coupling-high` / `deep`。`tier-opus` と `coupling-high` は PBI ごとに決まるので、`after: route` の関所でだけ使える |
| `then` | 選ばれたときに続けて動かす部品の id |
| `cost` | `low` / `mid` / `high` |
| `forbid` | 渡してはいけない引数や、飛ばす手順の短い文の配列 |
| `interactive`・`calls_back` | 真偽値。`calls_back` が真のものは使わない (呼び合いになる) |
| `license`・`link` | 外部のスキルのとき |

- `where` が `runner` のものは `memory` を `full` にできない。
- `builtin` は同梱の手順だけで動くので `requires` を持たない。

### テンプレート (`workflows/<名前>.json`)

```json
{
  "name": "jev-routed",
  "description": "...",
  "stages": {
    "understand": ["understand.builtin"],
    "design": ["design.grilling", "design.builtin"],
    "...": []
  },
  "gates": [{"after": "plan", "components": ["gate.jev"]}],
  "fix": {"max_rework": 2},
  "build": {"external_memory": "none"}
}
```

- 9 つの工程をすべて書く。各工程の配列は 1 つ以上で、最後は後ろ盾の部品。
- 候補は id の文字列か `{"id": ..., "when": ...}`。後者はテンプレートの中だけで `when` を上書きする (pstack の how を `always` にするなど)。
- 工程の配列の順が優先順。エンジンは前から順に、使えるもの (`requires` が満たされ、`when` が当たるもの) を 1 つだけ選ぶ。
- 関所の配列は、使えるものを全部動かす (Jev ゲート 1 と blast-radius を両方など)。
- 1 つの部品が工程と関所の両方を担ってよい (pstack の architect を、設計の工程にも、`after: route` の関所で opus 段の PBI にだけ動く形にも差せる)。
- `route` と `build` には同梱の `route.jev`・`route.opus`・`build.runners` を差す。`route.jev` の設定は今の振り分けテンプレート (`routing.json`) と優先テンプレートで、テンプレートには書き写さない。
- `fix.max_rework` は 0〜5。今の上限は 2。
- `build.external_memory` は `none` か `refs-only`。

### `flow.py` (PR 1)

- `python flow.py <pbi-dir> [--workflow 名前] [--home DIR]`。テンプレートを解決して検証し、工程ごとの計画をタブ区切りの表で出す (列: stage / component / invoke / where / memory / when / status / doc。doc は PR 2 で足した)。
- 関所の行は stage を `gate after <工程>` にし、その工程の直後に並べる。
- status は `builtin`・`script ok`・`cli missing <名前>`・`env missing <名前>`・`check skill list` (スキルとプラグインはスクリプトからは見えないので、設計者が自分のスキル一覧で確かめる)。
- 検証の誤りは `error <内容>` の行で出し、終了コード 2。表は出さない。
- 今の adapter_gate.py・select_model.py・prepare.py のふるまいは変えない。PR 1 は定義とその検査だけで、本文の手順はまだテンプレートを読まない。

### PR の分け方

1. **契約と定義の土台**: この仕様、`components/`、`workflows/` (3 つのプリセット)、`flow.py` とテスト。ふるまいは変えない
2. **エンジン化と指示文の書き直し**: 本文を「テンプレートの工程を順に回し、各コンポーネントの指示を読む」形にする。アダプター表を components に移す。generic-reorg.md の段 3 (指示文の英語化と約 110 行への削減) もここで行う
3. **pstack の部品**: `read-skill` の実行手順、関所の `coupling-high`・`deep` の判定、実装担当のメモリの強制 (`build.external_memory`)
4. **Template Studio のフロータブ**: `workflows/` の読み書きの API と画面
5. **改名**: スキルのディレクトリ・README・home の既定の場所 (`PBI_HOME` は別名として読む)。旧名のスキルは新しい名前へ案内するだけの中身でしばらく残す。リポジトリ名の変更は利用者が GitHub で行う

### エンジン化と指示文 (PR 2)

- 本文 (SKILL.md) は英語で約 110 行。止まる場面・不変条件・「事象 → 読む 1 本」の表・開始 (flow.py を動かす)・工程を順に回す規則・承認・1 PBI のチェックリスト・文脈を細く保つ規則・作業メモリの要点だけを置く。
- 工程の手順は、部品の `doc` が指す 1 本に置く: `appendix/design.md` (理解と設計)、`appendix/plan.md` (PBI の雛形・検証コマンドの書き方・facts と brief・prepare.py と route.opus)、`runners.md` (開発)、`appendix/verify.md` (1 PBI の仕分け・検証・記録)、`appendix/review.md` (最終レビューとセキュリティ)、`appendix/rework.md`、`appendix/finish.md`。部品の選び方・when の判定・read-skill の手順・STATUS.md と工程の記録の書式は `appendix/engine.md`。
- 既知の呼び出し先の表 (adapters.md) は components に移した。adapters.md には agents.json の `adapters` の設定と推奨セットだけが残る。verify-commands.md は plan.md に入れた。サブエージェントへの依頼文の雛形は `prompts/` に分けた。
- 用語集はスキルの中の `CONTEXT.md` に移し、英語と日本語の対訳にした。
- 指示文が英語になったので、スクリプトが読む書式も英語にした。進行中の作業を壊さないように、旧い日本語の書式も読む。
  - PBI の見出し (`## Verify commands` ほか): verify.py と jev-mcp は日本語の見出しも読む。jev-mcp の sections のキーは英語の見出し。
  - 依頼文の `Work root:` と STATUS.md の `Work key:`: record_run.py は `作業ルート:`・`作業キー:` も読む。
  - `--outcome` は `pass` / `rework` / `tier-up` / `unavailable` / `hold`。runs.jsonl には今までどおり日本語の値で書く (model_report.py と過去の行をそのまま使うため)。
  - 実装担当の報告は `Status:` / `Result:` / `Changed:` / `Open:` / `Details:` の 5 行。
- 外部の実行先には、今までどおり設計者が参照欄の ref を依頼文に書き写す。`build.external_memory` は実行先が自分で ref を get してよいかだけを決める (強制は PR 3)。
- 最終レビューの前の「全体の確認」(diff の読み直しと検証コマンドの再実行) は、どの部品が選ばれても必ず行う (今の動きのまま)。review.builtin はこの確認だけを指す。

### pstack の部品・when の判定・メモリの点検 (PR 3)

- **read-skill の実行**: `flow.py` が read-skill の部品の status を、プラグインの置き場 (`PBI_PLUGINS_DIR`、無ければ `~/.claude/plugins`) の下の `<プラグイン名>/` か `<プラグイン名>/<版>/` の直下に `invoke.path` があるかで決める (`plugin ok <SKILL.md の場所>` / `plugin missing <名前>`)。設計者はその SKILL.md を、部品の `where` のサブエージェントに読ませて従わせる (`appendix/engine.md`)。
- **PBI ごとの when の判定**: `flow.py <pbi-dir> --decide` が route.json から判定する。読めない route.json や型の違う欄 (tier・scores・review) があれば、表を出さずに終了コード 2。`tier-opus` は tier が opus、`coupling-high` は scores.coupling が 2、`deep` は review が deep。after: route の関所の部品は PBI ごとに 1 行、review の工程の deep の部品は「どれかの PBI が deep なら run」の 1 行。設計者は自分で判定しない。
- **メモリの点検**: 実行先に道具を渡さない仕組みは実行先ごとにまちまちなので、強制は「毎回の記録で確かめる」形にした。`record_run.py` が試行ごとに実装担当の shared-memory の呼び出しを調べ、`runs.jsonl` の `memory` に残す。agent は会話記録の tool_use を数え、get した id が PBI の Refs に無ければ `unlisted_refs` に入れる。外部の実行先は JSON / JSONL のログを読み、道具の名前の欄 (tool・name など) が shared-memory の道具の名前と完全に一致する呼び出しだけを数え、引数から get の id を取る (文中に名前が出ただけでは数えない)。Refs の id は語ごとに比べる。読めるログが無いとき、id の分からない get があるときは `violation` を null (確かめられない) にする。許す使い方は agent が refs-only、外部の実行先がテンプレートの `build.external_memory`。search・write、none での get、Refs に無い id の get を `violation` にして、標準出力に `memory violation` の 1 行を出す。設計者は STATUS.md に書き、同じ実行先が繰り返したら (モデルを問わず) `--skip <実行先>` で外し、完了報告に並べる。
- **yomiyasu**: `gate.yomiyasu` (Skill ツールの `yomiyasu:yomiyasu`、MIT、https://github.com/nanaism/yomiyasu) を `after: finish` の関所に足した (下の「外部部品」で、同梱から外し、利用者が登録する形にした)。完了報告を `docs/pbi/REPORT.md` に書いてから、日本語の報告の言い回しだけを整え、数値・id・パス・コマンド・貼った表は変えない。入っていなければ飛ばす。

### 外部部品 (PR 3)

- 利用者の求めで、同梱する部品とプリセットを、このスキルの部品と Claude Code の built-in (`code-review`・`security-review`) だけにした。他の人のスキルは、その部品の定義 (名前・リンク・ライセンスだけの JSON) も含めてリポジトリに置かない。
- スキルは利用者が Claude Code のプラグインのコマンド (`/plugin marketplace add`・`/plugin install`) で入れる。`component.py add <plugin>:<name> --stage <工程>` (関所は `--stage gate --after <工程>`) が、入った SKILL.md を読んで `<home>/components/<工程>.<plugin>-<name>.json` を書き、home のテンプレート (既定 jev-routed。無ければ同梱のものを写す) の工程の先頭か、関所の最後に差す。`disable-model-invocation: true` なら read-skill (プラグインのスキルだけ。自分のスキルはモデルから呼べるものに限る)、ほかは skill。ライセンスとリンクはプラグインの `.claude-plugin/plugin.json` から取る。`--memory`・`--forbid` で上書きできる。`--when`・`--where`・`--models` は共有の部品の定義ではなくテンプレートの候補 (`{"id", "when", "where", "models"}`) に書き、別のテンプレートの動きを変えない。同じ部品をほかの設定で登録し直すのは断る。`~/.claude/skills/<name>/` の自分のスキルも `add <name>` で同じように使える。
- 書いたあと flow.py と同じ検証をし、誤りなら書いたものを元に戻して終了コード 2。`remove <id>` で外す (id の形と components/ の直下であることを確かめ、ほかの home のテンプレートが使っていれば何も変えずに止める)。`list` で同梱と外部の部品を並べる。同梱の部品は消せず、同じ id の外部部品も作れない (どの工程も後ろ盾を失わないため)。プラグインのスキルの id に plugin 名を入れるのは、built-in の code-review と同じ名前のスキル (mattpocock-skills の code-review) とぶつからないようにするため。
- `flow.py` は同梱の `components/` のあとに `<home>/components/` も読む。
- `setup --recommended` は、承認されたら mattpocock-skills を入れ、grilling と code-review を `component.py add` で登録する。設計の呼び出し先が無いときに `setup --recommended` を勧める 1 行は出さない。README の「External components」に、mattpocock-skills・pstack・yomiyasu を入れて登録するコマンドを並べた。
- `test_component.py` が偽のプラグインと自分のスキルの置き場で add・remove・list を、`test_flow.py` が add で差した pstack の部品での `--decide` と plugin の status、同梱の部品が他の人のスキルを呼ばないことを確かめる。

### Template Studio のフロータブ (PR 4)

- 編集画面に 4 つ目のタブ「開発フロー」を足した。テンプレートを選び、9 つの工程と関所ごとに部品を並べ替え・足し・外し、候補ごとの `when` (空なら部品の既定) を選ぶ。PBI ごとの when は after: route の関所でだけ選べる。差し戻しの上限と `build.external_memory` も選ぶ。別の名前で写して新しいテンプレートを作れる。
- 足す部品の候補は、その工程 (関所は gate) を担う部品から、まだ並んでいないものだけ。工程に後ろ盾でない部品を足すと、最後の後ろ盾の手前に入れる。
- API は `GET /api/state` の `flow`、`PUT /api/workflows/<name>` (flow.py と同じ検証。home に書く)、`DELETE /api/workflows/<name>` (home の版だけ。同梱の同名があればそれに戻る)。同梱のテンプレートは書き換えない。
- 部品そのもの (components/) の登録と削除は画面では扱わず、`component.py` のまま。

### 改名 (PR 5)

- スキルのディレクトリを `skills/atelier` に、`SKILL.md` の name を `atelier` にした。README・アーキテクチャ図・CI・`.gitignore`・appendix のパスとコマンド (`/atelier ...`) も変えた。工程の部品やプリセットの名前 (`jev-routed`・`gate.jev`・jev-mcp など、判定 API の Jev を指すもの) は変えない。
- home の既定は `XDG_CONFIG_HOME/atelier` か `~/.config/atelier`。その場所が無く、同じ親の下に旧名の `pbi-orchestration-jev` があれば旧名を使う (改名前の設定・外部部品・テンプレートをそのまま読む。勝手には動かさない)。環境変数は `ATELIER_HOME` を先に読み、`PBI_HOME`・`PBI_TEMPLATES_HOME` も別名として読む。
- `skills/pbi-orchestration-jev/SKILL.md` は案内だけの中身にした。atelier がスキル一覧にあれば同じ引数で atelier を動かし、無ければ入れ方を伝えて止まる。
- リポジトリ名の変更は利用者が GitHub で行う。README の clone は旧い URL のまま (GitHub は改名後も旧い URL から転送する) で、置き場を `atelier` にする。

### テンプレートの見本と足りないスキルの導入

- 利用者の求めで、作者の PC のテンプレートを推奨の見本 `examples/workflows/recommended.json` として置いた。見本はテンプレート (`workflow`) と、使うスキルごとの入れ方 (`install`: マーケットプレイスの名前と取得元) と登録のしかた (`stage`・`after`・`where`・`memory`・`forbid`)、ライセンスとリンクだけを持つ。部品の定義は持たない (他の人のスキルの中身をリポジトリに置かない方針のまま。定義は入ったスキルの SKILL.md から作る)。
- `component.py example check <名前>` は、スキルごとに installed / missing と registered / not registered / differs を出し、足りないスキルを入れるコマンドを `install <コマンド>` の行で出す (足りなければ終了コード 3)。スクリプトは何も入れない。
- `component.py example use <名前> [--as 名前] [--replace]` は、全部入っていれば部品を登録し、テンプレートを home に書く。同じ id がほかの設定で登録済みなら止め、誤りがあれば書いたものを元に戻す。
- `setup --recommended` はこれを使う。足りないスキルをライセンスとリンク付きで見せ、1 回の承認でオーケストレーターが install のコマンドを実行し、既定にするか別名で置くかを聞いて use を走らせる。
- 名前で頼まれたテンプレートは `flow.py --workflow <名前>` で読む (SKILL.md の Start)。

### オプションの工程 (UI デザイン・マーケティング)

- 利用者の求めで、9 つの工程とは別に、テンプレートで入れたときだけ動く**オプションの工程**を 2 つ足した。`ui-design` (画面の方向性・テーマ・モックアップ・操作の動画。成果物は `docs/pbi/ui/<slug>/`) と `marketing` (誰のための作業で、どう受け取られるか。成果物は `docs/pbi/marketing/<slug>.md`)。手順は `appendix/optional.md`。
- テンプレートの `optional` に `{"stage", "after", "components"}` で置く。`after` は finish 以外の工程のどれか (既定は ui-design が design、marketing が understand。finish のあとは完了報告に成果物が載らないので置けない)。1 つのオプションの工程は 1 テンプレートに 1 回だけ。表の行は `<工程> after <工程>` で、after の工程の直後、関所より先に並ぶ。
- 関所と同じく、使えて `when` が成り立つ部品をすべて動かす (デザインのスキルのあとに動画のスキル、のように)。後ろ盾は要らず、使える部品が無ければ飛ばす。後ろ盾を必須にしないのは、入れない限り動かない工程で、無くても開発は終わるため。
- when に `ui-change` (画面を足す・見た目や操作の流れを変える) と `user-facing` (利用者に見える・お金を払う部分を足す・変える) を足した。どちらもオーケストレーターが依頼から判断し、迷えば設計の判断と同じときに 1 回だけ聞く。PBI ごとの when はオプションの工程でも使えない。
- 部品は `stages` にオプションの工程の名前を持つ。`component.py add <スキル> --stage ui-design|marketing [--after <工程>]` で登録し、doc は `appendix/optional.md` (read-skill は engine.md)。モデルから呼べるスキルは main で動く (画面の確認や質問があるため)。テンプレートにもう置いてあれば違う `--after` は断る。`remove` で最後の部品を外すと、そのオプションの工程ごとテンプレートから消す。
- 見本の `skills[].stage` にもオプションの工程を書ける。自分のスキル (プラグインでないもの) は `install.command` に入れ方のコマンド (clone して写すなど) を書け、`example check` がそのまま `install` の行に出す。
- 編集画面の「開発フロー」タブに「オプションの工程」を足した。工程ごとに入れる・入れないを選び、after と部品を並べる。保存のとき、部品の無いオプションの工程は書かない。
- 同梱のテンプレートはどちらも入れない (同梱は自作のものだけの方針のまま)。推奨の見本には、marketing に founder-skill (Jakeschincariol/founder-skill、MIT) の `founder-consumer` (`user-facing`)、ui-design に作者の design-studio (reeschan/claude-skill-design-studio) と ui-video (`ui-change`) を入れた。
- UI の動画を作るスキルは作者の PC に無かった (knowledge-digest は会話のダイジェスト、pstack の ketchup-video は作業経過のスライド) ので、作者のスキルとして `skills/ui-video` を足した。storyboard の JSON (場面ごとのページ・字幕・hover / click / type / scroll・待ち時間) から Playwright で録画し、ffmpeg があれば MP4 にする。atelier の一部ではなく、外部部品として登録して使う。

### オプションの工程: セキュリティ

- 利用者の求めで、3 つ目のオプションの工程 `security` を足した。変えたコードのセキュリティ監査をし、要約を `docs/pbi/security/<slug>.md` に書く (スキル自身の出力はスキルの既定の場所、リポジトリの外)。既定の場所は review のあと。fix より前なので、PBI が変えたファイルで確かめられた指摘は、レビューの指摘と同じくその PBI の差し戻しにする (上限に数える)。PBI の外の指摘と、確かめきれなかった候補は完了報告に書き、この回では直さない。
- when に `ask-user` を足した。オーケストレーターは自分で決めず、毎回 1 回だけ利用者に聞き、はいのときだけ動かす。route 以降に置いた工程なら PBI 一覧の承認のときに一緒に聞き、あとで止まらない (承認は route のあとなので、plan 以前に置いた工程はその工程の最初の行の前に聞く)。使えるのはオプションの工程だけで、ほかの工程と関所では flow.py が誤りにする。答えは `docs/pbi/adapters.json` に残し、resume で聞き直さない。答えが無ければ動かさない。
- 推奨の見本では、security に Cloudflare の security-audit (cloudflare/security-audit-skill、MIT) を `ask-user` で入れた。プラグインではなく Skills CLI で `~/.claude/skills/security-audit` に入る自分のスキルの形なので、`install.command` に `npx skills add ... --global --agent claude-code --yes` を書く。スキルの中身はリポジトリに入れない。対象のコードを動かすのはスキルが求めるサンドボックスの中だけで、無ければ候補のまま報告させる。

### pstack 0.15.36 への追従

- pstack 0.15.36 で `interrogate` が作り直され、敵役のサブエージェントを worktree で並べ、変更を実際に動かして示せた指摘だけを残すようになった。サブエージェントはサブエージェントを起こせないので、今までの読み取り専用のサブエージェントでは動かない。そこで、テンプレートの候補に `where` (そのテンプレートでだけ動かす場所を変える) と `models` (部品が起こすサブエージェントのモデル。`opus xhigh` の形で 1〜10 個) を足した。`models` は `where` が `main` の候補だけに書ける。部品の定義はテンプレートの間で共有するので、`when` と同じく候補に書く (登録済みの部品を消さずにテンプレートだけ替えられる)。推奨の見本では、interrogate を `main` にして、敵役を opus xhigh と sonnet high にした。
- 新しい interrogate の出力 (Intent・Findings・Dropped) には重要度が無い。実行で示せた指摘は minor にせず、受け入れ条件を破る・データを壊す・セキュリティの穴なら critical、ほかは major として `final.review.md` に写す。敵役が作る worktree (`.claude/worktrees/`) は、結果を捨てる判定の git status から外す。
- 4 つ目のオプションの工程 `model-check` (既定は design のあと) を足した。状態の多い作業で、ルールを TLA+ のモデルで確かめ、読む人の言葉の状態遷移図にする。推奨の見本では pstack の `statechart` を `ask-user` で入れた。初回は mise が TLC と Java (約 300 MB) を入れるので、聞くときにそれを伝え、はいをダウンロードの許可とする。使えなければ、仕様に Mermaid の `stateDiagram-v2` を手で描いて承認のときに確かめてもらう。合意したルールは仕様に、破れたルールの道筋は計画がテストにする。
- pstack の Orchestrate と poteto-mode から、自分の言葉で 2 つを取り込んだ: PBI のレビューは作った担当と別のモデルにする (opus で作ったものは sonnet high で見る)、最初のメッセージと仕様と完了報告で、利用者の目的を自分の言葉で言い直す。
- 最終レビューの重要度の基準を決め、承認済みの ADR との食い違いは見送らない (コードを直すか、新しい ADR を起こすかを利用者が決める) ことにした。worker は、PBI と仕様で決まっていない利用者に見える振る舞いを決めずに Open に書く。

## Testing Decisions

- generic-reorg.md と同じく、スクリプトを CLI として一時ディレクトリ (docs/pbi と home) で動かし、出力だけを見る。
- PR 1 の seam は `flow.py` の CLI。同梱の 3 つのプリセットが誤りなく解決されること、解決順 (作業リポジトリ → home → 同梱)、検証の誤り (知らない工程、工程の抜け、工程に合わない部品、最後が builtin でない、知らない部品、runner に full、`calls_back`、`max_rework` の範囲、`external_memory` の値)、`requires.cli` と `requires.env` の status を確かめる。
- 同梱のコンポーネントの定義がすべて形どおりであることを、定義ファイルを全部読むテストで確かめる。
- 既存のテストはそのまま通す (ふるまいを変えないことの確認)。

## Out of Scope

- 工程の追加・削除 (9 つで固定)。
- pstack の本文の同梱。自分で手を入れたくなった部品だけ、あとで個別に同梱に切り替える。
- poteto-agent を実装担当にすること。
- 作者の個人スキル (design-flow・review-flow) を同梱の候補にすること (phase-adapters.md と同じ扱い)。
- Jev の API 側の変更。

## Further Notes

- pstack (https://github.com/cursor/plugins/tree/main/pstack、MIT、Lauren Tan) のスキルは 2026-10 の 0.15.30 で確認した。全部が `disable-model-invocation: true` なので、Skill ツールでは呼べない。Claude Code では `<pstack>/docs/harness/claude-code.md` の対応表を読んでから SKILL.md に従う。
- yomiyasu (https://github.com/nanaism/yomiyasu、MIT) は、完了報告や PR 本文の推敲に使える候補として、PR 3 で finish のコンポーネントに足す。
