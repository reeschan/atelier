# pbi/ は cloud container 2026-11-10 の記録

cloud container 2026-11-10 の記録。API キーと外部通信が無い環境での結果で、ユーザー PC の状態ではない。Jev やツールが使えるかは、その回の確認結果で判断すること。

STATUS.md、CONTEXT_STATE.md、HANDOFF.md の「Jev unavailable」「Tools missing」は、そのコンテナで `prepare.py` と道具の確認が失敗したときの記録である。このフォルダや記憶は根拠にしない。

資格情報があるかは、その回の `tools_check.py` の `jev` 行で判断する。既定はキーの有無と読み込み元だけで、通信しない。Jev に届くかは `tools_check.py --probe` か、その回に Jev を呼び直した `prepare.py` の `Jev: ok n/n (<model>)` または `Jev: unavailable (<code>)` で判断する。応答ファイルが既にあると `prepare.py` は呼び直さず、`Jev: not-checked (cached ...)` と出す。呼び直すときは `prepare.py <dir> --only PBI-001,PBI-002` のように PBI を指定し、`--tier` は付けない（付けると答えを取り直さない）。

JSON（`jev/*.json`、`*.route.json`、`jev-log.jsonl` を含む）は、その実行の出力のまま残してある。
