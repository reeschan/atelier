# 実行先ごとの渡し方（opencode / Codex / Antigravity）

route に出た実行先を初めて使う前に、その節だけを読む。依頼文（SKILL.md 6 章）は `docs/pbi/<実行先>/<報告名>.prompt.md` に書き、末尾に「要約の書き先: `docs/pbi/<実行先>/<報告名>.last.md`」を足す。リポジトリのルートで Bash から `run_in_background` で 1 件ずつ起動する。cd はリテラルの絶対パスで行う（変数で cd すると Claude Code の権限確認で止まる）。

外部の実行先は shared-memory の `get` を使えないものとして扱う。PBI の「参照」欄に ref があれば、設計者がその本文を `get` して、依頼文のファイルの末尾に「## 参照（作業空間の ref）」として id ごとに書き写す。依頼文には「参照欄の ref は下に写してある。memory は使わない」と書き足す（SKILL.md 0.8）。

報告は `.last.md`（4 行）だけを読む。`.jsonl`・`.log`・`.json` は丸ごと読まず、下に書いた項目を `grep -m` や `tail -n` で取り出す。

差し戻しは新しく起動し直す（`<報告名>` を `PBI-<連番>.rework-<n>` にする）。前回の会話を続ける `opencode run -c`・`codex exec resume` は使わない。前回の文脈は報告ファイルと差し戻し文で渡す。

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
- トークン数は 12 章の統計に入らない。`.jsonl` の `step_finish` の `tokens` と `cost` を小さなスクリプトで足し、合計の 1 行だけを出して、jev-log の完了時の行に「opencode / モデル名 / tokens / cost」と書く

## Codex（`select_model.py` の `CODEX_UNTIL` まで。haiku・sonnet の段）

```bash
mkdir -p docs/pbi/codex
codex exec -m gpt-6-astra -c model_reasoning_effort='"<route の effort>"' -s workspace-write   -o docs/pbi/codex/<報告名>.last.md - < docs/pbi/codex/<報告名>.prompt.md   > docs/pbi/codex/<報告名>.log 2>&1
```

- `-o` の `.last.md` には最後のメッセージ（worker.md の 4 行）が入る。トークン数は `grep -A1 'tokens used' <報告名>.log | tail -n 1` の 1 行だけを取り、jev-log の完了時の行に書く（12 章の統計は Codex を数えない）
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
