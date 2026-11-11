---
lang: ja
template: doc
title: atelier v6 の PBI 一覧の承認
reply-meta-design_id: atelier-v6
reply-meta-revision: 1
reply-meta-record_hash: 0c2961236e3c37a842cb70d7f2f48b58fea282f205d0aa605f747a3dead6f66f
reply-meta-draft_hash: 94c31c20c831f8fbc6c893f8e31f98f927eecb14c41bc106892f4bef3318c880
---
## 目的
呼び出し側はユーザーと話すだけにし、Atelier が持つ設計者を出番ごとに新しく起動する。レビューとテストは別の担当が JSON で返し、設計者は照合だけをする。/clear と resume を手で打たなくてよくする。仕様: docs/specs/atelier-v6.md

## PBI (13 件・5 波)
| PBI | 内容 | 段 / route の先頭 | 依存 |
|---|---|---|---|
| 001 | role_results.py (JSON 3 種の検査とレビューの束) | sonnet / agent sonnet high | - |
| 002 | conductor.py の start・next・done・answer・status | sonnet / agent sonnet high (上書き) | 001 |
| 003 | conductor.py run (claude -p で設計者) | sonnet / agent sonnet high (上書き) | 002 |
| 004 | エージェント定義 3 つ (設計者・レビュー担当・テスト担当) | haiku / agent haiku max | - |
| 005 | レビュー担当・テスト担当の依頼文 | sonnet / agent sonnet high (上書き) | 001, 004 |
| 006 | designer.md と appendix/episodes.md | opus / agent opus (上書き) | 002, 005 |
| 007 | SKILL.md を呼び出し側に、CONTEXT.md | sonnet / agent sonnet high (上書き) | 006 |
| 008 | 付録 handoff・kiln・verify | haiku / agent haiku max | 006 |
| 009 | 付録 tests-at-end・review | haiku / agent haiku max | 006 |
| 010 | 付録 adapters・lint・optional の問い | haiku / agent haiku max | 006 |
| 011 | agent_stats.py --ctx --agent / --session | haiku / agent haiku max | - |
| 012 | README と README.ja | haiku / agent haiku max | 007 |
| 013 | CHANGELOG と setup の付録 | haiku / agent haiku max | 007 |

## 工程
- understand・map (代わりの依頼文)・lint-survey (代わりの依頼文)・design (明らかに複雑)・plan・route (Jev なし、facts だけ。5 件を上書き)
- tests at end: on (deferred: unittest discover)。テストは最後のテストの工程で足す (test_role_results.py・test_conductor.py・agent_stats の --ctx)
- review: code-review (スキルが使えれば) か built-in、security: 対象外 (sensitive・deep なし)

## 見つけたが今回は入れないもの (lint の下調べ)
- record_run.py の main が 127 行、run.json・jsonl の壊れを黙って捨てる、runs.jsonl の追記に排他が無い
- claude_run.py・verify.py が起動の失敗 (OSError) を捕らえない

## 入っていない推奨の道具
omm・jevlint・sac-strategic-compact・pstack の test principle・answer-me-with-html (どれも代わりで進める)

```ask
Claude Code から呼ぶときの既定の起動は
* agent: 呼び出し側が設計者をサブエージェントで起動する (仕様の既定) | 入れ子 3 層まで
- headless: conductor.py run が claude -p で起動し、呼び出し側は待つだけ | 裏で回す
```

```ask
この変更の版の名前は
* v6 (今の design-record までを v5 とする) | 仕様のファイル名 atelier-v6.md
- v5 の続き (v5.1 など) | 
```

```ask
headless の設計者の permission_mode の既定は
* auto | 分類器が判断。使えない環境では agents.json で変える
- acceptEdits と許可リスト | Bash の多くが拒まれる
- bypassPermissions | コンテナ・VM だけで安全
```
