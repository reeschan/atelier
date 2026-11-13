# pbi/ は cloud container 2026-11-10 の記録

cloud container 2026-11-10 の記録。API キーと外部通信が無い環境での結果で、ユーザー PC の状態ではない。Jev やツールが使えるかは、その回の確認結果で判断すること。

STATUS.md、CONTEXT_STATE.md、HANDOFF.md の「Jev unavailable」「Tools missing」は、そのコンテナで `prepare.py` と道具の確認が失敗したときの記録である。ユーザー PC に資格情報があるか、Jev に届くかは、再開した回の `prepare.py`（`Jev: ok n/n` または `Jev: unavailable (<code>)`）と `tools_check.py` の `jev` 行で判断する。このフォルダや記憶は根拠にしない。

JSON（`jev/*.json`、`*.route.json`、`jev-log.jsonl` を含む）は、その実行の出力のまま残してある。
