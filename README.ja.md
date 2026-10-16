# pbi-orchestration-jev

[English](README.md) | 日本語

複数ファイルにまたがる実装を、**PBI (Product Backlog Item) に分けてサブエージェントに渡す**ための [Claude Code](https://claude.com/claude-code) のスキルです。メインのセッションがオーケストレーターになり、PBI の起票・担当モデルの決定・レビュー・差し戻しを受け持ちます。実装はサブエージェント (opencode CLI の DeepSeek / Codex CLI / Antigravity CLI の Claude / Claude Code の Agent ツール) が行います。

途中の判断の一部は、判定 API の **Jev** ([TypeSafe](https://api.typesafe.ai)) に短い文章で聞きます。ただし合否は検証コマンドの実行結果で決め、回数は状態ファイルで数えます。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/architecture-dark.svg">
  <img src="assets/architecture-light.svg" alt="pbi-orchestration-jev のアーキテクチャ図。開発者が Claude Code のオーケストレーターに作業を頼み、承認し、引き継ぎ後に再開する。オーケストレーターは prepare.py (Jev のゲート 1 と select_model.py) で段と実行先の順番を決め、opencode・Codex・Antigravity・pbi-worker (Agent ツール) に PBI を渡し、verify.py と unchanged.py で検証し、Jev には文章だけで報告の仕分けと修正の規模を聞き、対象リポジトリの docs/pbi/ に状態と HANDOFF を記録する。任意の shared-memory には作業ごとの ref と引き継ぎのメモリを書き、pbi-worker は ref を get するだけ。任意で rtk がコマンド出力を圧縮する。Template Studio は OpenRouter と Artificial Analysis から作ったモデル一覧を画面に出し、選んだ試す順番を docs/pbi/routing.json に書く。優先テンプレートの Web ページ (claude.ai の Artifact) は templates.py と同期し、overrides.json で振り分けを期限付きで上書きする。">
</picture>

図のソースは `assets/architecture.d2` です ([D2](https://d2lang.com) で描いています)。

## 特徴

- **モデルは決定表で決める**: オーケストレーターが数えた事実 (変更ファイル数・モジュール数・機密性) と、Jev による要約の判定 (新規性・結合度・あいまいさ) から、`select_model.py` が段 (haiku / sonnet / opus) を決めます。勘では決めません。
- **実行先を順に試す**: 段ごとに「上から試す実行先の並び (`route`)」を出します。利用上限やエラーで使えなければ、次の実行先へ移ります。
- **合否は検証コマンドで決める**: サブエージェントの報告も Jev の仕分けも、そのまま信じません。オーケストレーターが検証コマンドを自分で実行し、受け入れ条件を 1 つずつ判定します。
- **差し戻しは 2 回まで**: 修正の規模を Jev に聞き、haiku の段は 1 回だけ sonnet の段に上げられます。設計から見直す必要があるときや、2 回差し戻しても通らないときは止めて、ユーザーの判断を待ちます。
- **Jev に送るのは文章だけ**: PBI 本文、要約、報告の抜粋、差し戻し文だけを送ります。ソースコード、diff、環境変数、認証情報は送りません。
- **オーケストレーターの文脈を細く保つ**: オーケストレーターの文脈に入ったものは、以後の全ターンで読み直されます。実装担当は最後に 4 行の報告だけを返して詳細はファイルに書き、長いコマンドの出力はログファイルに書き、PBI 本文は 1 回だけ書いてパスで渡します (`worker.md`、`SKILL.md` の 0.7)。
- **文脈が大きくなったら引き継ぐ**: PBI の波の区切りで `agent_stats.py --ctx` で文脈の大きさを測り、250,000 トークン以上なら `docs/pbi/HANDOFF.md` (と、共有メモリがあれば引き継ぎのメモリ) を書いて止まります。`/clear` のあと `/pbi-orchestration-jev resume` で続きから再開します。フォルダを選ばずに始めたセッションからは `/pbi-orchestration-jev resume <リポジトリのパス>` で再開でき、パスを省くと引き継ぎのメモリから作業リポジトリを探します。
- **Opus は設計とレビュー、実装は安いモデル**: Opus で実装するのは設計判断が要る PBI だけです。機密に触れる PBI や結合度の高い PBI は Sonnet で実装し、Opus のオーケストレーターが重点的にレビューします。
- **[rtk](https://github.com/rtk-ai/rtk) でコマンド出力を圧縮 (任意)**: 同梱のフックが、スキルが有効にしたセッションだけ Bash / PowerShell のコマンドを `rtk <cmd>` に書き換えます。
- **機械的な手順はスクリプトで**: Jev のゲート 1 (要約の判定・PBI の品質) と決定表の判定は `prepare.py`、検証コマンドの実行は `verify.py` が 1 回で行い、オーケストレーターには表だけを返します。PBI が触ってはいけないファイルは、`unchanged.py` が起動前にハッシュを取って確かめます。スキル本文も、毎回要る中核と、場面ごとに読む `appendix/` に分けてあります。作者の実測 (2026-10、6 タスク) では、この 2 つで費用が 15% 下がりました。
- **実装担当には必要十分な情報だけ (任意)**: 共有メモリの MCP (`mcp__shared-memory__*`) があるとき、オーケストレーターだけが作業ごとの空間に「ref」(1 件 1 事実) を書き、PBI の参照欄に id を並べます。実装担当用の `pbi-worker` はメモリの `get` だけを持ち、search と write はできません。
- **CLAUDE.md を前提にする**: リポジトリの安定した知識 (規約・コマンド・落とし穴) は、階層化した CLAUDE.md にあるものとして扱い、オーケストレーターの検索は補助にとどめます。
- **試す順番を画面で決める**: Template Studio で、使うモデルを選ぶと性能・コスパ・バランスの 3 案が出ます。並べ替えて「適用」を押すと、`docs/pbi/routing.json` に書かれます (下の「Template Studio」)。
- **期限付きで振り分けを上書きする**: 優先テンプレートをオンにすると、期限までのあいだ段ごとの route を差し替えます。期限が来たら続けるかを聞きます。設定は Web ページ (Artifact) からもできます (下の「優先テンプレート」)。
- **統計を取る**: `agent_stats.py` が、Claude Code の会話記録からモデル別・エージェント別のトークン数、プロンプトキャッシュの作り直しの回数、今の文脈の大きさを集計します。

## 流れ

```mermaid
flowchart TD
  A[設計と PBI への分解] --> B[事実を数える<br/>facts.json]
  B --> C[prepare.py<br/>Jev: 要約の判定と PBI の品質<br/>select_model.py: 段と route]
  C --> F{ユーザーの承認}
  F --> G[波を route の順に実行<br/>opencode → Codex → Antigravity → Agent]
  G --> H[Jev: 報告の仕分け]
  H --> I[レビュー<br/>verify.py で検証コマンドを実行]
  I -->|fail| K[Jev: 修正の規模]
  K -->|差し戻し 2 回まで| G
  K -->|設計の見直し / 上限| L[止めてユーザー判断]
  I -->|pass| M{文脈 ≥ 25 万?}
  M -->|いいえ・PBI が残る| G
  M -->|はい| N[HANDOFF.md・引き継ぎのメモリ<br/>/clear・resume]
  N --> G
  M -->|すべて完了| J[完了報告と統計]
```

## 振り分け

| 条件 (上から順に評価) | 段 | レビュー |
|---|---|---|
| `novelty` = 2 かつ `ambiguity` = 2 (設計判断が要る) | opus | normal |
| `sensitive` が true | sonnet (Agent ツールだけ) | deep |
| `coupling` = 2 | sonnet | deep |
| どれかの score が 1 以上、または変更ファイル ≥ 4、またはモジュール ≥ 2 | sonnet | normal |
| 上のどれでもない | haiku | normal |

| 段 | 1. opencode | 2. Codex (`CODEX_UNTIL` まで) | 3. Antigravity (`agy`) | 4. Claude Code の Agent ツール |
|---|---|---|---|---|
| haiku | `opencode-go/deepseek-v4.1-flash` | `gpt-6-astra` effort `medium` | `claude-sonnet-5-5-low` | haiku、effort `max` |
| sonnet | `opencode-go/deepseek-v4.1-flash` | `gpt-6-astra` effort `high` | `claude-sonnet-5-5-high` | sonnet、effort `high` |
| sonnet (sensitive) | (使わない。外のサービスに出さない) | (使わない) | (使わない) | sonnet、effort `high` |
| opus | (使わない) | (使わない) | (使わない) | opus、effort 既定 |

- Jev の confidence が 0.7 未満の score は 1 段下げて扱います。Jev が使えないときは score をすべて 1 として扱います。
- Codex は `select_model.py` の `CODEX_UNTIL` の日まで使います。過ぎると route から外れます。使わないなら、この日付を過去にしてください。
- Antigravity には haiku が無いので、haiku の段は Sonnet の low にしています。
- `--skip opencode,agy` で、このセッションで上限や拒否が出た実行先を外せます。`--all docs/pbi` は全 PBI を 1 回で判定し、1 PBI 1 行の表だけを出します。
- レビューが deep の PBI は、Opus のオーケストレーターが diff を全部自分で読みます。normal で 300 行を超える diff は、Opus のレビュー担当のエージェントに読ませます。

## Template Studio (振り分けテンプレートを画面で組む)

上の表の「どの実行先・モデルを、どの順に試すか」は、画面で選んで差し替えられます。段の決め方 (決定表) は変わりません。

```
/pbi-orchestration-jev templates
```

Claude Code でこう呼ぶと、スキルが手元 (127.0.0.1) に小さなサーバーを立て、ブラウザで画面を開きます。スキルを通さずに開くときは次を実行します。

```sh
python ~/.claude/skills/pbi-orchestration-jev/studio/serve.py --target <対象リポジトリのルート>
```

1. **使うモデルを選ぶ**: 会社ごとのカードで、契約している実行先のチップだけを残します。
2. **案を選ぶ**: 選んだモデルから、性能・コスパ・バランスの 3 案が出ます。作業の重さ (軽い作業 / ふつうの作業 / 設計が要る作業) ごとの性能の下限も変えられます。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/studio-proposals-ja-dark.png">
  <img src="assets/studio-proposals-ja-light.png" alt="Template Studio の画面。会社ごとのカードでモデルを選び、性能・コスパ・バランスの 3 案から選ぶ。" width="800">
</picture>

3. **試す順番を整える**: 作業の重さごとに、ドラッグか ↑ ↓ で並べ替え、チェックで使う・使わないを切り替えます。最後に受ける Claude Code のモデルも選べます。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/studio-order-ja-dark.png">
  <img src="assets/studio-order-ja-light.png" alt="試す順番を調整する画面。作業の重さごとの列で、モデルを並べ替えたりチェックを外したりする。" width="800">
</picture>

4. **適用する**: 「このリポジトリに適用」を押すと `docs/pbi/routing.json` に書かれ、サーバーは閉じます。前のものは `routing.prev.json` に残ります。以後 `prepare.py` はこの順番で実行先を試します。

- 画面は日本語と英語に対応しています。ブラウザの言語に合わせて開き、右上の「EN / 日本語」ボタンで切り替えられます (選んだ言語は次回も使います)。URL に `?lang=ja` か `?lang=en` を付けても切り替えられます。
- モデル一覧 (`studio/catalog.json`) は `python studio/update_catalog.py` で更新します。価格と新しいモデルは OpenRouter、性能は Artificial Analysis のコーディング指標 (環境変数 `AA_API_KEY` があるとき) から取ります。取るまでの値は仮の値で、画面にそう出ます。
- サーバーが書くのは `docs/pbi/routing.json` だけです。起動ごとの合言葉、Host の確認、形の確認のどれかが合わなければ書きません。
- 設計の詳細は [docs/template-studio.md](docs/template-studio.md) にあります。

## 優先テンプレート

**優先テンプレート**は、振り分けを期限付きで上書きする仕組みです。いつもの振り分けは Template Studio で決める `routing.json` で、優先テンプレートはその上に重なります。段ごとの route に名前を付けて保存し (形は `routing.json` と同じ)、期限を決めてオンにすると、その間は上書きした段の route がテンプレートのものになります (上書きの無い段はそのまま。最後に受ける Claude Code は `routing.json` のその段のもの)。期限を過ぎたときの扱いは、`ask` なら続けるかを聞き (「延長 / やめる / 期限なし」に答えます)、`drop` なら黙って外れます。同梱の Web ページ (専用のデータベースを持つ Artifact) では、今の適用・テンプレートの一覧・履歴を見られ、適用の開始・延長・停止ができます (スマホからも使えます)。

テンプレートで変えられないことが 2 つあります。

- `sensitive` な PBI は、テンプレートが何を言っても外部の実行先に出ません。
- 設計判断の見直しとレビューの担当は、決定表のままです。

テンプレートは個人の設定です。`templates/` (スキルの隣、版管理の外) に、写し (`overrides.json`)・Web ページの URL (`web.json`) とともに置き、リポジトリにはコミットしません。同梱の例を試すには、コピーして置きます。

```sh
mkdir -p skills/pbi-orchestration-jev/templates
cp skills/pbi-orchestration-jev/examples/templates/deepseek-all.json skills/pbi-orchestration-jev/templates/
```

この例は全段の route を `opencode-go/deepseek-v4.1-flash` の effort `max` にします。優先テンプレートの手順には交互グループ `{"rotate": [手順, 手順, ...]}` も書けます。PBI の番号で先頭のメンバーが決まり、残りのメンバーはその PBI の控えとして後ろに続くので、PBI がメンバーに等分に配られます (例: 2 つの無料枠のモデルを交互に使い、だめなら 2 つの有料のモデルを交互に使う)。ファイルの形・置き場所の決め方・手順の全部は `skills/pbi-orchestration-jev/appendix/priority-templates.md` にあります。

## 必要なもの

| もの | 必須 | 用途 |
|---|---|---|
| [Claude Code](https://claude.com/claude-code) | 必須 | オーケストレーターと Agent ツール |
| Python 3.10 以上 | 必須 | `prepare.py`・`select_model.py`・`verify.py` などのスクリプトと Jev クライアント |
| Jev の API キー | 推奨 | 無ければ Jev のゲートを飛ばし、既定値で進む |
| shared-memory の MCP (`mcp__shared-memory__*`) | 任意 | 作業ごとの ref と引き継ぎのメモリ。無ければ PBI の補足に書く |
| [opencode](https://opencode.ai) + OpenCode Go | 任意 | haiku・sonnet の段の第一候補 |
| [Codex CLI](https://github.com/openai/codex) | 任意 | 次の候補 (`CODEX_UNTIL` まで) |
| Antigravity CLI (`agy`) | 任意 | Google AI のサブスクの Claude |
| Node.js | 任意 | rtk のフックと、JS 版の Jev クライアント (`jev.mjs`) |
| [rtk](https://github.com/rtk-ai/rtk) | 任意 | コマンド出力の圧縮 |

使えない実行先は、route の次へ移るだけです。Claude Code だけでも動きます。

## 導入

```sh
git clone https://github.com/reeschan/pbi-orchestration-jev.git
cp -r pbi-orchestration-jev/skills/pbi-orchestration-jev ~/.claude/skills/
mkdir -p ~/.claude/agents && cp pbi-orchestration-jev/skills/pbi-orchestration-jev/agents/pbi-worker.md ~/.claude/agents/
```

`pbi-worker` は実装担当とレビュー用のサブエージェントの定義です。Claude Code を再起動すると使えるようになります。置かないときは general-purpose で動きます (メモリの search を止める仕組みが無くなるだけです)。

Jev の API キーは、次のどちらかに置きます (クライアントはこの順で探します)。

1. 環境変数 `TYPESAFE_API_KEY`
2. 資格情報ファイル `~/.config/typesafe/credentials.json` (`{"api_key": "..."}`)。場所は環境変数 `TYPESAFE_CREDENTIALS_FILE` で変えられます。

キーをリポジトリやスキルのファイルに書かないでください。エージェントが資格情報ファイルを読まないよう、Claude Code の `settings.json` の `permissions.deny` に入れておくことをお勧めします。

### rtk を使うとき (任意)

[rtk](https://github.com/rtk-ai/rtk) は、よく使うコマンド (git・テスト・ビルド) の出力を圧縮する CLI のプロキシです。このスキルは、全セッションで有効になる `rtk init -g` を使いません。代わりに、同梱のフックが、スキルが `rtk/rtk-mode.mjs on` を実行したセッションだけコマンドを書き換えます。

1. rtk を入れて (例: `winget install rtk-ai.rtk`、または `cargo install --git https://github.com/rtk-ai/rtk`。Git URL なしの `cargo install rtk` は別物です)、`rtk gain` が動くことを確かめます。
2. `~/.claude/settings.json` にフックを足します。

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash|PowerShell",
           "hooks": [{ "type": "command", "command": "node \"<HOME>/.claude/skills/pbi-orchestration-jev/rtk/rtk-gate.mjs\"", "timeout": 10 }]
         }
       ]
     }
   }
   ```

3. Claude Code を再起動して、フックと PATH 上の `rtk` を読み直させます。

フラグのファイル (`~/.claude/state/rtk/<session_id>.on`) が無いとき、または `rtk` が PATH に無いときは、フックはコマンドをそのまま通します。opencode と Codex には、rtk の `rtk init -g --opencode`・`rtk init -g --codex` が全体で効きます。

### opencode を使うとき

- OpenCode Go の DeepSeek は、ワークスペースの Privacy 設定が Global でないと HTTP 400 になります。
- `opencode run` を同時に起動すると `database is locked` で落ちることがあります。スキルは起動を 30 秒ずつずらします。
- fnm や nvm で入れた Node に opencode を入れていると、Claude Code の PATH に出ないことがあります。探し方は `runners.md` にあります。

### Antigravity を使うとき

- 2026-10-13 時点では、ヘッドレス (`agy -p`) でコマンドの実行が自動で拒否され (`RunCommand`)、作業が進みませんでした。スキルは、最初にこの拒否が出たら、そのセッションの残りでは `agy` を外します。
- `agy` は、許可の無いコマンドを**黙って拒否し、終了コード 0 で返します**。拒否は結果の JSON の `denied_actions` と標準エラーにしか出ません。スキルは毎回 `denied_actions` を確かめます。
- 許可ルールは `~/.gemini/antigravity-cli/settings.json` の `permissions.allow` に書きます。サンドボックスが既定でオフなので、`command(node)` だけでは足りず、`unsandboxed(node)` も要りました (agy 1.3.3 で確認)。

## 使い方

Claude Code で、複数ファイルにまたがる実装を頼むときに呼びます。

```
/pbi-orchestration-jev <やりたいこと>
```

振り分けテンプレートを画面で決めるときは `/pbi-orchestration-jev templates` と呼びます (上の「Template Studio」)。

作業の記録は、対象リポジトリの `docs/pbi/` に残ります。

| パス | 中身 |
|---|---|
| `docs/pbi/STATUS.md` | PBI ごとの状態・担当・差し戻しの回数 |
| `docs/pbi/HANDOFF.md` | 引き継いだとき、次のセッションに要ること |
| `docs/pbi/PBI-<n>.md` | PBI 本文 |
| `docs/pbi/PBI-<n>.facts.json` / `.brief.md` / `.route.json` | 振り分けの材料と結果 |
| `docs/pbi/worker.md` | 実装担当の進め方と報告の形 (スキルからコピー) |
| `docs/pbi/tools/unchanged.py` | 触らない範囲の検証 (スキルからコピー) |
| `docs/pbi/routing.json` | 試す順番 (Template Studio が書く。無ければ既定) |
| `docs/pbi/reports/`、`docs/pbi/notes/` | 実装担当の詳細な報告と、調べ物の結論 |
| `docs/pbi/verify/`、`docs/pbi/reviews/` | 検証コマンドのログと、任せたレビューの結果 |
| `docs/pbi/jev-log.jsonl` | Jev の判定と採った行動 |
| `docs/pbi/jev.config.json` | Jev のゲートとしきい値 (初回にテンプレートから作る) |
| `docs/pbi/opencode/`、`docs/pbi/codex/`、`docs/pbi/agy/` | 各実行先に渡した文章と出力 |
| `docs/pbi/stats/` | トークンとキャッシュの統計 |

止まってユーザーを待つのは、PBI 一覧の承認、差し戻しの上限 (または設計の見直し)、破壊的な操作の前、文脈が大きくなったときの引き継ぎの 4 つだけです。引き継いだら `/clear` のあと `/pbi-orchestration-jev resume` を実行します。

フォルダを選ばずに始めたセッション (Claude のデスクトップアプリが空のフォルダを作って動かすもの) では、作業ディレクトリに `docs/pbi/STATUS.md` がありません。そのときは次の順で作業リポジトリを決めます。

1. `/pbi-orchestration-jev resume <リポジトリのパス>` のパス
2. パスが無ければ、共有メモリの引き継ぎのメモリ (タグ `handoff`) にあるパス。複数の作業が引き継ぎ中なら、どれを再開するかを聞きます
3. それでも決まらなければ、ユーザーに聞きます

決まったら、`STATUS.md` とブランチが合うことを確かめ、セッションをそのフォルダへ移すか、以後は絶対パスで作業します。このため、`HANDOFF.md` と引き継ぎのメモリには、リポジトリの絶対パスとブランチを必ず書きます。

## ファイル

```
skills/pbi-orchestration-jev/
├── SKILL.md                    # スキル本文 (オーケストレーターへの指示)
├── runners.md                  # opencode / Codex / Antigravity への渡し方
├── verify-commands.md          # 検証コマンドを書くときの注意
├── worker.md                   # 実装担当の進め方と報告の形
├── appendix/                   # 場面ごとに読む付録 (memory・rework・handoff・finish・jev・templates・priority-templates)
├── agents/pbi-worker.md        # 実装担当・レビュー用のサブエージェントの定義 (~/.claude/agents に置く)
├── prepare.py                  # Jev のゲート 1 と決定表を 1 回で実行し、表だけを出す
├── verify.py                   # PBI の検証コマンドを実行し、終了コードと末尾だけを表で出す
├── select_model.py             # 決定表: 段・route・レビューの深さ
├── routing.default.json        # 振り分けテンプレートの既定 (docs/pbi/routing.json で差し替え)
├── studio/                     # テンプレートを画面で組む (index.html・serve.py・update_catalog.py・catalog.json)
├── templates.py                # 優先テンプレート: 検査、Web ページからの同期、適用・回答・写しの書き込み
├── web/priority-templates.html # 優先テンプレートを見て設定する Web ページ (Artifact)
├── examples/templates/         # 優先テンプレートの例 (templates/ にコピーして使う)
├── unchanged.py                # PBI が触ってはいけないファイルをハッシュで確かめる
├── agent_stats.py              # 会話記録からトークン・キャッシュ・文脈の大きさを集計
├── jev.config.template.json    # Jev のゲートとしきい値の雛形
├── opencode.config.json        # opencode の権限 (push・reset・リポジトリ外を拒否)
├── rtk/
│   ├── rtk-gate.mjs            # rtk 用の PreToolUse フック (任意)
│   └── rtk-mode.mjs            # このセッションの rtk の有効・無効を切り替える
├── jev/
│   ├── jev.py / jev.mjs        # Jev クライアント (Python / JS)
│   └── test_jev.py / jev.test.mjs
├── test_select_model.py
├── test_templates.py / test_prepare.py / test_unchanged.py
└── test_agent_stats.py
```

### Jev クライアント

```sh
python jev/jev.py <config.json> <ゲート名> <文章ファイル>   # config のゲートを使う
python jev/jev.py --request <request.json>                 # リクエストを直接渡す
```

成功時は応答の JSON を標準出力に出し、終了コード 0 で終わります。Jev の失敗時は `{"error": ...}` を出して 1、引数の誤りは 2 です。429 / 529 は最大 3 回、5xx・接続エラー・タイムアウトは 1 回だけ再試行します。

## 調整

- 決定表としきい値は仮の値です。PBI が 10〜20 件たまったら、モデル別の結果 (一発合格・差し戻し・昇格) を見て、`select_model.py` と `jev.config.json` を見直してください。
- 実行先のモデルと effort は、テンプレート `routing.json` で変えられます (形は `routing.default.json`)。対象リポジトリの `docs/pbi/routing.json`、またはスキルのディレクトリの `routing.json` に置くと、`select_model.py` がそちらを使います。無ければ今までの表のままです。
- テンプレートは画面でも組めます (上の「Template Studio」)。
- 引き継ぎのしきい値 (250,000 トークン) と、sonnet の段の opencode の結果も同じ時期に見直してください。

## テスト

```sh
cd skills/pbi-orchestration-jev
python -m unittest discover -p 'test_*.py'
python -m unittest discover -s studio -p 'test_*.py'
cd jev
python -m unittest test_jev
node --test --test-reporter=tap jev.test.mjs
```

Jev クライアントのテストは、127.0.0.1 のモックサーバーだけを相手にします (本物の Jev は呼びません)。

## ライセンス

[MIT](LICENSE)
