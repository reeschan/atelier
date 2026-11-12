# harness-log の見本 (2026-11-12)

[harness-metrics.md](../specs/harness-metrics.md) の送り手 `skills/harness-log` が、herness-log に何を送るかの見本。送ったのは本物の herness-log ではなく、手元に作った bare リポジトリ。

## 作り方

1. 使い捨ての git リポジトリ (`origin` を `https://github.com/reeschan/atelier.git` にしたもの) で、新しい `conductor.py` を回した。start → next → 設計の出番が OMM の問い (`decision`・`omm`) で終わる → answer → abort。
2. 設計者のコマンドとして `events.py answer --kind decision --topic omm --choice b --wait-s 42` と `events.py stage --component map.builtin --decision cache --by user --reason approved` を打った。
3. `HARNESS_LOG_CONFIG` で別の設定を使い、`preview` → `enable` → `flush` で bare リポジトリに送った。

イベントは 9 行 (run.started 1・episode.started 1・question.asked 1・question.answered 2・stage.decided 1・episode.ended 1・episode.cost 1・run.ended 1) になった。

## 送り先の並び

```
SCHEMA.md
implement/atelier/2026-11/<run_id>.jsonl
implement/atelier/coverage.json
```

コミットは flush 1 回につき 1 つ (`harness-log flush`)。この見本には `task.profiled` が無いので、`coverage.json` の `rows` は空。

## 種類ごとの 1 行 (preview の出力のまま)

```jsonl
{"schema":"harness.event/1","ts":"2026-11-12T10:04:18+00:00","phase":"implement","skill":"atelier","skill_version":"unreleased","data_schema":"atelier.event/1","repo":"reeschan/atelier","branch":"h:22337a41597a","host":"h:1fc1bdafb42f","run_id":"cc87eb65-1066-4967-a775-a24ca357b9b5","seq":0,"stage":null,"pbi":null,"change_id":"ch-20261111134033-55a531","config_fp":"8f6c93fdaae1","type":"run.started","data":{"workflow":"default","launch":"agent","roles_from":{"designer":"default","reviewer.deep":"default","reviewer.other":"default","tester":"default"}}}
{"schema":"harness.event/1","ts":"2026-11-12T10:04:18+00:00","phase":"implement","skill":"atelier","skill_version":"unreleased","data_schema":"atelier.event/1","repo":"reeschan/atelier","branch":"h:22337a41597a","host":"h:1fc1bdafb42f","run_id":"cc87eb65-1066-4967-a775-a24ca357b9b5","seq":1,"stage":null,"pbi":null,"change_id":"ch-20261111134033-55a531","config_fp":"8f6c93fdaae1","type":"episode.started","data":{"episode":"design","designer":"fallback","launcher":"agent","roles":{"designer":{"from":"default","model":"opus"},"reviewer.deep":{"from":"default","model":"opus"},"reviewer.other":{"from":"default","model":"sonnet","effort":"high"},"tester":{"from":"default","model":"sonnet"}}}}
{"schema":"harness.event/1","ts":"2026-11-12T10:04:18+00:00","phase":"implement","skill":"atelier","skill_version":"unreleased","data_schema":"atelier.event/1","repo":"reeschan/atelier","branch":"h:22337a41597a","host":"h:1fc1bdafb42f","run_id":"cc87eb65-1066-4967-a775-a24ca357b9b5","seq":1,"stage":null,"pbi":null,"change_id":"ch-20261111134033-55a531","config_fp":"8f6c93fdaae1","type":"question.asked","data":{"kind":"decision","topic":"omm","items":1}}
{"schema":"harness.event/1","ts":"2026-11-12T10:04:19+00:00","phase":"implement","skill":"atelier","skill_version":"unreleased","data_schema":"atelier.event/1","repo":"reeschan/atelier","branch":"h:22337a41597a","host":"h:1fc1bdafb42f","run_id":"cc87eb65-1066-4967-a775-a24ca357b9b5","seq":1,"stage":null,"pbi":null,"change_id":"ch-20261111134033-55a531","config_fp":"8f6c93fdaae1","type":"question.answered","data":{"kind":"decision","topic":"omm","choice":null,"wait_s":1,"revise":false}}
{"schema":"harness.event/1","ts":"2026-11-12T10:04:19+00:00","phase":"implement","skill":"atelier","skill_version":"unreleased","data_schema":"atelier.event/1","repo":"reeschan/atelier","branch":"h:22337a41597a","host":"h:1fc1bdafb42f","run_id":"cc87eb65-1066-4967-a775-a24ca357b9b5","seq":1,"stage":"map","pbi":null,"change_id":"ch-20261111134033-55a531","config_fp":"8f6c93fdaae1","type":"stage.decided","data":{"component":"map.builtin","decision":"cache","by":"user","reason":"approved"}}
{"schema":"harness.event/1","ts":"2026-11-12T10:04:18+00:00","phase":"implement","skill":"atelier","skill_version":"unreleased","data_schema":"atelier.event/1","repo":"reeschan/atelier","branch":"h:22337a41597a","host":"h:1fc1bdafb42f","run_id":"cc87eb65-1066-4967-a775-a24ca357b9b5","seq":1,"stage":null,"pbi":null,"change_id":"ch-20261111134033-55a531","config_fp":"8f6c93fdaae1","type":"episode.ended","data":{"episode":"design","status":"question","seconds":0,"failures":0}}
{"schema":"harness.event/1","ts":"2026-11-12T10:04:19+00:00","phase":"implement","skill":"atelier","skill_version":"unreleased","data_schema":"atelier.event/1","repo":"reeschan/atelier","branch":"h:22337a41597a","host":"h:1fc1bdafb42f","run_id":"cc87eb65-1066-4967-a775-a24ca357b9b5","seq":1,"stage":null,"pbi":null,"change_id":"ch-20261111134033-55a531","config_fp":"8f6c93fdaae1","type":"episode.cost","data":{"episode":"design","method":"unmeasured","roles":{}}}
{"schema":"harness.event/1","ts":"2026-11-12T10:04:20+00:00","phase":"implement","skill":"atelier","skill_version":"unreleased","data_schema":"atelier.event/1","repo":"reeschan/atelier","branch":"h:22337a41597a","host":"h:1fc1bdafb42f","run_id":"cc87eb65-1066-4967-a775-a24ca357b9b5","seq":1,"stage":null,"pbi":null,"change_id":"ch-20261111134033-55a531","config_fp":"8f6c93fdaae1","type":"run.ended","data":{"result":"aborted","episodes":1,"seconds":1}}
```

## 読み取れること

- 本文・自由文・パスは入っていない (goal の文、問いの summary、answer の文は外に出ていない)。
- `repo` は `show_repos` (`reeschan/*`) に合うので `reeschan/atelier` のまま。`branch` と `host` は鍵付きのハッシュ (`h:` + 12 桁)。
- `change_id` と `config_fp` は、home の `changes.jsonl` から付く。見本でも、ほかの作業で登録した change が付いた (完了報告の PBI-13 の注意「保全先の changes.jsonl を repo 間で共有している」と同じ)。
- `skill_version` は、リポジトリから動かすと `unreleased` になる (~/.claude/skills の版付きの導入から動かすと、その版になる)。
- conductor が出す `question.answered` の `choice` は null。選んだ記号は、設計者の `events.py answer` の行 (`choice: b`・`wait_s: 42`) に入る。`choice` は選択肢の記号 (`a`〜`z`・`yes`・`no`・`revise`) だけを受け、`cache` のような語は受けない。
- 設計者の `stage` の `reason` も決まった語彙 (`approved`・`spec-given` など) だけを受ける。

## 本物の送り先の今の状態

- `~/.claude/harness-log.json` は `init` 済みで `enabled: false`。送り先は `https://github.com/reeschan/herness-log.git`、clone は `~/.claude/harness-log/clone`、埋め込みは `http://127.0.0.1:11435` (SSH 先の Ollama)。
- スプールは空。送り始めるのは、ユーザーが `python skills/harness-log/harness_log.py enable` を打ってから。
