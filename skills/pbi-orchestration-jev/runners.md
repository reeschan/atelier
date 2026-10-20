# 実行先ごとの渡し方（opencode / Codex / Antigravity / dsh）

route に出た実行先を初めて使う前に、その節だけを読む。依頼文（SKILL.md 6 章）は `docs/pbi/<実行先>/<報告名>.prompt.md` に書き、末尾に「要約の書き先: `docs/pbi/<実行先>/<報告名>.last.md`」を足す。リポジトリのルートで Bash から `run_in_background` で 1 件ずつ起動する。cd はリテラルの絶対パスで行う（変数で cd すると Claude Code の権限確認で止まる）。route に出る外部の実行先は、prepare.py が agents.json と CLI の検出で照合済み。見つからない・使わない・選んでいないモデルの手順は、承認一覧の「外した手順」に出て route に入らない。

外部の実行先は shared-memory の `get` を使えないものとして扱う。PBI の「参照」欄に ref があれば、設計者がその本文を `get` して、依頼文のファイルの末尾に「## 参照（作業空間の ref）」として id ごとに書き写す。依頼文には「参照欄の ref は下に写してある。memory は使わない」と書き足す（SKILL.md 0.8）。

報告は `.last.md`（4 行）だけを読む。`.jsonl`・`.log`・`.json` は丸ごと読まず、下に書いた項目を `grep -m` や `tail -n` で取り出す。

差し戻しは新しく起動し直す（`<報告名>` を `PBI-<連番>.rework-<n>` にする）。前回の会話を続ける `opencode run -c`・`codex exec resume`・`dsh --session-id` は使わない。前回の文脈は報告ファイルと差し戻し文で渡す。

## opencode（haiku・sonnet の段の第一候補）

opencode CLI（`npm i -g opencode-ai`）で OpenCode Go のサブスクの `opencode-go/deepseek-v4.1-flash` を使う。DeepSeek は Go のワークスペースの Privacy 設定が Global でないと 400 エラーになる（Global にしておく）。

優先テンプレートは無料枠のモデル（OpenCode Zen。model ID が `opencode/` で始まる。例 `opencode/deepseek-v4.1-flash-free`）も route に入れられる。渡し方は Go のモデルと同じ。無料枠は提供元が入力を記録・利用することがあるので、sensitive の PBI には当たらない（優先テンプレートは sensitive に当たらない）。

**最初に opencode の場所を確かめる。** opencode を fnm や nvm で入れた Node のグローバルに入れていると、Claude Code のシェルの PATH には出ないことがある。`which opencode` で見つからなくても「入っていない」と判断しない。次のように探し、見つかったディレクトリ（`<OC_DIR>`）を起動のたびに PATH の先頭に足す。

```bash
# Windows で fnm を使っている例。見つかった opencode.cmd のディレクトリを <OC_DIR> にする
command -v opencode || ls -d "$(cygpath "$APPDATA")"/fnm/node-versions/*/installation/opencode.cmd 2>/dev/null
"<OC_DIR>/node_modules/opencode-ai/bin/opencode.exe" --version   # 動くか
```

それでも見つからなければ、使えないときとして `route` の次へ進み、STATUS.md に書く（インストールはユーザーに頼む）。

```bash
mkdir -p docs/pbi/opencode
PATH="<OC_DIR>:$PATH" OPENCODE_CONFIG="<このディレクトリ>/opencode.config.json" opencode run -m <route の model>[#<route の effort>] --format json   "docs/pbi/opencode/<報告名>.prompt.md を読み、その指示どおりに作業してください。"   < /dev/null > docs/pbi/opencode/<報告名>.jsonl 2> docs/pbi/opencode/<報告名>.err
```

- effort: route の effort が null でなければ `#<effort>` を付ける (例 `opencode-go/deepseek-v4.1-flash#max`)。null なら付けない。無い effort 名は `provider.no-route` の `Variant unavailable` で落ちる
- **同時に起動するときは 30 秒ずつずらす**: opencode はデータベース（SQLite）を共有するので、同時に起動すると `.err` に `database is locked` が出て、起動直後に落ちる（`.jsonl` は 0 行、作業ツリーは無変更）。2 件目以降は、コマンドの先頭に `sleep 30;`・`sleep 60;` … を付けて起動する。5 件までなら、これで落ちなかった
- **`database is locked` で落ちたら**: PBI の失敗とは数えない。下の「使えないとき」として 1 回だけ、単独で再実行する
- **権限**: `opencode.config.json`（このディレクトリ）を `OPENCODE_CONFIG` で渡す。編集とコマンドは許可し、`git push`・`git reset --hard`・`git clean`・`rm -rf`・`rm -r` とリポジトリ外への操作を拒否する。`--auto` は付けない。ユーザー全体の設定（`~/.config/opencode/`）は変えない（rtk のプラグインを入れていれば、それはそのまま使う）
- **拒否は成功扱いで返る**: 拒否されても終了コードは 0。`grep -c 'rule which prevents' <報告名>.jsonl` が 1 以上なら拒否がある（`"tool"` イベントの `"status":"error"`）。検証コマンドだけの拒否なら 8 章で自分で実行すれば足りる。作業が進んでいなければ `route` の次へ進む
- **使えないとき**（終了コードが 0 以外、`grep -m 3 '"type":"error"'` に当たる（利用上限・認証・リージョン・接続のエラー）、または `.last.md` が無いか空）: 1 回だけ再実行し、だめなら `route` の次へ進む。利用上限で落ちたときは STATUS.md の上に「opencode <model>: 上限到達（日時）」と書き、このセッションの残りの PBI では `--skip <model>`（例 `--skip opencode/deepseek-v4.1-flash-free`）を付けて route を出し直す。外れるのはそのモデルの手順だけで、同じ opencode の別のモデル（交互グループの相方や有料のモデル）は使い続ける。複数のモデルが上限なら、カンマでつなぐ。opencode そのものが動かない（認証・接続のエラー）ときは `--skip opencode` で opencode の手順を全部外す
- opencode は AGENTS.md を読むが CLAUDE.md は読まない。PBI に要る規約は PBI 本文に書く
- トークン数・cost・ステップ数・時間は、合否を決めたときの `record_run.py`（SKILL.md 8 章）が `.jsonl` の `step_finish` から読んで runs.jsonl に書く。手で足さない。無料枠（`opencode/…-free`）の cost は 0 で出る

## Codex（haiku・sonnet の段）

期限を付けたいときは、振り分けテンプレートの手順に until を書く。

```bash
mkdir -p docs/pbi/codex
codex exec -m gpt-6-astra -c model_reasoning_effort='"<route の effort>"' -s workspace-write   -o docs/pbi/codex/<報告名>.last.md - < docs/pbi/codex/<報告名>.prompt.md   > docs/pbi/codex/<報告名>.log 2>&1
```

- `-o` の `.last.md` には最後のメッセージ（worker.md の 4 行）が入る。トークン数は `record_run.py` が `<報告名>.log` の `tokens used` の合計から読む（内訳は取れないので null になる。12 章の統計は Codex を数えない）
- effort は route の値（haiku 相当 `medium`、sonnet 相当 `high`）。high より上（xhigh / max）は使わない
- **使えないとき**（終了コードが 0 以外で、利用上限・認証・接続のエラー、または `.last.md` が空）: 1 回だけ再実行し、だめなら `route` の次へ進む
- Codex は CLAUDE.md を読まず、リポジトリの AGENTS.md を読む。PBI に要る規約は PBI 本文に書く

## Antigravity（haiku・sonnet の段の Codex の次）

**2026-10-13 時点では、ヘッドレス（`-p`）でコマンドの実行が `RunCommand` の auto-denied になり、作業が進まない**（作者の環境で 2 回、`denied_actions: [{action: command}]`、`.last.md` なし）。1 件目で同じ拒否が出たら、STATUS.md の上に「Antigravity: ヘッドレスで command が拒否（日時）」と書き、このセッションの残りの PBI では `--skip agy` を付けて route を出し直す。許可ルール（`~/.gemini/antigravity-cli/settings.json`）を足せば使える見込みだが、リポジトリ外の設定なのでユーザーに確認してから行う。

Antigravity CLI（`agy`、Windows では `%LOCALAPPDATA%\agy\bin\agy.exe`）は Google AI のサブスク特典の Claude を使う。

```bash
mkdir -p docs/pbi/agy
agy -p "docs/pbi/agy/<報告名>.prompt.md を読み、その指示どおりに作業してください。"   --model <route の model> --mode accept-edits --output-format json --print-timeout 3600s   < /dev/null > docs/pbi/agy/<報告名>.json 2> docs/pbi/agy/<報告名>.err
```

- **拒否は成功扱いで返る**: 許可の無いコマンドは黙って拒否され、終了コード 0・status SUCCESS のまま。`grep -o '"denied_actions":\[[^]]*\]' <報告名>.json` と `tail -n 5 <報告名>.err` を見る。検証コマンドが拒否されただけなら 8 章で自分で実行すれば足りる。ファイルの編集や作業に要るコマンドが拒否されて作業が進んでいなければ、`route` の次（Agent ツール）へ進む
- **使えないとき**（終了コードが 0 以外、出力に利用上限・quota・rate limit・`RESOURCE_EXHAUSTED`・認証のエラー、または `.last.md` が無いか空）: 1 回だけ再実行し、だめなら `route` の次（Agent ツール）へ進む。利用上限で落ちたときは STATUS.md の上に「Antigravity: 上限到達（日時）」と書き、以後 `--skip agy`
- agy の会話には CLAUDE.md も AGENTS.md も効くとは限らない。PBI に要る規約は PBI 本文に書く
- トークン数は 12 章の統計に入らない。完了時の jev-log の行に「agy / モデル名」とだけ書く

## dsh（DeepSeek Harness の headless。優先テンプレートから使う）

DeepSeek Harness（`@deepseek-ai/dsh`）の headless で、DeepSeek 公式 API などのモデルを直接呼ぶ。route の model は `<provider>/<model>`（dsh の provider 名とモデル ID）で、例えば DeepSeek 公式の V4.1 Flash 1017 は `deepseek-official/deepseek-flash`。effort は dsh の reasoningEffort（DeepSeek 公式の Flash は `low` / `high` / `max` と、付けない。`medium` は無い）。provider と既定の設定は `DSH_HOME` の `cordis.patch.yml` に書く（DeepSeek 公式の `deepseek-official` は dsh に組み込まれている）。

```bash
mkdir -p docs/pbi/dsh
python <このディレクトリ>/dsh_run.py <報告名> --model <route の model> [--effort <route の effort>]
```

- `dsh_run.py` が、実行ごとの上書き設定 `docs/pbi/dsh/<報告名>.patch.yml`（provider・model・reasoningEffort）を書いて `--patch` で渡し、`--json` のイベントを `<報告名>.jsonl`、診断を `<報告名>.err`、final の本文を `<報告名>.final.md`、開始・終了の時刻と終了コードを `<報告名>.run.json` に書く。effort が null なら `--effort` を付けない
- **キー**: `dsh_run.py` が環境変数を優先し、無ければ `~/.config/deepseek/deepseek.key`（DeepSeek 公式）・`~/.config/opencode/opencode-go.key`（OpenCode Go）から読む。値は表示しない。route の provider のキーが無ければ終了コード 2 で止まる（使えないときとして次へ進み、ユーザーにキーの置き場を伝える）
- **場所**: 既定は PATH の `dsh`（`npm i -g @deepseek-ai/dsh`）。PATH に無ければ `PBI_DSH_BIN`（`@deepseek-ai/dsh/lib/bin.js`）と `PBI_DSH_NODE`（省略時は PATH の node）で渡す。provider の設定を置いた `DSH_HOME` もここで渡す。`dsh_run: dsh が見つかりません` などで止まったら、使えないときとして次へ進み、STATUS.md に書く
- **権限**: `DSH_PERMISSION_MODE=workspace-write`（書き込みはリポジトリの中だけ、コマンドは Windows では PowerShell のサンドボックス）。headless では昇格の確認に答える人がいないので、サンドボックスの外が要るコマンドは拒否される（`tool_result` の `status` が成功以外）。`npm run build` などが named pipe で落ちることがある。検証コマンドだけの失敗なら 8 章で自分で実行すれば足りる。作業が進んでいなければ `route` の次へ進む
- **同時に起動するときは 30 秒ずつずらす**（dsh はセッションの記録を DSH_HOME に書く。opencode と同じ扱いにしておく）
- **使えないとき**（終了コードが 0 以外、`.err` に `dsh: <code>:`（`MISSING_CREDENTIAL`・`TRANSPORT`・`INVALID_REQUEST`・利用上限など）が出る、または `.last.md` が無いか空）: 1 回だけ再実行し、だめなら `route` の次へ進む。利用上限・残高不足で落ちたときは STATUS.md の上に「dsh <model>: 上限到達（日時）」と書き、以後 `--skip <model>`（例 `--skip deepseek-official/deepseek-flash`）。dsh そのものが動かないときは `--skip dsh`
- 差し戻しは新しく起動し直す（`--session-id` で前の会話は続けない）
- dsh は AGENTS.md・CLAUDE.md を読むとは限らない。PBI に要る規約は PBI 本文に書く
- トークン数・ステップ数・時間は `record_run.py` が `.jsonl` の `status` イベントの usage と `.run.json` から読む。cost は出ない（null）。DeepSeek 公式の料金は DeepSeek のアカウントの従量課金
