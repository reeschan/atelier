# 付録: テンプレートと適用の手順

テンプレート・適用を扱うとき (開始時の同期、チャットでの適用・作成、期限切れの確認) に読む。編集画面から扱うときは `appendix/setup.md` の `## templates` を読む。用語は CONTEXT.md、仕様は docs/specs/priority-templates.md に従う。

## 1. 用語と置き場所

- **テンプレート**: 名前を付けた route の上書き。段 (haiku / sonnet / opus) ごとに手順の配列を持つ。上書きを持たない段には当たらない。
- **適用**: テンプレートを期限付きでオンにしたもの。同時に 1 つだけ存在できる。フィールドは id・template・until (null は期限なし)・on_expire (ask / drop)・reason・started_at・started_via。
- **期限切れの扱い**: `ask` は自動では当てず、ユーザーに「延長 / やめる / 期限なし」を聞く。`drop` は当てない。適用の状態は none / active / expired_ask / expired_drop の 4 つ。
- **正本はローカルの `<home>/overrides.json`**。Web ページ (claude.ai の Artifact) は任意の同期先で、`<home>/web.json` に URL があるときだけ同期する。チャットでの適用・延長・停止と期限切れの確認は、ローカルだけで完結する。
- **`<home>/overrides.json`**: 正本。`{"synced_at": ISO|null, "updated_at": ISO|null, "application": <適用>|null}`。updated_at はローカルで適用を変えた時刻。無い・壊れているときは適用なしとして扱う (エラーにしない)。
- **`<home>/history.jsonl`**: 終わった適用を 1 行 1 件で追記する (`dict(application, ended_at=..., ended_how=...)`)。ended_how は replaced / stopped / declined / expired。
- **`<home>/web.json`**: 任意の同期先の URL。`{"url": "https://claude.ai/..."}`。
- **home**: 場所と、置くファイル (agents.json・models/<runner>.json・routing.json・templates/<name>.json・overrides.json・history.jsonl・web.json・prices.json) の表は `appendix/setup.md` の `## home` を見る。リポジトリにコミットしない (個人の設定)。
- **`<home>/templates/<name>.json`**: テンプレート 1 つ (UTF-8)。name は `^[a-z0-9][a-z0-9-]{0,62}$` に合い、ファイル名 (拡張子を除く) と一致すること。description は文字列 (空でもよい)。tiers は空でないオブジェクト。各手順は runner (opencode / codex / agy / dsh / agent)・model (空でない文字列)・effort (文字列か null) を持つ。手順の代わりに交互グループを置ける。
- **交互グループ**: 段の手順の列の 1 要素として書く `{"rotate": [手順, 手順, ...]}`。メンバーは通常の手順で 2 つ以上、agent と入れ子は不可、`rotate` 以外のキーは置かない。PBI ごとに先頭のメンバーが入れ替わり、残りのメンバーはその PBI の控えとして後ろに並ぶ。複数の実行先に PBI を等分に配るために使う。
- **回す数**: 交互グループの先頭を決める数で、PBI の番号 (PBI-007 なら 7)。先頭は `回す数 mod メンバー数` 番目 (0 始まり) で、元の並びを回した順に続く。select_model.py は facts のファイル名から取り、単体の判定では `--pbi N` で上書きできる。取れなければ 0。差し戻し・昇格で出し直しても同じ PBI は同じ並びになる。
- 例: `[{"rotate": [A の無料枠, B の無料枠]}, {"rotate": [A の有料, B の有料]}]` は、偶数の PBI で A の無料枠 → B の無料枠 → A の有料 → B の有料、奇数の PBI で B の無料枠 → A の無料枠 → B の有料 → A の有料 の順に試し、最後に routing のその段の agent が付く。route.json の route は展開済みの平らな列。展開してから `--skip` (runner 名かモデル ID) と until で絞る。
- 時刻はすべて ISO 8601 のタイムゾーン付き文字列 (例 `2026-10-22T23:59:00+09:00`)。タイムゾーンの無い文字列は不正。

## 2. Web ページ (任意の同期先)

ローカルだけで使うときは、この節を飛ばす。

- 使うときは、利用者本人の非公開の Artifact として発行する。ソースは `<このディレクトリ>/web/priority-templates.html`。個人のデータはページに埋め込まない (データはすべてデータベースから読む)。
- `capabilities: {db: {}, user: {}}` を付けて発行する。
- 発行した URL を `<home>/web.json` に書く。
- `python <このディレクトリ>/templates.py push` の writes を出し、ArtifactData の batch で書く。テンプレートの閲覧用の写しが `templates/<name>` に入る。
- Web からはテンプレートを作れない・書き換えられない。作成・書き換えはチャット (「テンプレートを作る・書き換える」) か編集画面 (`appendix/setup.md` の `## templates`) で行う。

## 3. 開始時の同期

分解の前に 1 回だけ行う。同期するかどうかは `<home>/web.json` で決める。

1. `<home>/web.json` を読む。
2. URL が無いときは同期しない。`python <このディレクトリ>/templates.py status` を実行し、ローカルの状態 (`{"status", "application", "writes"}`) を読む。status が `expired_drop` なら、このコマンドが history.jsonl への追記と overrides.json の後始末を行う。承認一覧には「ローカルの写しを使用」と書く。
3. URL があるときは、ArtifactData の get で `state/current` を読む。get できないときは 2 と同じくローカルだけで進める。
4. data (`{"application": ..., "updated_at": ...}`) を `docs/pbi/state.json` に書く。ファイルが無い・`{}` なら適用なし。
5. `python <このディレクトリ>/templates.py sync --state docs/pbi/state.json` を実行する。Web の updated_at とローカルの updated_at を比べ、新しい方を採って overrides.json を書き、`{"status", "application", "writes"}` を出す。
6. `writes` があれば、読んだ version を `if_version` に足して ArtifactData の batch で書く。既にあるドキュメントへの書き込みは `if_version` が必須。新しく作るドキュメントには付けない。
7. status が `unknown_template` のときは、テンプレートがローカルに無い・不正。写しには application をそのまま残している。テンプレートを直すか、ユーザーに確認する。

その作業の終わりまでは開始時の判定を使う。途中で期限が来ても読み直さない。

## 4. チャットで適用を始める・延長する・止める

まずローカルに書く。Web ページを使っているときだけ、出力の writes を ArtifactData の batch で書く。`--state` を渡すとデータベースの写しから writes を出すだけで、ローカルは変わらない。チャットからの適用では `--state` を省く。

**始める**
1. テンプレート名・期限 (ISO 8601、または期限なし)・期限切れの扱い (ask / drop)・理由を決める。
2. `python <このディレクトリ>/templates.py apply --template NAME --until ISO|none --on-expire ask|drop --reason TEXT --via chat` を実行する。
3. 出力は新しい `application` と `writes`。前の適用があるときは history.jsonl に 1 行追記され、writes の先頭に history への set が入っている (終わり方は通常 replaced、期限切れの drop なら expired)。
4. `<home>/web.json` に URL があるときだけ、`writes` に、読んだ version の `if_version` を足して ArtifactData の batch で書く。URL が無ければ何も書かない。
5. URL があるときだけ、`state/current` を get し直してファイルに書き、`templates.py sync --state` をもう一度実行して写しを合わせる。無ければ `templates.py status` でローカルの状態を確かめる。

**延長する・期限なしにする**
- 延長: `python <このディレクトリ>/templates.py answer --choice extend --until ISO`。until は now より後。
- 期限なし: `--choice forever` (until が null になる)。
- 出力の `application` と `writes` を始めるときと同じように書く (Web ページがあるときだけ writes を書く)。ローカルの状態は status で確かめる。

**止める**
- `python <このディレクトリ>/templates.py answer --choice stop`。
- history.jsonl への追記と、state/current を null にする set の writes が出る (active なら stopped、expired_ask なら declined)。Web ページがあるときだけ書く。

## 5. 期限切れの確認

- 承認の前に、状態が `expired_ask` なら、承認と一緒に「延長 (新しい期限を聞く) / やめる / 期限なし」を聞く。新しい止まる場面は増やさない。
- 答えは `answer` で反映する。延長は `extend`、やめるは `stop` (ended_how は declined)、期限なしは `forever`。`--state` は省く。
- Web ページがあるときだけ、`writes` を batch で書き、sync し直す。無いときは `templates.py status` で確かめる。
- `prepare.py` を `--only` なしでもう一度実行して担当を判定し直す。
- `expired_drop` は自動では当てない。後始末は開始時の同期 (Web ページがあれば `sync --state`、無ければ `status`) の writes が行う (ended_how は expired、ended_at は until)。

## 6. テンプレートを作る・書き換える

チャットか編集画面 (`appendix/setup.md` の `## templates`) で行う。

1. `<home>/templates/<name>.json` に JSON を書く (Write ツール。name はファイル名と一致させる)。編集画面では「優先テンプレートと適用」タブで同じファイルに保存する。
2. `python <このディレクトリ>/templates.py check [NAME]` で形を検査する。1 件 1 行で `ok <name>` / `ng <name>: <問題>`。1 件でも ng なら保存しない。
3. 実行先でモデル名・effort 名が実在するか確かめる (交互グループのメンバーも 1 つずつ)。opencode なら `opencode run -m <model>#<effort> "Reply with OK"` が `provider.no-route` (`Variant unavailable`) で落ちないこと。effort が null なら `#` を付けない。dsh なら、依頼文に「Reply with OK」だけを書いて `dsh_run.py` を動かし、終了コード 0 で `.final.md` に OK が出ること (effort 名が無いと `.err` に出る)。
4. Web ページにも置くときだけ、`python <このディレクトリ>/templates.py push --existing NAME,...` を実行する。正しいテンプレートの `templates/<name>` への set と、ローカルに無い名前の delete の writes が出る。`--existing` には Web にある名前 (ArtifactData で読む) を渡す。
5. その `writes` を ArtifactData の batch で書く。Web は読むだけ。

当てるのは決定表のあとで、振り分けテンプレート (routing.json) より先。sensitive の PBI には当たらず、route の最後は routing.json のその段の agent。手順には routing.json と同じく until (日付) を書け、過ぎた手順は外れる。上書きを持たない段にも当たらない。

## 7. 毎朝の通知タスク

- 毎朝 9:00 に 1 回動くスケジュールタスクを 1 つ作る。
- タスクの指示文の雛形: `<home>/overrides.json` を読み、`application` の `until` が 24 時間以内なら通知する (テンプレート名・期限・残り時間・理由)。`until` が null・無い・過ぎているときは何もしないで終える。
- 窓は既定の 24 時間。同じ期限の通知は 1 回で足りる。
- 読むだけで、書き換えない。
