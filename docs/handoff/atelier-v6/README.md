# 引き継ぎ: atelier v6 (設計者を Atelier の中へ)

クラウドのセッションで Atelier 自身の流れを使って設計した run を、ローカルで続けるための引き継ぎ書。2026-11-10 時点。

## 何を頼まれ、どこまで進んだか

頼まれたこと:

- `/clear` → resume の繰り返しを無くす。文脈を冷ますのは Atelier の中で済ませる。
- 呼び出し側 (Claude・Codex・Grok など) はユーザーと話すだけにする。開発は Atelier が担う。設計者は Atelier が持ち、サブエージェントとして起動し、そのさらにサブエージェントとして作業者が動く。
- レビューは別のエージェントが行い、全部のレビューがそろったら設計者に渡す。テストは Sonnet 級のサブエージェントがスキルを呼んで流す。設計者の責務は、初回の PBI 設計、レビュー結果の照合、テスト結果の照合の 3 つにする。
- この設計と実装も Atelier を使って進める。

進んだところ:

- understand → map → lint の下調べ → design → plan → route まで終わり、**PBI 一覧の承認の前**で止めた。
- 仕様: [docs/specs/atelier-v6.md](../../specs/atelier-v6.md) (このブランチにコミット済み)。
- PBI 13 件、facts、brief、route.json、設計の正本 (design-record.json)、承認の下書き、受け入れ用のスクリプト: [pbi/](pbi/) (下の手順で `docs/pbi/` に戻す)。
- Atelier の形式の引き継ぎ: [pbi/HANDOFF.md](pbi/HANDOFF.md)。状態の板: [pbi/CONTEXT_STATE.md](pbi/CONTEXT_STATE.md)。

## 分かっていること

- Claude Code のサブエージェントは、既定で 3 層まで入れ子にできる (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`、https://code.claude.com/docs/en/sub-agents.md)。これで「呼び出し側 → 設計者 → 作業者・レビュー担当・テスト担当」がそのまま組める。
- `claude -p` は `~/.claude/agents` を読み、`--agent` でメインのスレッドの定義を選べる。`--permission-prompts none` は、許可を求める呼び出しを拒む。
- **Jev はクラウドのコンテナで使えなかった。** API キー (`TYPESAFE_API_KEY` か `~/.config/typesafe/credentials.json`) が無く、`api.typesafe.ai` への通信もネットワークの設定で止められていた。そのため段は facts だけで決め、5 件を設計者が上書きした。これはユーザー PC の状態ではない。使えるかは、その回の `prepare.py` と `tools_check.py` で判断する（[pbi/README.md](pbi/README.md)）。ローカルでは route をやり直す。

## ローカルで続ける手順

1. ブランチを取ってくる:

   ```bash
   git fetch origin claude/intelligent-ritchie-xmydhe
   ```

   ```bash
   git checkout claude/intelligent-ritchie-xmydhe
   ```

2. 状態を `docs/pbi/` に戻す (`docs/pbi/` は .gitignore の対象):

   ```bash
   mkdir -p docs/pbi && cp -R docs/handoff/atelier-v6/pbi/. docs/pbi/
   ```

3. `docs/pbi/HANDOFF.md` の `Repository:` の行を、ローカルの clone の絶対パスに書き換える。
4. Claude Code で `/atelier resume` を実行する。HANDOFF.md の「次にやること」に、Jev で route をやり直し、承認の下書きを作り直してから承認を見せる、と書いてある (今の v5 の流れでは、resume は build の前をやり直さないので、その例外として書いた)。
5. 承認で答える 3 つの問い:
   - Claude Code から呼ぶときの既定の起動: agent (推奨) / headless
   - 版の名前: v6 (推奨) / v5 の続き
   - headless の設計者の permission_mode の既定: auto (推奨) / acceptEdits と許可リスト / bypassPermissions

## 承認の中身 (要約)

| 波 | PBI |
|---|---|
| 1 | 001 role_results.py (JSON の検査と束)・004 エージェント定義 3 つ・011 `agent_stats --ctx` の拡張 |
| 2 | 002 conductor.py の中心・005 レビュー担当とテスト担当の依頼文 |
| 3 | 003 conductor.py run (headless)・006 designer.md と appendix/episodes.md |
| 4 | 007 SKILL.md を呼び出し側に・008 / 009 / 010 付録の書き換え |
| 5 | 012 README・013 CHANGELOG と setup |

- テストは最後にまとめる (tests-at-end)。テストの工程で `test_role_results.py`・`test_conductor.py`・`agent_stats` の `--ctx` のテストを足す。
- 範囲の外で見つけたもの (今回は入れない): `record_run.py` の main 127 行と例外の握りつぶし、`claude_run.py`・`verify.py` の起動失敗の扱い、`runs.jsonl` の追記の排他。

## 片づけ

v6 が終わってマージする前に、このフォルダ (`docs/handoff/atelier-v6/`) は消す。run の作業の状態なので、リポジトリには残さない。
