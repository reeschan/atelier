# 外部調査: マルチエージェント開発ワークフローの作り (Atelier の作り直し向け)

調査日: 2026-11-16。一次資料 (README・公式ドキュメント・SKILL.md) を WebFetch で読んだ範囲でまとめた。
`[未確認]` は一次資料で裏が取れなかったもの、`[記憶]` は今回の取得では確認できず以前の知識によるもの。

制御の場所の記号:
- (a) プロンプト・Markdown の指示を LLM が守る
- (b) 決まった動きをするスクリプト・状態機械を LLM が呼び、次の手を受け取る (Atelier の conductor.py と同じ型)
- (c) 外のプログラムが `claude -p` / Agent SDK / ランタイムを動かす (スクリプトがループを持つ)
- (d) フック (Stop / SubagentStop / PreToolUse など) で止める・差し戻す

---

## 1. 比較表

| プロジェクト | 制御 | 手順の進め方 | レビューのループ | モデルの振り分けと説明 | トークン集計 | 人の関門 | URL |
|---|---|---|---|---|---|---|---|
| obra/superpowers (subagent-driven-development) | (a) 主体。起動時の注入に SessionStart フックを使う環境あり | brainstorm → worktree → writing-plans → タスクを 1 件ずつ直列 (並列にしない)。進み具合は ledger ファイル | タスクごとに 1 回のレビュー (仕様適合と品質の 2 判定)。差し戻しは 1〜3 回目は同じ実装者を resume、4〜5 回目は 1 段上のモデルに上げる。上限を超えたら「保留にして裁定を記録」して止まる。最後に全体レビュー 1 回 + 修正 1 波 | 「その役割をこなせる最も弱いモデル」。機械的な 1〜2 ファイル = 安いモデル、統合・デバッグ = 標準、設計と最終レビュー = 最上位。モデルは必ず明示 (省略すると高い既定を継ぐ) | 明示の集計はなし。「安いモデルでも turn が 2〜3 倍なら高くつく」と注意 | 設計の承認、実行開始の "go"、ブランチの終え方の選択。実行中は不可逆・セキュリティ・外部副作用・計画破綻の 4 つだけで止まる | https://github.com/obra/superpowers / https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md |
| Anthropic feature-dev プラグイン | (a) | 7 段 (Discovery → 探索 → 質問 → 設計 → 実装 → レビュー → まとめ) を 1 つのコマンドの指示で進む | 実装後に 3 人のレビュアーを並列 (簡潔さ / バグ / 規約)。信頼度で絞る (80 以上)。直すかは人に聞く。自動の再実行なし | 記載なし | 記載なし | 理解の確認、質問への回答、設計案の選択、実装前の明示承認、レビュー後「今直す / 後で / このまま」 | https://raw.githubusercontent.com/anthropics/claude-plugins-official/main/plugins/feature-dev/README.md |
| Anthropic code-review プラグイン | (a) | 1 回の実行で 4 エージェント並列 (CLAUDE.md 準拠 ×2、バグ、git blame/履歴) | ループなし (1 パス)。指摘ごとに信頼度採点、80 未満を捨てる。既存の問題・linter で拾えるもの・CLAUDE.md に根拠がない品質論を除外 | 記載なし | 記載なし | なし (CI で PR 更新ごとに再実行) | https://raw.githubusercontent.com/anthropics/claude-plugins-official/main/plugins/code-review/README.md |
| Anthropic ralph-loop プラグイン | (d) Stop フック | 終わろうとすると Stop フックが止めて同じプロンプトを再投入 | 完了の合言葉 (`<promise>`) か `--max-iterations` で止まる。既定は無制限で、上限が「主な安全装置」 | なし | なし | `/cancel-ralph` のみ。人の判断が要る作業には使うなと明記 | https://raw.githubusercontent.com/anthropics/claude-plugins-official/main/plugins/ralph-loop/README.md |
| Claude Code dynamic workflows (公式機能) | (c) ランタイムが JS スクリプトを実行 | スクリプトの `agent()` / `pipeline()` / `parallel()` / `phase()`。途中結果はスクリプトの変数に置き、Claude の文脈には最終結果だけ | スクリプトに書く (例「2 回続けて進展なしなら止める」)。`schema` 付き agent は構造化出力、検証失敗は 5 回まで再試行 | 段ごとにスクリプトがモデルを指定可。指定なしはセッションのモデル | `/workflows` でエージェントごとのトークンを表示。25 体超か 1.5M トークン見込み超で `Large workflow` 警告。全体 1,000 体の上限 | 実行前に計画 (段の一覧) を承認。**実行中は人に聞けない** — 段ごとの承認が要るなら段ごとに別の workflow にせよと明記。完了済みエージェントは再実行時に結果を再利用 (resume) | https://code.claude.com/docs/en/workflows |
| GitHub spec-kit | (a) スラッシュコマンド + 補助スクリプト | constitution → specify → plan → tasks → implement → converge。implement と converge を "Converged" まで繰り返す | converge が収束判定 (詳細は別ページ、未読) | 記載なし | 記載なし | 各段を人が 1 つずつ実行し、結果を見てから次へ | https://github.com/github/spec-kit |
| Kiro specs | IDE 製品 (a に近い) | requirements → design → tasks。「全部実行」では依存グラフから wave を作り、wave 内は並列、wave 間は直列 | 文書に対する Markdown Review (箇所にコメント → まとめて送る) | 記載なし | 記載なし | 段の間に承認 (Quick Spec は「承認の関門なし」と対比で書かれている) | https://kiro.dev/docs/specs/ |
| cc-sdd (gotalab) | (a) スキル | kiro-discovery → spec-init/requirements/design/tasks → kiro-impl (1 反復 1 タスク)。spec-batch はロードマップを依存 wave で複数 spec に | タスクごとに新しい実装者 (TDD) + 独立レビュアー。何度も差し戻されたら auto-debug。学びは tasks.md の `## Implementation Notes` で次タスクへ | 記載なし | 記載なし | コードを書く前に契約 (spec) を人が承認 | https://github.com/gotalab/cc-sdd |
| Taskmaster (claude-task-master) | (b) に近い: MCP/CLI のツールが tasks.json を持ち、`next` が決定的に次のタスクを返す | `next` = pending/in-progress で依存がすべて済んだものを、優先度 → 依存数 → ID で選ぶ | なし (タスクの status に `review` があるだけ) | 役割で固定: main / research / fallback を config.json に。fallback は失敗時 | 価格表に無いモデルは "Unknown" と出る程度。詳細は記載なし | 記載なし | https://github.com/eyaltoledano/claude-task-master / https://github.com/eyaltoledano/claude-task-master/blob/main/docs/task-structure.md |
| BMAD-METHOD | (a) スキル・エージェントの役割 | Clarify → Plan → Build and verify → Learn。規模で入口が変わる。チケットが仕様を運ぶ | `bmad-build` が「書いてレビューする」 (詳細は別ページ、未読) | 記載なし | 記載なし | 「判断は人に残す」と方針のみ | https://github.com/bmad-code-org/BMAD-METHOD / https://docs.bmad-method.org/ |
| mattpocock/skills | (a) | grill → to-spec → to-tickets (ブロック関係付き) → implement / implement-spec (タスクグラフの ready な前線に実装サブエージェント) → code-review | code-review は Standards と Spec の 2 軸を別サブエージェントで並列 (互いに影響させない) | 記載なし | 記載なし | grilling で人が答える。ユーザー起動のスキルは別のユーザー起動スキルを呼ばない規則 | https://github.com/mattpocock/skills |
| oh-my-claudecode | (a)+(d) スキル・フック・`.omc/` の状態ファイル | team: plan → prd → exec → verify → fix のループ。autopilot / execute / verify など | verify/fix を品質関門が通るまで。具体的な回数上限は記載なし。「主なループの権限はセッションに 1 つ」と注意 | Haiku = 簡単、Opus = 複雑 と書くが、決め方の説明はなし。「30〜50% 節約」は根拠なし | HUD のステータスライン、セッション横断のコスト集計、friction report (文脈の肥大を検出) | `/deep-interview` で要件を詰める。team などは明示で起動 | https://github.com/Yeachan-Heo/oh-my-claudecode |
| ruflo (旧 claude-flow) | (a)+(d)+MCP ツール 400 超 | swarm / queen / トポロジ。README 自身が「概念図で実行の流れではない」と書く | 専用のレビューループなし | 「smart routing」「89% accuracy」は根拠なし [宣伝] | `ruflo-cost-tracker` で予算・警告 | 破壊的・ネットワーク・支出の操作は auto でも確認 | https://github.com/ruvnet/claude-flow |
| wshobson/agents | (a) エージェント定義集 | 定義集であって進行役はない | なし | 各エージェントが model alias (opus/sonnet/haiku/inherit) を宣言。理由の説明は README になし | なし | なし | https://github.com/wshobson/agents |
| aider architect/editor | (c) aider 本体が 2 回呼ぶ | architect が方針を出し、editor が編集に変換 | なし | 役割で固定: `--model` (architect)、`--editor-model` (既定は main から決まる)、`--weak-model` (コミット文・要約) | aider 本体にトークン/コスト表示あり [記憶] | `--auto-accept-architect` 既定 True (architect の案を自動で editor へ) | https://aider.chat/docs/usage/modes.html / https://aider.chat/docs/config/options.html |
| PAL MCP (旧 Zen MCP) | MCP ツール (LLM が呼ぶ) | consensus / codereview / planner / clink (外部 CLI をサブエージェントとして起動し最終結果だけ返す) | codereview は複数パス・重大度付き | `DEFAULT_MODEL=auto` だと Claude が課題ごとに選ぶ。順位は「知能スコア」。選んだ理由を出すかは記載なし [未確認] | ツールの数が文脈を食うので必要なものだけ有効に、という注意のみ | なし | https://github.com/BeehiveInnovations/pal-mcp-server |
| Claude Code Router | プロキシ (LLM の外) | リクエストごとに振り分け | なし | 現 README は「ヘッダ・本文の条件、fallback、retry」。default/background/think/longContext/webSearch の経路と、サブエージェントのプロンプトに `<CCR-SUBAGENT-MODEL>` を埋める方式は旧版の記述 [記憶・未確認] | ログに解決したプロバイダ・モデル・トークン・推定コスト | なし | https://github.com/musistudio/claude-code-router |
| LiteLLM Router | ライブラリ/プロキシ | simple-shuffle / usage / latency / least-busy / cost の戦略 + fallback・cooldown | なし | **何が選ばれたか** は `x-litellm-model-id` ヘッダと spend log に残る。**なぜ** の説明はなし | spend log、予算ルーティング | なし | https://docs.litellm.ai/docs/routing |
| OpenHands SDK | (c) SDK がループを持つ | イベントで状態を持つ会話 | なし (critic は今回未確認) | `RouterLLM`。例の `MultimodalRouter` は「画像があれば primary、なければ secondary」の規則。理由の記録は記載なし | `conversation_stats.get_combined_metrics().accumulated_cost` | `ConfirmRisky(threshold=HIGH)` などの確認方針。止まると `WAITING_FOR_CONFIRMATION`、拒否は理由付きで `reject_pending_actions()` | https://docs.openhands.dev/sdk/guides/llm-routing.md / https://docs.openhands.dev/sdk/guides/security |

---

## 2. Anthropic の公開ガイダンス (要点)

- **workflow と agent の使い分け**: 決まった手順で済む作業は「predefined code paths」の workflow で予測可能にし、複雑さは必要なときだけ足す。agent には「最大反復回数」などの停止条件を持たせる。人には「チェックポイントか、行き詰まったとき」に戻す。
  https://www.anthropic.com/engineering/building-effective-agents
- **orchestrator-workers**: 複数ファイルにまたがるコード変更は、サブタスクを入力に応じて orchestrator が決める型の代表例。evaluator-optimizer は「評価基準がはっきりしていて、繰り返しで測れる改善があるとき」に向く。(同上)
- **サブエージェントへの依頼の書き方**: 目的・出力形式・使う道具・範囲の境界を必ず書く。曖昧だと重複と抜けが出る。成果物はファイルに書いて参照だけ返す (伝言ゲームを避ける)。マルチエージェントはチャットの約 15 倍のトークン。規模ごとのエージェント数の目安をプロンプトに書く。
  https://www.anthropic.com/engineering/multi-agent-research-system
- **返す要約の大きさ**: サブエージェントは「1,000〜2,000 トークン程度の要約」を返す。必要な情報はパス等の参照で持ち、使う時に読む (just-in-time)。NOTES.md 等の外部メモで長期の状態を持つ。
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- **長時間の作業の型**: 機能一覧を JSON (`passes` フラグ) で持ち、エージェントは状態だけ変えてよい (Markdown より勝手に書き換えにくい)。1 回に 1 機能。進捗ファイル + git で次のセッションに渡す。
  https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- **サブエージェントの文脈**: 親の会話・読んだファイル・スキルは見えない。渡すのは依頼文 + CLAUDE.md + git status。必要な規則は依頼文で繰り返す。モデルは `model` フィールドか呼び出し時の指定で、省略すると親のモデル。要約が親の文脈に積もるので、詳しい結果をたくさん返させると親を圧迫する。
  https://code.claude.com/docs/en/sub-agents
- **ベストプラクティス**: 新しいレビュアーは「書いた経緯を見ていない」ことに価値がある。同じ作業を委ねても答えは良くならない。レビュアーに「skip style」を付けよ — 問題を探せと言われたレビュアーは必ず何か見つけ、全部直すと余計な抽象が増える。`/usage` でサブエージェント種別ごとの使用量を見る。
  https://code.claude.com/docs/en/best-practices
- **dynamic workflows と他の仕組みの比較表**: 「次に何を動かすか誰が決めるか」が subagents/skills = Claude、workflows = スクリプト。途中結果の置き場は前者が Claude の文脈、後者はスクリプトの変数。実行中に人へ質問はできない。
  https://code.claude.com/docs/en/workflows
- **トークン集計 (Agent SDK)**: result の `total_cost_usd` と `modelUsage` はサブエージェント分を含む。`usage` は含まない (入れ子で過小になる)。同じ message id は重複して数えない。これらはクライアント側の推定で請求額ではない。`maxBudgetUsd` で上限。
  https://code.claude.com/docs/en/agent-sdk/cost-tracking
- **フックで関門を作るとき**: `SubagentStop` / `Stop` は `decision: "block"` で差し戻せる。`PreToolUse` (matcher `Agent`) は起動前に deny できる (例: 「このタスクのレビュアー枠を使い切った」)。ただし Stop 系のフックは**スクリプトが落ちると素通り (fail open)** になる。
  https://code.claude.com/docs/en/hooks

---

## 3. 共通のパターン

1. **計画は人が承認し、実行中は止めない。止めるのは決まった条件だけ。**
   superpowers は設計承認と "go" の後は走り続け、止まるのは不可逆・セキュリティ・外部副作用・計画破綻の 4 条件だけ (SKILL.md)。feature-dev は段ごとに聞く。spec-kit は人が 1 段ずつ回す。Claude Code workflows は「実行中は聞けないので、承認が要るなら段ごとに別 workflow」。
   → 承認点を「段の境目」に固定し、その間の自動ループには必ず上限を付ける、という形が多い。
2. **ループには上限と脱出がある。**
   superpowers: 3 回同じ実装者 → 2 回上位モデル → 保留して記録。ralph-loop: `--max-iterations` が主な安全装置。workflows: 「2 回続けて進展なしなら止める」をスクリプトに書く例、全体 1,000 体上限。building-effective-agents: 最大反復回数。
3. **ワーカーには全部を渡さず、タスクの要旨ファイル + 参照を渡す。結果はファイルに書かせ、短い状態だけ返させる。**
   superpowers (brief ファイル、報告はファイル、返すのは status・commit・1 行のテスト要約)、multi-agent research (成果物はファイル、参照だけ返す)、context engineering (1〜2k トークンの要約)。
4. **レビュアーには差分と仕様だけを渡す。**
   superpowers のレビュアー = brief + 報告 + diff パッケージ + そのタスクに効く全体制約。code-review プラグインは diff と関係する CLAUDE.md。mattpocock の code-review は Standards/Spec を別エージェントに。best-practices は「書いた経緯を見ていない」ことが価値。
5. **レビューの指摘はふるいにかける。**
   code-review プラグインは信頼度 80 未満と「既存の問題・linter で拾える・規約に根拠なし」を捨てる。best-practices は「skip style」を指示せよ。
6. **モデルは「役割」で決め、作業ごとの判断は少ない。**
   aider (architect / editor / weak)、Taskmaster (main / research / fallback)、wshobson (エージェント定義に alias)、superpowers (役割の階段)。動的に選ぶもの (PAL の auto、OMC、ruflo) は**選んだ理由をユーザーに出す仕組みが一次資料に見当たらない**。LiteLLM は「何が選ばれたか」だけをヘッダとログに残す。
7. **次の作業の選択は決定的なコードで。**
   Taskmaster `next` (依存が済んだもの → 優先度 → 依存数 → ID)、Kiro と cc-sdd の依存 wave。ここは LLM に任せていない。
8. **状態はファイルに置き、会話には置かない。**
   superpowers の ledger、OMC の `.omc/state`、Anthropic の feature list JSON + 進捗ファイル、workflows のスクリプト変数と resume。
9. **トークン集計を持つものは少ない。** 持っているのは OMC (HUD・セッション横断)、ruflo (cost-tracker)、CCR/LiteLLM (プロキシのログ)、OpenHands (`accumulated_cost`)、Claude Code の `/usage`・`/workflows`・Agent SDK の `modelUsage`。スキル型 (superpowers、spec-kit、BMAD、cc-sdd、feature-dev) は持っていない。

### 制御の場所の傾向
- (a) プロンプトだけ: 最も多い (superpowers、spec-kit、BMAD、cc-sdd、mattpocock、feature-dev)。手軽だが、上限や関門は LLM が守るかどうか次第。
- (b) 決定的なツールを LLM が呼ぶ: Taskmaster の `next` が近い。ただし Taskmaster が決めるのは「次のタスク」だけで、レビュー・差し戻し・人への質問までを状態機械で持つ例は、今回の調査では Atelier 以外に見当たらなかった。
- (c) ランタイムがループを持つ: Claude Code workflows、aider、OpenHands。途中結果が LLM の文脈に乗らない。人への質問は段の境目でしかできない。
- (d) フック: ralph-loop、OMC。関門には使えるが、スクリプトが落ちると素通りになる点に注意。

---

## 4. Atelier の痛みに効きそうな手法

### 4.1 差し戻しの作り直しが、聞かずに何度も走る
- **回数を数え、超えたら止めて人へ。** superpowers は 1〜3 回目は同じ実装者を resume、4〜5 回目は上位モデル、それを超えたら「保留にして裁定を記録」して止まる。https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md
- **「進展なし」を止める条件にする。** workflows の例「2 回続けて進展がなければ止める」。https://code.claude.com/docs/en/workflows
- **直すかを人に聞く段を置く。** feature-dev はレビュー後に「今直す / 後で / このまま」を聞く。自動で作り直さない。https://raw.githubusercontent.com/anthropics/claude-plugins-official/main/plugins/feature-dev/README.md
- **差し戻しの理由を絞る。** superpowers は「仕様不適合・Critical/Important・確かめられない穴」だけで差し戻す。code-review プラグインは信頼度 80 未満を捨てる。best-practices は「skip style」。軽い指摘で作り直しのループに入らない。https://code.claude.com/docs/en/best-practices
- conductor 側の実装案: 差し戻しの回数と「直前と同じ指摘か」を状態として持ち、上限超え・進展なしのときは「次の手 = 人に聞く」を返す。

### 4.2 モデルを選んだ理由が説明されない
- 一次資料で「理由を出す」仕組みを持つものは見つからなかった。近いものは:
  - **役割で決める表を 1 つ持ち、それを根拠として示す。** superpowers の階段 (機械的 1〜2 ファイル = 安い、統合・デバッグ = 標準、設計・最終レビュー = 最上位、レビュアーは差分の大きさとリスクで、下限は中位)。「省略すると高い既定を継ぐので必ず明示」。https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md
  - **何が選ばれたかを記録に残す。** LiteLLM の `x-litellm-model-id` と spend log。https://docs.litellm.ai/docs/routing
  - **規則で選ぶ router。** OpenHands の `MultimodalRouter` は条件が規則そのもの。https://docs.openhands.dev/sdk/guides/llm-routing.md
- conductor 側の実装案: 振り分けを「PBI の特徴 (ファイル数・種類・リスク) → 規則 ID → モデル」の表にし、PBI ごとに当たった規則 ID と入力の値を承認ページに出す。説明文を LLM に書かせるより、規則の当たりを示すほうが他のツールの作りに近い。

### 4.3 レビュアーごとに全文脈を読み直してトークンが膨らむ
- **レビュアーに渡すのは brief + 報告 + diff + そのタスクに効く制約だけ。** https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md
- **再レビューは範囲を絞る。** superpowers は修正のたびに「scoped re-review」。最上位モデルの全体レビューは最後の 1 回だけ。(同上)
- **成果物はファイル、返すのは短い要約 (1〜2k トークン)。** https://www.anthropic.com/engineering/multi-agent-research-system / https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- **同じ前提を持つエージェントはプロンプトキャッシュを共有できる。** workflows では、モデル・effort・エージェント種別・ツール・schema・作業ディレクトリが同じなら先頭の部分が共有される。レビュアーの共通部分を先頭にそろえると効く。https://code.claude.com/docs/en/workflows
- **CLAUDE.md を読ませない設定がある。** サブエージェントは既定で CLAUDE.md を読むが、`omitClaudeMd: true` で外せる。https://code.claude.com/docs/en/sub-agents

### 4.4 設計記録の古さの検査が後の PBI を止める
- 直接同じ事例は見つからなかった。近い作り:
  - **次の作業を選ぶのは依存の済んだものだけ。** Taskmaster `next`、Kiro / cc-sdd の依存 wave。検査を「全 PBI の前提」でなく「その記録に依存する PBI だけの前提」にすれば、関係ない PBI は進められる。https://github.com/eyaltoledano/claude-task-master/blob/main/docs/task-structure.md / https://kiro.dev/docs/specs/
  - **止めずに記録して進む。** superpowers は曖昧な点を自分で裁定し、「決定・理由・外れたときの損」を ledger に残して進む。人に止めるのは 4 条件だけ。https://raw.githubusercontent.com/obra/superpowers/main/skills/subagent-driven-development/SKILL.md
  - **学びは次のタスクへ書き足す。** cc-sdd は tasks.md の `## Implementation Notes`。https://github.com/gotalab/cc-sdd

### 4.5 文書の方針とローカル設定のずれ
- **設定を 1 か所に置き、他はそこから読む。** Taskmaster は `.taskmaster/config.json` の models に役割ごとの provider / modelId を置き、CLI (`task-master models --set-<role>`) で変える。https://raw.githubusercontent.com/eyaltoledano/claude-task-master/main/docs/configuration.md
- **エージェント定義に alias を書き、変換表 1 つで各ツールの実モデルに写す。** wshobson/agents。https://github.com/wshobson/agents
- **モデル指定の優先順位を知っておく。** 呼び出し時の指定 > エージェント定義の `model` > `CLAUDE_CODE_SUBAGENT_MODEL` > 親のモデル。`CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` は全部を上書き。設定のずれの原因になりうる。https://code.claude.com/docs/en/sub-agents
- conductor 側の実装案: 文書に書く方針表を、conductor が読む設定ファイルから生成する (逆にしない)。起動時に両者を照合して、ずれたら最初の手で人に知らせる。

### 4.6 トークンの総計がない
- **Agent SDK / `claude -p` の result の `total_cost_usd` と `modelUsage` を足す。** サブエージェント分を含む。`usage` は含まないので使わない。同じ message id を二重に数えない。推定であって請求額ではない。https://code.claude.com/docs/en/agent-sdk/cost-tracking
- **`/usage` がスキル・サブエージェント・プラグイン・MCP ごとの内訳を出す。** https://code.claude.com/docs/en/best-practices
- **workflows はエージェントごとのトークンを表示し、25 体超 / 1.5M トークン見込みで警告。** https://code.claude.com/docs/en/workflows
- **外部モデル (Gemini、DeepSeek) はプロキシのログで数える例がある。** CCR (トークン・推定コストをログ)、LiteLLM (spend log)。https://github.com/musistudio/claude-code-router / https://docs.litellm.ai/docs/routing
- conductor 側の実装案: ワーカーの起動ごとに result の数値 (または各 CLI の使用量の出力) を状態に足し、承認ページに PBI 別・モデル別・段別で出す。予算を超えたら「次の手 = 人に聞く」。

### 4.7 制御の作りそのもの (conductor.py 3,500 行をどうするか)
- **Anthropic は「決まった手順は workflow (コード)、柔らかい判断は agent」と分ける。** https://www.anthropic.com/engineering/building-effective-agents
- **Claude Code の dynamic workflows は (c) の公式な受け皿。** ループ・分岐・途中結果をスクリプトが持ち、Claude の文脈には最終結果だけ入る。resume (完了済みエージェントの結果を再利用) あり。ただし**実行中は人に聞けない**ので、承認を挟む Atelier では「段 = 1 workflow、段の間で人に聞く」形になる。Agent tool の `model` 指定は使えるが、Gemini / DeepSeek の CLI は agent の中から呼ぶことになる。https://code.claude.com/docs/en/workflows
- **(b) を続けるなら、状態機械が持つのは「次のタスクの選択・回数・予算・関門」だけに絞るのが他の例に近い。** Taskmaster は次のタスクの選択だけを持ち、レビュー方針はスキル側。superpowers はループの上限と止める条件を Markdown に書く。
- **フックは関門の補助に。** `PreToolUse`(Agent) で「レビュアー枠を使い切った」の deny、`SubagentStop` で差し戻し。ただし Stop 系は落ちると素通り。https://code.claude.com/docs/en/hooks

---

## 5. 確かめられなかったこと
- Claude Code Router の経路の種類 (default / background / think / longContext / webSearch) と `<CCR-SUBAGENT-MODEL>` タグ: 現 README には無く、旧版の記述の記憶。設定ドキュメントは取得できなかった。
- PAL MCP の auto モードで選んだ理由を出すか: README に記載なし。
- spec-kit の converge の中身、BMAD のレビューの手順、OpenHands の critic と condenser: 別ページで未読。
- oh-my-claudecode の「30〜50% 節約」、ruflo の「89% accuracy」: 根拠が示されていない宣伝。
- aider のトークン/コスト表示: 今回のページでは確かめていない (記憶)。
- Anthropic の feature-dev / code-review プラグインがどのモデルを使うか: README に記載なし。
