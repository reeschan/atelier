# Desk 向け JSON: 部品とフローの参照 (`desk_catalog.py`)

Atelier Desk が、選んだ版の Atelier から部品とフローの完全な定義を読むための読み取り専用 CLI。既存の人向け CLI (`flow.py`、`component.py`) と Studio の出力は変えない。

```
python <atelier>/desk_catalog.py components --home C:/abs/home
python <atelier>/desk_catalog.py workflows  --home C:/abs/home
```

- Python 3.10 以上。スクリプトのパスを直接実行する。引数はシェルに展開しない。
- `--home` は必須で絶対パス。相対パスは終了 2。存在しない絶対パスは空の home として扱い (同梱の定義だけを返す)、何も作らない。存在するがディレクトリでない home は終了 4。
- home には何も書かない。正常でも異常でも、home の項目 (ファイル・ディレクトリ・内容) は変わらない。ネットワークは使わない。

## schema

| コマンド | `schema` | 成功時の JSON |
|---|---|---|
| `components` | `atelier.components/1` | `{"schema":"atelier.components/1","items":[{"id":"...","source":"bundled\|home","data":{}}]}` |
| `workflows` | `atelier.workflows/1` | `{"schema":"atelier.workflows/1","items":[{"name":"...","source":"bundled\|home","bundled":true,"data":{}}]}` |

- `items` は `id` / `name` の Unicode コードポイント昇順。
- `data` は元の JSON 定義を、欠けた項目を補わず全体のまま返す (説明・invoke・link・手順書 `doc`・`then`・工程・gate・optional を含む)。存在しない項目は存在しないまま。
- 部品の `source`: 同梱の `components/<id>.json` があれば `bundled`、なければ `home`。home の部品が同梱 ID と衝突するときは上書きではなくエラー (`flow.load_components` の規則)。
- フローの `source`: `<home>/workflows/<name>.json` があれば `home` (同名の同梱より優先)、なければ `bundled`。`bundled` は同名の同梱フローがあるか。home のファイルが壊れていても同梱には戻らず、エラーにする。
- フローの名前は `templates.NAME_RE` に合う stem だけ。合わない名前のファイルは一覧から黙って除外する (Studio と同じ。`flow.py --workflow` では使える)。

## 出力と終了コード

| 終了 | 意味 | stdout | stderr |
|---|---|---|---|
| 0 | 成功 | ASCII にエスケープした JSON 1 行 (UTF-8 として正しく、コードページや改行変換に左右されない) | 空 |
| 2 | 引数の誤り (未知のコマンド、`--home` の欠落、相対パス、未知の引数) | 空 | 診断 1 行 |
| 4 | 定義の破損・読み取り失敗 | 空 | 診断 1 行 |

- `--help` は例外として通常のヘルプを出し、終了 0。
- 終了 3 は使わない (名前を指定した対象ファイルがあるコマンド用)。終了 1 も使わない。未捕捉の例外は終了 4 に変換する。
- 終了 4 になるもの。部品の誤りが 1 件でもある (JSON でない・オブジェクトでない・BOM・不正な UTF-8・`NaN`・`flow.component_error` の誤り・同梱 ID との衝突・`then` の参照切れ)。フローが読めない・`flow.workflow_errors` が空でない。`*.json` がディレクトリなど通常ファイルでない。`components` / `workflows` がディレクトリでない、読み取り権限がない。
- 部品やフローの誤りが 1 件でもあれば一覧全体が終了 4。Studio は誤りのあるフローも一覧に残すが、この CLI は残さない。これは「一覧に出たものはすべて `flow.py` で実行できる」ための選択で、空の成功結果にはしない。
- 診断は stderr に UTF-8 で 1 行 (ファイル名と最初の問題、残りは `(+N more)`)。

Desk は終了 0 のときだけ `schema` を照合して `items` を使い、終了 2・4、未知の `schema`、壊れた JSON は接続または操作のエラーにする。

---

# Desk 向け JSON: 状態・検証・保存予定 (`desk_state.py`・`desk_validate.py`・`desk_plan.py`)

Desk が選んだ版の Atelier から、状態の取得・保存前の検証・保存予定の生成を行うための CLI。どれも読み取り専用で、既存の人向け CLI と Studio の出力は変えない。上の `desk_catalog.py` と共通の約束。

- Python 3.10 以上。スクリプトのパスを直接実行する。引数はシェルに展開しない。
- `--home` は絶対パス。相対パスは終了 2。存在しない絶対パスは空の home として扱い、何も作らない。存在するがディレクトリでない home は終了 4。
- home にも配布版にも何も書かず、ネットワークは使わない。正常でも異常でも、home の項目 (ファイル・ディレクトリ・内容) は変わらない。
- 成功と、終了 1 の検証失敗では、stdout に ASCII にエスケープした UTF-8 の JSON を 1 行だけ書き、stderr は空。終了 2〜4 では stdout は空で、stderr に診断を 1 行書く。`--help` は通常のヘルプを出して終了 0。
- 未捕捉の例外を終了 1 にしない。読み取り失敗などは終了 4 に変換する。
- Desk は終了 0 (と、検証・保存予定では終了 1) のときだけ `schema` を照合して使い、終了 2〜4、未知の `schema`、壊れた JSON は接続または操作のエラーにする。

## schema の名前と版の一覧

| CLI | `schema` | 返る場面 |
|---|---|---|
| `desk_catalog.py components` | `atelier.components/1` | 終了 0 |
| `desk_catalog.py workflows` | `atelier.workflows/1` | 終了 0 |
| `desk_state.py` | `atelier.state/1` | 終了 0 |
| `desk_validate.py` | `atelier.validation/1` | 終了 0 (有効)・終了 1 (検証失敗) |
| `desk_plan.py` | `atelier.save-plan/1` | 終了 0 |
| `desk_plan.py` | `atelier.validation/1` | 終了 1 (検証失敗。`desk_validate.py` と同じ形) |

## 終了コードの表

| 終了 | 意味 | `desk_catalog.py` | `desk_state.py` | `desk_validate.py` | `desk_plan.py` |
|---|---|---|---|---|---|
| 0 | 成功 | 使う | 使う | 使う (有効) | 使う |
| 1 | 検証失敗 (入力は正しい JSON だが Studio なら保存を断る) | 使わない | 使わない | 使う (`valid:false`) | 使う (`atelier.validation/1` の `valid:false`) |
| 2 | 呼び出しの誤り (引数不正・不正な JSON・JSON オブジェクトでない入力・UTF-8 でない入力・未知の KIND・未知の runner・不正な名前・相対パスの `--home`) | 使う | 使う | 使う | 使う |
| 3 | `--input` など名前を指定した対象ファイルが無い | 使わない | 使わない | 使う (`--input` の不在) | 使う (`--input` の不在。削除・コピー元が無いのは Studio の 404 に当たる) |
| 4 | 定義の破損・読み取り失敗・予期しない例外 | 使う | 使う | 使う | 使う |

## `desk_state.py`: home の状態

```
python <atelier>/desk_state.py [--home C:/abs/home]
```

- `--home` は省略できる。省略すると Atelier の既存の規則 (`common.resolve_home`) で位置を決める。指定したときは上の共通の約束どおり。
- 成功 (終了 0): `{"schema":"atelier.state/1","home":"...","runners":{},"routing":{},"templates":{},"examples":{},"application":null,"application_status":"...","warnings":[]}`。値は Studio の `state()` と同じ形 (上の空オブジェクトは形の例)。`flow` だけは Desk が `desk_catalog.py` で取るので含めない。
- 期限切れの優先テンプレートも読み取りだけで `application_status` に表し、履歴や `overrides.json` を書き換えない。
- JSON として読めたが意味の誤りがあるファイルは、Studio と同じ既定値か `warnings` で終了 0 のまま返す。JSON として読めないファイル (`routing.json` を含む) と、存在するがディレクトリでない home は終了 4。
- 終了 3 は使わない。終了 1 も使わない。

## `desk_validate.py`: 保存前の検証

```
python <atelier>/desk_validate.py KIND --home C:/abs/home --input FILE [--name NAME]
```

- `KIND`: `runner` / `model` / `routing` / `template` / `workflow` / `application` / `application-answer`。
- `--input` は BOM なしの UTF-8 の JSON オブジェクトで、KIND ごとの Studio の PUT/POST 本文と同じ形。Studio の 64 KB の上限は CLI には適用しない。
- `--name`: `runner` と `model` は runner 名、`template` と `workflow` は対象名で必須。残りの KIND は `--name` を受け付けない (終了 2)。
- 成功 (終了 0): `{"schema":"atelier.validation/1","kind":"...","valid":true,"problems":[]}`。
- 検証失敗 (終了 1): 同じ形で `valid:false`。`problems` は Studio の HTTP エラー本文の `error` と完全に同じ文字列 1 つの配列。Studio が複数の問題を一文に結合する場合もその一文のまま。home の既存ファイルが壊れていても Studio と同じ結果を返す (Studio は壊れた定義を既定値として扱う)。
- 終了 2: 引数の誤り・不正な JSON・JSON オブジェクトでない入力・UTF-8 として読めない入力・未知の KIND・未知の runner・不正な名前・`--name` の過不足・相対パスの `--home`。終了 3: `--input` のファイルが無い。終了 4: 読み取り失敗 (入力がディレクトリ、home がディレクトリでない、予期しない例外)。
- 判定の順序: `--name` の誤り (未知の runner・不正な名前・過不足) は `--input` の読み取りより先に判定する。そのため、名前が誤っていて入力ファイルも無いときは、終了 3 ではなく終了 2 になる (終了 2 が終了 3 より優先)。
- `application` / `application-answer` は操作の事前条件も検証するが、副作用のある `apply_local` / `answer_local` や保存ハンドラーは呼ばない。

## `desk_plan.py`: 保存予定

```
python <atelier>/desk_plan.py KIND --home C:/abs/home --input FILE [--name NAME]
```

- `KIND` は `desk_validate.py` と同じ 7 種類と、本文のない 4 種類 (`template-delete`・`example-copy`・`workflow-delete`・`application-expire`)。本文のない KIND は `--input` に `{}` だけを渡す。`{}` 以外は終了 2。
- `--name`: `runner`・`model` は runner 名、`template`・`template-delete`・`workflow`・`workflow-delete` は対象名、`example-copy` は同梱 example 名で必須。ほかの KIND (`routing`・`application`・`application-answer`・`application-expire`) は受け付けない。
- 成功 (終了 0): `{"schema":"atelier.save-plan/1","kind":"runner","evaluated_at":"...","reads":[...],"writes":[...]}`。
  - `evaluated_at`: 操作の評価時刻 (タイムゾーン付き。計画内で 1 回だけ採る)。
  - `reads`: 計画が頼った全ての読み取り (不在も)。ファイル依存は `{"root":"home|atelier","type":"file","path":"agents.json","sha256":"<小文字 64 桁>|missing"}`。ディレクトリ依存は `{"root":"home|atelier","type":"directory","path":"components","missing":false,"entries":[{"name":"x.json","type":"file","sha256":"..."}]}`。`entries` は直下で `*.json` に一致するものだけを名前順に含む (`notes.txt` などは含まない)。一致の大文字小文字の扱いは OS の `Path.glob` に従う。一致したものが通常ファイルでなければ (リンク・ディレクトリなど) 終了 4。`root` が `atelier` のものは配布版の読み取り専用の定義で、書き込み先にはならない。`sha256` は生バイトの SHA-256。不在の `missing` と空は区別する。
  - `writes`: 順序付きの `{"op":"replace","path":"...","content_utf8":"..."}` か `{"op":"delete","path":"..."}`。パスは `/` 区切りの home 相対。`content_utf8` はファイル全体で、Studio の保存とバイト単位で同じ (履歴の追記は既存の内容に `os.linesep` の 1 行を足したもの)。
  - 期限切れでない `application-expire` は `writes` が空で終了 0。
- 検証失敗 (終了 1): `desk_validate.py` と同じ `atelier.validation/1` (`valid:false`、`problems` は Studio の `error` と同じ文字列 1 つ) を stdout に返す。`atelier.save-plan/1` は返さない。
- 終了 2〜4 の意味は上の表のとおり。削除・コピー元が無いときは終了 3 (Studio の 404 に当たる)。
- 壊れた `agents.json`・`models/<runner>.json`・`overrides.json` は Studio と同じく上書きする計画を返す (終了 0。Desk が置換前に退避する)。
- リンク・ジャンクション・通常ファイル以外の読み取り対象、配布版の外 (`..` や絶対パス) を指す home の部品の `doc` や `invoke.path` は、依存として表せないので終了 4。
- 終了 4 になるもう 1 つの場合: `history.jsonl` に追記する KIND (履歴を残す `application`・`application-answer`・`application-expire`) で、既存の `history.jsonl` が UTF-8 として読めない (追記後の完成内容を `content_utf8` で表せない)。この場合、Studio は追記に成功し (200)、`desk_validate.py` も `valid:true` を返すが、`desk_plan.py` は終了 4 にする。
- 計画には書き込み先だけを載せ、親ディレクトリは作らない (無ければ Desk の置換側が作る前提)。
