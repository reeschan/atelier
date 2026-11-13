> **注記:** cloud container 2026-11-10 の記録。API キーと外部通信が無い環境での結果で、ユーザー PC の状態ではない。Jev やツールが使えるかは、その回の確認結果で判断すること。

Repository: <ローカルの clone の絶対パス (クラウドでは /home/user/atelier)>
Branch: claude/intelligent-ritchie-xmydhe
Run id: e242f76d-977b-459c-9352-c1a7460b67a6

## 今どこか
- 承認の前 (止まる場所 1) で止めた。承認はまだ。build は 1 件も始めていない。
- 終わった工程: understand / map / lint-survey / design / plan / route。仕様は docs/specs/atelier-v6.md (コミット済み)。
- 理由: クラウドのコンテナで始めてしまったため、ローカルに移す。

## 次にやること (resume の手順の 2 で「build の前の工程をやり直さない」の例外)
1. route を Jev でやり直す: `python <skill>/prepare.py docs/pbi`。クラウドでは Jev が使えず (API キーなし・api.typesafe.ai への通信が止められていた)、段は facts だけで決めた。
2. 上書きした段 (PBI-002・003・005・007 → sonnet、PBI-006 → opus) は、Jev の結果が低いときだけ、もう一度 `--only PBI-<n> --tier <tier>` で上書きする。理由は jev-log の override 行。
3. `docs/pbi/notes/approve.md` の PBI の表を新しい段にそろえ、`design_record.py markdown --draft docs/pbi/notes/approve.md` を流し直す (draft_hash が変わる)。正本 (design-record.json、atelier-v6 revision 1) は targets と concerns が変わらなければそのまま。
4. Approve を見せて待つ。ユーザーへの問い (ask) は 3 つ: 既定の起動 agent か headless、版の名前 v6、headless の permission_mode の既定 auto。
5. 承認のあとの波: W1 001,004,011 / W2 002,005 / W3 003,006 / W4 007,008,009,010 / W5 012,013。

## 上書きした判断
- 実行先は agent だけで記録した (クラウドに外部の CLI が無かった)。ローカルでは `setup` で選び直してよい。記録は home の agents.json なので、この写しには入っていない。
- 段の上書き 5 件 (上の 2)。

## 設計の判断で、まだ PBI に入っていないもの
- なし。仕様の Further Notes の 2 件 (入れ子のサブエージェントの会話記録の置き場所、`claude -p --agent` と permission_mode auto) は、最初の波で実物を確かめる。

## レビューで見つけた、あとの PBI への注意
- まだレビューは無い。lint の下調べの bug-hunt の候補 8 件はすべて範囲の外 (record_run.py・claude_run.py・verify.py)。承認の「見つけたが今回は入れないもの」に載せた。

## 受け入れの道具 (設計者が書いた。作業者は変えない)
- docs/pbi/tools/conductor_smoke.py (PBI-002)、conductor_run_smoke.py と fake_designer.py (PBI-003)。下書きは docs/pbi/notes/drafts/。

## その他
- docs/pbi/adapters.json がある (design の判断 run、聞いていない)。
- 共有メモリの MCP はつながっていなかった。引き継ぎのメモリは書いていない。
- STATUS.md の段の行と stage records は、そのまま使える。
