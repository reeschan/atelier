# 付録: テンプレートと適用の手順

テンプレート・適用を扱うとき (開始時の同期、チャットでの適用・作成、期限切れの確認、Web ページ) に読む。用語は CONTEXT.md、仕様は docs/specs/priority-templates.md に従う。

## 1. 用語と置き場所

- **テンプレート**: 名前を付けた route の上書き。段 (haiku / sonnet / opus) ごとに手順の配列を持つ。上書きを持たない段には当たらない。
- **適用**: テンプレートを期限付きでオンにしたもの。同時に 1 つだけ存在できる。フィールドは id・template・until (null は期限なし)・on_expire (ask / drop)・reason・started_at・started_via。
- **期限切れの扱い**: `ask` は自動では当てず、ユーザーに「延長 / やめる / 期限なし」を聞く。`drop` は当てない。適用の状態は none / active / expired_ask / expired_drop の 4 つ。
- **正本は Web ページのデータベース**。コレクションは `state/current`・`templates/<name>`・`history/<id>`。ローカルには写し (`overrides.json`) を置き、手で書き換えない。
- **home**: `templates/` ディレクトリ・`overrides.json`・`web.json` を置くディレクトリ。解決順は (1) コマンドの `--home DIR` (2) 環境変数 `PBI_TEMPLATES_HOME` (3) スクリプトのディレクトリの親の名前が `versions` ならその親 (版管理のローカル配置。スキルのルート) (4) それ以外はスクリプトのディレクトリ。リポジトリにコミットしない (個人の設定)。
- **`<home>/templates/<name>.json`**: テンプレート 1 つ (UTF-8)。name は `^[a-z0-9][a-z0-9-]{0,62}$` に合い、ファイル名 (拡張子を除く) と一致すること。description は文字列 (空でもよい)。tiers は空でないオブジェクト。各手順は runner (opencode / codex / agy / agent)・model (空でない文字列)・effort (文字列か null) を持つ。
- **`<home>/overrides.json`**: 写し。`{"synced_at": "<ISO>", "application": <適用 または null>}`。無い・壊れているときは適用なしとして扱う (エラーにしない)。
- **`<home>/web.json`**: Web ページの URL。`{"url": "https://claude.ai/..."}`。
- 時刻はすべて ISO 8601 のタイムゾーン付き文字列 (例 `2026-10-22T23:59:00+09:00`)。タイムゾーンの無い文字列は不正。

## 2. Web ページの初回の発行

- 利用者本人の非公開の Artifact として発行する。ソースは `<このディレクトリ>/web/priority-templates.html`。個人のデータはページに埋め込まない (データはすべてデータベースから読む)。
- `capabilities: {db: {}, user: {}}` を付けて発行する。
- 発行した URL を `<home>/web.json` に書く。
- `python <このディレクトリ>/templates.py push` の writes を出し、ArtifactData の batch で書く。テンプレートの閲覧用の写しが `templates/<name>` に入る。
- Web からはテンプレートを作れない・書き換えられない。作成・書き換えはチャットで行う (「6. テンプレートを作る・書き換える」)。

## 3. 開始時の同期

分解の前に 1 回だけ行う。

1. `<home>/web.json` を読む。無い・url が無いときは同期せず、写しのまま進め、承認一覧に「Web と同期できず、<synced_at> 時点の写しを使用」と書く。
2. ArtifactData の get で `state/current` を読む。get できないときも 1 と同じ。
3. data (`{"application": ..., "updated_at": ...}`) を `docs/pbi/state.json` に書く。ファイルが無い・`{}` なら適用なし。
4. `python <このディレクトリ>/templates.py sync --state docs/pbi/state.json` を実行する。写しが書かれ、`{"status", "application", "writes"}` が出る。
5. `writes` があれば (expired_drop の後始末)、読んだ version を `if_version` に足して ArtifactData の batch で書く。既にあるドキュメントへの書き込みは `if_version` が必須。新しく作るドキュメントには付けない。
6. status が `unknown_template` のときは、テンプレートがローカルに無い・不正。写しには application をそのまま残している。テンプレートを直すか、ユーザーに確認する。

その作業の終わりまでは開始時の判定を使う。途中で期限が来ても読み直さない。

## 4. チャットで適用を始める・延長する・止める

「deepseek-all を 11/5 まで適用して」のように頼まれたときに読む。書き込みはすべて ArtifactData の batch で行う。

**始める**
1. テンプレート名・期限 (ISO 8601、または期限なし)・期限切れの扱い (ask / drop)・理由を決める。
2. `python <このディレクトリ>/templates.py apply --state docs/pbi/state.json --template NAME --until ISO|none --on-expire ask|drop --reason TEXT --via chat` を実行する。
3. 出力は新しい `application` と `writes`。前の適用があるときは history への set が先に入っている (終わり方は通常 replaced、期限切れの drop なら expired)。
4. `writes` を、読んだ version の `if_version` を足して ArtifactData の batch で書く。
5. `state/current` を get し直してファイルに書き、`templates.py sync --state` をもう一度実行して写しを合わせる。

**延長する・期限なしにする**
- 延長: `python <このディレクトリ>/templates.py answer --state docs/pbi/state.json --choice extend --until ISO`。until は now より後。
- 期限なし: `--choice forever` (until が null になる)。
- 出力の `application` と `writes` を始めるときと同じように書いて、sync し直す。

**止める**
- `python <このディレクトリ>/templates.py answer --state docs/pbi/state.json --choice stop`。
- history への set と state/current を null にする set の writes が出る (active なら stopped、expired_ask なら declined)。書いて、sync し直す。

## 5. 期限切れの確認

- 承認の前に、sync の status が `expired_ask` なら、承認と一緒に「延長 (新しい期限を聞く) / やめる / 期限なし」を聞く。新しい止まる場面は増やさない。
- 答えは `answer` で反映する。延長は `extend`、やめるは `stop` (ended_how は declined)、期限なしは `forever`。
- `writes` を batch で書き、sync し直し、`prepare.py` を `--only` なしでもう一度実行して担当を判定し直す。
- `expired_drop` は自動では当てない。後始末は開始時の同期の `sync` の writes が行う (ended_how は expired、ended_at は until)。

## 6. テンプレートを作る・書き換える

チャットで行う。

1. `<home>/templates/<name>.json` に JSON を書く (Write ツール。name はファイル名と一致させる)。
2. `python <このディレクトリ>/templates.py check [NAME]` で形を検査する。1 件 1 行で `ok <name>` / `ng <name>: <問題>`。1 件でも ng なら保存しない。
3. 実行先でモデル名・effort 名が実在するか確かめる。opencode なら `opencode run -m <model>#<effort> "Reply with OK"` が `provider.no-route` (`Variant unavailable`) で落ちないこと。effort が null なら `#` を付けない。
4. `python <このディレクトリ>/templates.py push --existing NAME,...` を実行する。正しいテンプレートの `templates/<name>` への set と、ローカルに無い名前の delete の writes が出る。`--existing` には Web にある名前 (ArtifactData で読む) を渡す。
5. `writes` を ArtifactData の batch で書く。Web は読むだけ。

当てるのは決定表のあとで、振り分けテンプレート (routing.json) より先。sensitive の PBI には当たらず、route の最後は routing.json のその段の agent。手順には routing.json と同じく until (日付) を書け、過ぎた手順は外れる。上書きを持たない段にも当たらない。

## 7. 毎朝の通知タスク

- 毎朝 9:00 に 1 回動くスケジュールタスクを 1 つ作る。
- タスクの指示文の雛形: `<home>/web.json` の URL で ArtifactData の get により `state/current` を読む。読めなければ何もしないで終える。data を `docs/pbi/state.json` に書き、`python <このディレクトリ>/templates.py due --state docs/pbi/state.json` を実行する。`due` の適用があれば通知する (テンプレート名・期限・残り時間・理由)。なければ何もしない。
- 窓は既定の 24 時間。同じ期限の通知は 1 回で足りる。
- 読むだけで、書き換えない。
