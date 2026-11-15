# 重い見直しは実装のあとに差分を調べて聞く

決定: 2026-11-15、持ち主の指示。「interrogate を実行する際は必ず本当に深い実装をしたのかを調べたうえで answer スキルにて html を提示したうえでユーザーに強制確認させる。でないとトークンの消費が半端ないため」。確かめる時点は実装のあと、最後の見直しの直前。ふだんの最後の見直しは lens-review。永久の決まりとして Atelier の実装も変える。

## Problem Statement

P-103 の run (Atelier v7.11) で、ホームのテンプレート `~/.config/atelier/workflows/jev-routed.json` の interrogate が古い `when: deep` のままだった。`deep` は「どれかの PBI の review が deep なら run」で、PBI の見込みから決まり、ユーザーに聞かない。そのため重くない作業で interrogate (敵役のサブエージェントを並べ、変更を実際に動かして確かめる見直し) が確認なしに動き、トークンを大量に使った。

v7.12 の `heavy-ask` は承認のときに聞く形だったが、聞く材料は実装の前の計画で、実際の差分ではなかった。また、テンプレートに `deep` や `always` を書けば確認を飛ばして動く道が残っていた。

## Solution

1. 聞く時点を、実装を終えたあと、最後の見直しの直前に移す。聞く材料は実際の差分。
2. 差分を測って基準に当てるのはスクリプト (`heavy_check.py check`) で、深いときだけ問いのページの原稿を書く。設計者は名指しされたファイルを読んで確かめ、深くなければ理由を付けて聞かずに飛ばせる。深ければ、そのページ (kind decision、`present.py` が answer-me-with-any で HTML にする) で聞く。
3. 答えは `heavy_check.py record` が `docs/pbi/adapters.json` の `heavy-review` に残す。`run` はユーザーの答えで、しかも確認のページを見せたあと (`presentations.jsonl` の shown の行) でなければ書けない。答えが無ければ動かさず、次の候補 (推奨の見本では lens-review) が見直す。
4. 確認を飛ばして重い見直しが動く道を `flow.py` の検証で塞ぐ。

## 新しい流れ

```
すべての PBI が通る → テストの工程 → lint-recheck → 最後の見直し
  1. 全体の確かめ (tester の final)
  2. 関所 (adapter_gate.py): review が skip / off なら、重い見直しも動かない
  3. 重い見直しの確かめ (heavy-ask の行があるとき)
       heavy_check.py check
         recorded …   → 記録のとおり (resume・2 回目の最後の見直し)
         verdict none → heavy-ask の行が無い
         verdict light → record --decision skip --by auto、聞かない
         verdict heavy → 設計者が名指しのファイルで確かめる
             深くない → record --decision skip --by auto --reason …、聞かない
             深い     → 問いのページ (kind decision) で出番を終える → present.py が HTML で見せる
                         動かす → record --decision run --by user → 重い見直しが最後の見直し
                         ふつうの見直し・答え無し → record --decision skip --by user → 次の候補
  4. 見直しの選び方 (Pick the review) → 見直し → 指摘の照合
```

## 判定の基準 (`heavy_check.py`)

マージ基点 (`git merge-base <base> HEAD`) と作業ツリーを `git diff -M` で比べる。新しいファイルは関所の手順の `git add -N` で差分に出る。`docs/pbi/` と `.claude/worktrees/` の下は数えない。

| 基準 | 当たるとき |
|---|---|
| new-app (新しいアプリケーションを一から作った) | マニフェスト (package.json・pyproject.toml・go.mod・Cargo.toml など) を、マージ基点でマニフェストの無かったフォルダに足し、そのフォルダの下に 300 行以上足した |
| new-subsystem (自前のデータモデルか外の入出力を持つ新しい仕組みを足した) | コードのモジュール (フォルダの先頭 2 階層。文書と試験は数えない) を 3 個以上変え、そのうち新しいモジュールが 1 個以上あり、データモデル (移行・スキーマ・models など) か外の入出力 (api・routes・*.proto・openapi など) のファイルを新しく足した |
| new-architecture (構成を置き換えた) | 配置先のファイル (Dockerfile・*.tf・fly.toml など) を足して 500 行以上変えた、または、マージ基点からあるマニフェストの依存を入れ替え (足して消した)、300 行以上消した |

- 1 つでも当たれば heavy。差分が大きいだけ、sensitive な PBI、PBI の review が deep は、どれも基準ではない。
- 線はスクリプトの定数 (`NEW_APP_LINES` など)。見落としは安い側 (ふつうの見直し) に倒れ、誤って heavy と出ても聞くだけで動かない。
- スクリプトは機械の判定で、設計者が名指しのファイルを読んで確かめる (fixtures や vendored のフォルダ、生成されたファイルは新しいアプリケーションではない)。設計者が深くないと見たときは `--reason` が要る。設計者が heavy に上げる道は無い (light を聞かずに飛ばすのは費用を守る側)。

## 問いのページの中身

`heavy_check.py check` が heavy のとき、`docs/pbi/notes/heavy-review.question.md` に書く (`--draft` で場所、`--lang ja|en` で言葉を選ぶ)。`appendix/questions.md` の decision の形で、`design_record.py lint --kind decision` に警告なしで通る。

- 結論: どの基準に当たったか、重い見直しのレビュアーと費用の見込み、ふつうの見直しの費用、答えが無ければ動かないこと。
- 今の流れ・答えたあとの流れ: 最後の見直しの前で止まったところと、動かす / ふつうの見直しの 2 本の分かれ道。
- 決めること: 動かす (推奨。ここまで来たのは確かめて深い差分だけ) / ふつうの見直し の 2 択。
- 判断の材料: 3 つの基準ごとの結果と理由、差分の数字 (変えたファイル・新しいファイル・消したファイル・足した行・消した行・モジュール・新しいモジュール)、起こすレビュアーごとのモデル・入力と出力の見込み・費用の見込み、ふつうの見直しの見込み。

費用の見込みは、変えたファイルの今の中身と差分を chars-est/1 で数えた C から、1 人あたり 入力 = base_in + reads × C、出力 = out で出す (`ESTIMATE`。敵役 1 人は 50,000 + 3C と 20,000、main の行の取りまとめは 20,000 + C と 5,000、ふつうの見直しは 30,000 + C と 8,000)。キャッシュを数えない高めの目安で、請求額ではない。単価は `prices.json` (home が勝つ) のキーのうち、モデルの系統 (opus・sonnet・haiku・fable) を含むもの。候補の `models` の `inherit` と、`models` の無い候補は `--designer-model`、ふつうの見直しは `--usual-model` (Roles の reviewer.deep の系統)。

## adapters.json への記録

```json
{"heavy-review": {"heavy": true, "verdict": "heavy", "criteria": ["new-app"], "reason": "new-app",
                  "decision": "run", "asked": true, "by": "user", "diff_sha256": "…",
                  "checked_at": "…", "at": "…"}}
```

- `check` は記録があれば `recorded …` を出して測り直さない。resume でも、2 回目の最後の見直しでも聞き直さない。
- `record` は `heavy.json` (`docs/pbi/verify/heavy.json`、schema `atelier.heavy-check/1`) が無ければ終了コード 2。断るとき (終了コード 3): `run` を `--by auto` で書く、確認のページを見せる前 (heavy.json の `checked_at` より前の shown しか無い) に `run` を書く、heavy の差分を `--reason` なしで `--by auto` の skip にする。
- `stage.decided` のイベントを出す (by user / auto、reason approved / declined / when-false)。

## 確認を飛ばす道の扱い (決めたこと)

| 道 | 扱い | 理由 |
|---|---|---|
| review の工程の `when: deep` | `flow.py` の検証の誤り。メッセージで heavy-ask か adapter-gate を案内する | P-103 の原因。PBI の見込みは差分の深さを表さない。誤りにすれば古いテンプレートの run は始まる前に止まり、黙って動くことがない |
| main で動く見直しの `always` などの heavy-ask 以外 | 検証の誤り (builtin を除く) | main の見直しは、自分でレビュアーを起こす重い見直しの形 (`models` も main にしか書けない)。部品の定義に「重い」の印を足すより、すでにある形で見分けられる |
| サブエージェントで動く見直し部品の `always` | 残す | lens-review や code-review などのふつうの見直しの形。重い見直しはサブエージェントの中では敵役を起こせない |
| 関所の `deep` (振り分けのあと) | 残す | PBI ごとの関所 (architect など) の条件で、最後の見直しとは別 |
| `flow.py --decide` の heavy-ask の行 | 記録があればその run / skip、無ければ orchestrator | 設計者が自分の判断で run にしない |

部品の定義に `heavy: true` のような印を足すことも考えたが、今の home の部品 (component.py add が書いたもの) が「違う設定」になり、`example use` が断るようになる。main の形で見分ければ、今の設定のまま守れるので、印は足さない。

## Testing Decisions

- `test_heavy_check.py`: 一時ディレクトリの git リポジトリで CLI を動かす。基準ごとの境目 (new-app の 299 / 300 行、マニフェストのあったフォルダ、モジュールの数と新しいモジュールとデータモデル、配置先の行数と依存の入れ替え)、文書と試験と `docs/pbi/` を数えないこと、原稿が lint に通ること (ja・en)、費用の式、record の断りと記録、記録のあとは聞き直さないこと。home・プラグイン・ハーネスのログは切り離す。
- `test_flow.py`: review の工程の deep と、main で heavy-ask でない見直しが誤りになること、関所の deep は使えること、`--decide` が記録を読むこと。
- `test_component.py`: main の interrogate を `--when always` で登録し直すと断ること。

## Out of Scope

- 手元のスキル (`~/.claude/skills/atelier/versions/`) とホームの設定 (`~/.config/atelier`) の書き換え。版の作成と切り替えはマージのあと。
- `harness.schema.json` の問いの topic の追加 (今は other)。分析側の契約を変えるため、別の作業にする。
- 基準の線を home で変える設定。
