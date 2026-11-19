<p align="center"><img src="assets/atelier-banner.jpg" alt="Atelier: 光る大鍋をかき混ぜる少女がいるアトリエ" width="100%"></p>

# Atelier

[![Tests](https://github.com/reeschan/atelier/actions/workflows/test.yml/badge.svg)](https://github.com/reeschan/atelier/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
[![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-D97757?logo=claude&logoColor=white)](https://claude.com/claude-code)

[English](README.md) | 日本語

Atelier は、複数のファイルにまたがる実装のための Claude Code のスキルです。依頼を合意した仕様にし、小さな作業（PBI）に分け、PBI ごとにモデルを選んで作らせ、機械で確かめ、変更全体を 1 回だけ見直し、使ったトークンと費用を報告します。1 ファイルの小さな直しには使いません。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/architecture-dark.svg">
  <img src="assets/architecture-light.svg" alt="Atelier v8 のアーキテクチャ図。開発者は呼び出し側（自分の Claude Code のセッション）と話す。呼び出し側は、モデルを使わないスクリプトの進行役（conductor.py）に start・next・done・answer・run で問い合わせ、モデルの役（調べる役・詰める役・決める役・実装役・見直し役）をすべて深さ 1 で起動する。進行役は run の状態の遷移表を持ち、テンプレート 1 種類（難度 L1〜L5 から経路、役、上限）・守りの規則 G1〜G14・run の記録を使う。外部の実行先（Codex・Antigravity・opencode・dsh・claude -p）は進行役が自分で起動し、~/.config/atelier/agents.json からは実行先の登録だけを読む。役と進行役は対象リポジトリの docs/pbi に、run.json・事実の束・設計の木・PBI・record.jsonl・REPORT.md を書き、実装役はリポジトリの規約の文書を読んでからソースコードを変える。承認ページは answer-me-with-html で描き、調べる役はリポジトリの外のキャッシュから omm-pr-scan --cache で OMM の図を差分で更新する。使える走査がキャッシュに無いときは、ユーザーが選べば呼び出し側が omm-scan で全体を走査する。呼び出し側は、最後の見直しとしてリポジトリのレビューのスキルを 1 回流し、承認した外のデータをリポジトリの中へ写す。">
</picture>

図のソースは `assets/architecture.d2` です（arch-diagram スキル経由で [D2](https://d2lang.com) で描いています）。

## run の流れ

1. **調べる**：安いモデルが、リポジトリの事実の束を書きます。項目ごとに出典（`path:line` か OMM の要素）を付けます。事実の束には、実装の前に読むと指された規約の文書・検査のコマンド・リポジトリのレビューのスキルも入ります。OMM の図はリポジトリの外のキャッシュ（omm-pr-scan と同じ置き場所）から取り、一番近いコミットから差分で更新します。使える走査が無ければ、全体を走査して作るかを先に聞きます。
2. **詰める（飛ばせる）**：仕様がそろっていないときは、詰める役が番号つきの問いをおすすめの答えと一緒に出し、決まっていない問いがなくなるまで巡を重ねてから仕様を書きます。飛ばすかどうかはユーザーが決めます。
3. **決める**：決める役が、承認した仕様・事実の束・規約の文書から PBI を書きます。触れてよいファイルを決める前に、呼び出し元・網羅の表・同じ役割の既存の仕組みをコードで調べ、調べた場所を PBI に残します。PBI には、難度（L1〜L5）とその理由、検査のコマンド、外のデータが要ればその元と置き先、慎重に扱う必要があれば sensitive の候補と理由が付きます。計画には、リポジトリ全体の検査・E2E のコマンド・最後の見直しに使うスキルも付きます。
4. **承認**：1 枚のページに、役とモデル、PBI ごとの難度・理由・規則・経路、sensitive の候補、外部のモデルに送る PBI、上限と見積もり、決めることを載せます。sensitive とした部分は run から外し、引き継ぎメモを書きます。
5. **wave ごとに作る**：承認した外のデータがあれば、呼び出し側が先にリポジトリの中へ写します。依存の済んだ PBI を実装役が規約の文書を読んでから作ります。wave ごとの確かめは機械の検査（その wave の PBI の検査と計画の検査）だけで、合否は終了コードで決めます。実装役は検査を自分で流し、落ちたら 2 回まで直します。対象の外でも既にあるファイル（`do_not_touch` を除く）は変えてよく、仕様どおりの変更で古くなったテストの期待値もそうして直します。変えたファイルは理由つきで報告に載ります。wave の検査で落ちても止めず、持ち越しとして記録し、次の wave の頭に修正の PBI を入れます。待つ PBI はそのあとに作ります（最後の wave なら、修正の段で見直しの指摘と一緒に直します）。修正の PBI はテストのファイルを変えられません。同じ持ち越しの修正の PBI が 2 回（2 回目は 1 難度上）落ちたときだけ、同じ経路でもう 1 回・1 難度上の経路で・保留、から選んでもらいます。実装役が計画の前提が崩れたと返したら、計画を組み直す（合格済みの PBI はそのまま、承認をもう一度）・同じ PBI をやり直す・止める、から選んでもらいます。持ち越しが残ったまま終わった run は「未完了」と報告します。詳しくは [docs/specs/carry-over-failures.md](docs/specs/carry-over-failures.md)。
6. **最後に 1 回見直す**：リポジトリにレビューのスキルがあれば呼び出し側がそのスキルで、無ければ見直し役が規約の文書と照らして、変更全体を 1 回だけ見直します。指摘には重さと確かさが付き、確かさの高い指摘を決める役が「直す・見送る・聞く」に仕分けます。直す一覧の承認ページには、見送ったもの・確かさの低いもの・書き方だけのものも見送りの候補として並び、どれでも直す一覧に足せます。
7. **修正と E2E**：承認した直しを、ファイルの重ならないまとまりで実装役が直します。E2E は計画の E2E のコマンドで流し（無ければ飛ばして報告に書く）、落ちたらテンプレートの上限まで修正に戻し、超えたら聞きます。
8. **報告と引き継ぎ**：`docs/pbi/REPORT.md` に、PBI 別・役別・モデル別のトークンと費用、守りの規則ごとの数、難度とモデルの組ごとの初回の合格率を書きます。`docs/pbi/HANDOFF.md` には作業ルート・ブランチ・始めたときのコミット・目的・仕様・報告の場所を書き、長い run を持たない新しい会話で PR まで進められるようにします。新しい会話に移るかは、呼び出し側がユーザーに聞きます。

合意した状態の図と、その TLC の検査結果は [docs/specs/atelier-v8.md](docs/specs/atelier-v8.md) にあります。規約・検査のコマンド・組み直し・外のデータ・OMM を足した版は [docs/specs/fewer-review-loops.md](docs/specs/fewer-review-loops.md) です。

## 2 つの層

- **呼び出し側**：ユーザーのセッションです。進行役のコマンドを回し、言われた役を起動し、ページを見せて答えを返します。設計はしません。モデルの要らない手（検査・外部の実行先・承認した外のデータの写し）は `conductor.py drive` に裏で任せ、モデルの役・スキル・ユーザーの答えが要るときだけ戻ります。
- **進行役**（`conductor.py`）：モデルを使わないスクリプトです。run の状態を `docs/pbi/run.json` に持ち、次の 1 手を 1 つずつ返します。回数と予算を数え、外部の実行先を前後の検査つきで起動し、規則で決められないことはユーザーへの質問にします。

決める役を呼ぶのは、PBI を書くとき、承認で直してと言われたとき、計画の前提が崩れて組み直すとき、最後の見直しの指摘を仕分けるときの 4 か所だけです。

## テンプレートと難度

変えられることは、すべてテンプレート 1 つにまとまっています。

- `routes`：難度 L1〜L5 ごとに、試す実行先・モデル・effort の並び。隣り合う難度が同じモデルで effort だけ違ってもかまいません。
- `roles`：役ごとのモデル（調べる役・詰める役・決める役・見直し役。修正だけ別のモデルにすることもできます）。
- `limits`：E2E の修正の回数と、トークンか費用の予算（任意）。
- `parts` と `reading`：動かす部品と、モデルに読ませる規則の文・指示文の節。

難度は、熟練者なら何分かかるかの見当と、設計の判断が要るかで定義し（`skills/atelier/atelier/levels.json`）、モデル名は使いません。期限を付けて別のテンプレートに切り替えることもできます。同梱の既定は `skills/atelier/template.default.json` です。

## 守りの規則と記録

- [GUARDS.md](skills/atelier/GUARDS.md) に、14 の守りの規則（G1〜G14）と、それぞれの止め方（起動の前・進行役が拒否・終わったあとの検査・指示文。指示文は 2 つだけ）を載せています。
- `docs/pbi/record.jsonl` には、起動ごと（役・モデル・トークンまたは `unknown`・規則と指示文の節ごとに読ませたトークン）、検査ごと、直しごとに 1 行ずつ書きます。テンプレートで規則の文や節を読ませないようにでき、読ませた run と読ませなかった run を数で比べられます。

用語は [GLOSSARY.md](skills/atelier/GLOSSARY.md) にあります。

## 必要なもの

| 道具 | 必須 | 使いどころ |
|---|---|---|
| [Claude Code](https://claude.com/claude-code) | はい | 呼び出し側とモデルの役 |
| Python 3.10+ | はい | 進行役とそのモジュール（標準ライブラリだけ） |
| Node.js と answer-me-with-html スキル | 推奨 | 承認ページを描く |
| oh-my-mermaid と omm-pr-scan スキル（v1.2 以降） | いいえ | 調べる工程の OMM の図。なければ調べる役がコードを読む |
| [Codex CLI](https://github.com/openai/codex)、Antigravity CLI（`agy`）、[opencode](https://opencode.ai)、DeepSeek Harness（`dsh`）、`claude` | いいえ | テンプレートの経路に書いた外部の実行先 |

## インストール

```sh
git clone https://github.com/reeschan/atelier.git
cp -r atelier/skills/atelier ~/.claude/skills/
python ~/.claude/skills/atelier/setup.py runners
python ~/.claude/skills/atelier/setup.py models codex
```

`setup.py runners` は CLI を見つけ、パスと版を `~/.config/atelier/agents.json` に書きます（実行先の登録だけ）。`setup.py models <runner>` は、その実行先のモデルの一覧を `~/.config/atelier/models/` に置きます。別のフォルダを使うときは `--home <フォルダ>`、`PATH` にない CLI はたとえば `runners --codex <パス>` で指定します。

## 使い方

Claude Code で作業を頼み、`/atelier` を呼びます。呼び出し側が目的をファイルに書いて、次を流します。

```sh
python skills/atelier/conductor.py start --root <作業ルート> --goal-file <ファイル> [--spec <仕様のファイル>]
```

そのあとは、[SKILL.md](skills/atelier/SKILL.md) のとおりに `drive`・`answer`・`start` の返す次の 1 手に従います。`drive` はモデルの要らない手をすべて済ませ、呼び出し側の出番の手で止まります。`drive --done <launch id>` は、先にサブエージェントの起動を終わらせます。同じ wave で `targets` の重ならない PBI と、修正のグループは、一斉起動（`launches`）としてテンプレートの `limits.parallel`（既定 3）本まで同時に動きます。外部 runner の一斉起動は `drive` が同時に動かし、サブエージェントの一斉起動は、呼び出し側が 1 つのメッセージでまとめて起こし、`drive --done` 1 回で片付けます。1 手ずつ進める `next`・`done`・`run` も残しています。run のファイルは `<作業ルート>/docs/pbi/` にあります。

## テスト

```sh
cd skills/atelier
python run_tests.py
```

## ライセンス

[MIT](LICENSE)
