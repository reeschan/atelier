# 状態の図の検査（fewer-review-loops）

`../../atelier-v8-notes/statechart/v8.mmd` に次を足した図（`v8-fewer.mmd`）を pstack の `/statechart` from-chart で検査した。

- 本筋（Run）: `O0` OMM を確かめている・`O1` OMM を作るかを聞いている・`O2` OMM を作っている・`D0` データを用意している・`B0` 前提が崩れたと聞いている。きっかけ `M1`〜`M5`、`L9b`（承認・外のデータは要らない）、`L40`（置き先にそろった）、`L41`（計画を組み直す）、`L42`（前提が崩れた）、`L43`（同じ PBI をやり直す）、`L44`（止める）、`L45`（E2E のコマンドが無い）。
- 計画（Plan）: 組み直しで承認が取り消される（`S24 --> S23: L41`）。
- データ（Data）: `X0` まだそろっていない・`X1` そろった・`X2` 要らない。組み直しで `X0` に戻る。
- 規則: `DataNotReady`（`S6 & X0`）、`ReplanWithoutApproval`（`S6 & S23`）。

ほかの状態とラベルの意味は v8 の `map.json` と同じ。結果は `run.txt`。
