# 付録: 振り分けテンプレートを画面で決める

引数が `templates` のときに読む。PBI の作業は始めない。

1. モデル一覧が古ければ更新する。`<このディレクトリ>/studio/catalog.json` の `updated` が 14 日より前なら、次を実行する (ネットワークが無ければ飛ばし、仮の値のままだとユーザーに伝える)。

   ```bash
   python <このディレクトリ>/studio/update_catalog.py | tail -n 3
   ```

   出力に「新しい候補」が 1 件以上あれば、`catalog.json` の `candidates` を読み、どの実行先で呼べそうかをユーザーに一言で伝える (`models` には勝手に足さない)。

2. 画面を開く。リポジトリのルートで、Bash の `run_in_background` で起動する。

   ```bash
   python <このディレクトリ>/studio/serve.py --target "<リポジトリのルートの絶対パス>"
   ```

   出力の `OPEN <URL>` をユーザーに伝え、「モデルを選び、案を選んで並びを整え、『このリポジトリに適用』を押してください」と頼む。ブラウザは自動で開く。待つ間は何もしない (起動したコマンドが終わると知らせが来る)。

3. 終わったら出力の最後の行を見る。
   - `APPLIED <パス>`: `docs/pbi/routing.json` が書かれた。前のものは `routing.prev.json` に残っている。`docs/pbi/PBI-*.facts.json` があれば `python <このディレクトリ>/select_model.py --all docs/pbi` を実行して、新しい表で担当がどう変わったかを 1 PBI 1 行で伝える
   - `TIMEOUT`: 30 分のあいだ適用されなかった。何も書いていない。もう一度開くか聞く

`docs/pbi/` は対象リポジトリの作業記録なので、routing.json もそこに置く。全リポジトリで同じ表を使いたいときは、ユーザーに確認してから、このディレクトリの `routing.json` にコピーする。
