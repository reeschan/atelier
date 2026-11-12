# Glossary

Terms used by this skill's instructions, with the Japanese term used in the specs and with Japanese-speaking users. Each entry: **English (日本語)**, the meaning, and words to avoid.

## Roles

**caller (呼び出し側)**:
The session the user talks to (Claude Code, Codex, Grok …). It runs the conductor, launches the designer when the conductor says to, and carries the lines and questions to the user and the answers back. It designs, builds and reviews nothing, and reads no PBI or diff.
_Avoid_: main agent, parent session

**conductor (進行役)**:
The script (`conductor.py`) that keeps one run in `docs/pbi/conductor/` and hands the designer one episode at a time. It is not a model and never designs.
_Avoid_: dispatcher, scheduler, loop

**orchestrator (設計者)**:
The designer Atelier owns, launched fresh for every episode; inline, the caller's own session. It designs, splits the work into PBIs, routes and accepts them. It does not build.
_Avoid_: parent agent, オーケストレーター (only as a transliteration)

**worker (実装担当)**:
A subagent or external CLI that builds one PBI from the PBI and its refs alone, following `worker.md`.
_Avoid_: child agent, ワーカー

**reviewer (レビュー担当)**:
The subagent that runs the verify commands, reads the diff and writes one review JSON per report. Never the model that built what it reviews.
_Avoid_: checker, QA

**tester (テスト担当)**:
The subagent that runs the tests at the end of a wave and writes a test result. It proposes the cause of each failure; the designer decides.
_Avoid_: test runner, QA

**runner (実行先)**:
What runs a worker or a role: `agent` (Claude: a Claude Code subagent, or `claude -p` for a headless designer), opencode, codex, agy, dsh, claude, or mcp. A role's runner is its `runner` key (`agent` unless set); the designer takes `agent`, codex or claude only, and mcp runs workers only.
_Avoid_: provider, backend

**Agent tool (Agent ツール)**:
Whether the designer can launch subagents. A designer on `agent` has it; a designer on another runner does not, and launches the roles and workers set to `agent` through `claude -p` instead.
_Avoid_: subagent support

## Workflow

**workflow template (開発フローのテンプレート)**:
A file in `workflows/` that lists, for each of the nine stages, its candidate components in priority order, plus gates and settings. `flow.py` resolves and checks it.
_Avoid_: pipeline, preset (except for the bundled templates)

**stage (工程)**:
One of the nine fixed steps: understand, design, plan, route, build, verify, review, fix, finish. Each hands its artifact to the next.
_Avoid_: phase, 段 (that is a tier)

**episode (出番)**:
One launch of the designer for one step of a run (design, build, test, review, finish). It starts fresh with a prompt, has no memory of earlier episodes, and ends by writing its result. The prompt and the links are in `appendix/episodes.md`.
_Avoid_: turn, session, iteration

**episode result (出番の結果)**:
The JSON an episode writes at `docs/pbi/conductor/episodes/<run8>-<seq>-<episode>.result.json` (`<run8>`: the first 8 characters of the run id) (schema `atelier.episode/1`): status, summary, next, question, state, report, reason. The conductor checks it to move the run on.
_Avoid_: output, return value

**component (コンポーネント)**:
A definition in `components/` that serves one or more stages: how it is invoked, where it runs, what it needs, how much memory it may use, and the doc that holds its procedure.
_Avoid_: plugin, module

**gate (関所)**:
Components that run after a stage, all of them that are usable (Jev gate 1, blast-radius, the security review). "Jev gate" names the Jev API checks inside some of them.
_Avoid_: hook, checkpoint

**fallback (後ろ盾)**:
The last component of every stage: builtin, or a script with no requirements, so every template runs with Claude Code alone.
_Avoid_: default component

**optional stage (オプションの工程)**:
A step placed after one of the nine stages by the template's `optional` list: ui-design, marketing, security, map, lint-survey, lint-recheck. The bundled templates place map, lint-survey and lint-recheck.
_Avoid_: extra stage, 段 (that is a tier)

**UI new look / UI retouch (見た目づくり・見た目の手直し)**:
The two halves of the ui-design decision, `ui-change` and `ui-retouch`; at most one holds in a run. A new look makes screens from a direction (design-studio); a retouch audits screens that already exist for AI-looking signs and fixes them through PBIs (unslop).
_Avoid_: redesign (it can mean either), unslop for prose (pstack's `unslop` cleans writing, not screens)

**map (構成図の工程)**:
The optional stage that summarises the layers and modules the request touches, from the OMM cache, an OMM scan, or the fallback prompt.
_Avoid_: architecture scan (that is OMM's own step), diagram

**lint survey / lint recheck (lint の下調べ・lint の見直し)**:
The optional stages that run jevlint before the design (scores, long functions, weak questions, bug-hunt leads in the range) and after the test stage (what the change made worse than the merge-base).
_Avoid_: lint (that is the repository's own linter), audit alone

**send approval (送信の許可)**:
The user's answer, asked right before each jevlint run that sends file contents to an outside model. One answer covers one file list once.
_Avoid_: consent in general, standing approval

**fallback prompt (代わりの依頼文)**:
The bundled prompt that does a recommended tool's job inside Claude Code when the tool is missing (`prompts/map-fallback.md`, `prompts/lint-fallback.md`, `prompts/kiln-chamber.md`).
_Avoid_: fallback alone (that is the last component of a stage)

**recommended tool (推奨の道具)**:
oh-my-mermaid, jevlint, sac-strategic-compact and pstack's test principle: used when installed, offered at Approve when missing, replaced by a fallback prompt otherwise. `tools_check.py` lists them.
_Avoid_: dependency, requirement

**wave (波)**:
PBIs with no unfinished dependency, run together and reviewed before the next wave starts.
_Avoid_: batch, round

## PBIs and runs

**PBI**:
One small unit of work in `docs/pbi/PBI-<n>.md`, written so that a worker needs nothing else.
_Avoid_: task, ticket

**report name (報告名)**:
`PBI-<n>`, or `PBI-<n>.rework-<k>` for a rework. Files, transcripts and runs are linked through it.
_Avoid_: run id

**work root (作業ルート)**:
The absolute path of the repository or worktree a worker works in, named on the prompt's first line.
_Avoid_: cwd, base directory

**rework (差し戻し)**:
Sending a PBI back to a new worker with a rework note. Limited by the template's `fix.max_rework`.
_Avoid_: retry (a retry is a rerun of an unusable runner)

## Routing

**tier (段)**:
The weight the decision table gives a PBI: haiku, sonnet or opus.
_Avoid_: level, rank, stage

**tier up (段上げ)**:
Raising a PBI's tier and rewriting its route. Recorded as the outcome `tier-up`.
_Avoid_: promotion (used only for memory)

**route**:
The runner, model and effort candidates for one PBI, in the order to try. When a step is unusable, the user picks what runs next, often a later step of the route (`runners.md`).
_Avoid_: assignment list

**sensitive**:
A PBI that touches authentication, permissions, a database schema, deletion of user data, or secrets. It never goes to an external runner through a priority template.
_Avoid_: confidential PBI, dangerous PBI

## Personal settings

**home**:
The per-user settings directory outside the skill, kept across updates of the skill.
_Avoid_: config directory, skill root

**available models (使えるモデル)**:
The models a runner reports that it accepts.
_Avoid_: catalog (that is the quality and price reference)

**selected models (選んだモデル)**:
The available models the user chose for routing. Only these appear in routing template steps.
_Avoid_: enabled models

## Templates for routing

"Template" alone is ambiguous; always name which one.

**routing template (振り分けテンプレート)**:
The usual routing table: a route per tier, each ending with `agent`. Built in Template Studio, kept in `docs/pbi/routing.json` or home. No expiry (a step may have an until).
_Avoid_: routing settings, default template

**priority template (優先テンプレート)**:
A named, saved override of the routing template, applied with an expiry. Same shape as the routing template; cannot change how sensitive PBIs, reviews or design reviews are handled; the final agent comes from the routing template.
_Avoid_: profile, override

**rotate group (交互グループ)**:
`{"rotate": [step, ...]}` in a priority template's steps. The first member changes per PBI and the rest follow as fallbacks, spreading PBIs evenly. Not allowed in the routing template.
_Avoid_: rotation, round robin

**rotation number (回す数)**:
The PBI number; the first member of a rotate group is rotation number mod member count. The same PBI always gets the same order.
_Avoid_: offset, seed

**application (適用)**:
A priority template turned on with an expiry, an on-expire rule and a reason. Only one exists at a time.
_Avoid_: activation, override

**on expire (期限切れの扱い)**:
What happens after an application expires: `ask` stops applying and asks the user, `drop` stops applying without asking.
_Avoid_: expiry policy

## Memory

**work, work key, workspace (作業・作業キー・作業空間)**:
One user request; its key `<repo>:<branch>:W-<n>`; and its memory scope `project:work.<repo>.<branch>.W-<n>`.
_Avoid_: session (a work can span sessions)

**ref**:
One fact the orchestrator writes in the workspace, listed by id in the PBIs that need it.
_Avoid_: note (notes are files in `docs/pbi/notes/`)

**promotion (昇格)**:
At the end, copying up to 3 checked refs into repository or global memory.
_Avoid_: tier up (that is routing)

## Context capacity

**Kiln (窯)**:
Limit on working context for this run. The English name is Kiln, from Compaction (working-context cool-down / strategic compaction). The Japanese reading is 窯. Soft / peak / handoff / aux lines, plus separate chambers for the orchestrator (designer) and every subagent and worker (implementer). On by default; `docs/pbi/kiln.json` with `"enabled": false` turns it off. It reaches subagents and external workers through the implementer block of `prompts/kiln-chamber.md`. Details in `appendix/kiln.md`.
_Avoid_: leading with 窯 as the source term, context window (that is the model limit), budget alone (name the Kiln lines)

**state tablet (状態の板)**:
The short file written before a cool-down (`docs/pbi/CONTEXT_STATE.md`), or the `## State` section a worker writes when it stops at its peak: objective, facts, changes, verification, dead ends, remaining plan.
_Avoid_: summary, memo

**designer chamber / implementer chamber (設計者室・実装者室)**:
The Kiln budgets for the orchestrator and for one worker attempt. The implementer chamber prefers a fresh worker per attempt (`worker.md`).
_Avoid_: parent/child context

## Adapters

**adapter setting (アダプターの設定)**:
The `adapters` object in agents.json: per design, review and security, `auto`, `off`, or a skill name that replaces the template's components.
_Avoid_: hook, integration

**callee (呼び出し先)**:
Another skill that a component calls. It never decides pass or fail, and never calls this skill back.
_Avoid_: external skill, dependency

## Approval

**approval draft (承認の原稿)**:
The Markdown the designer writes to `docs/pbi/notes/approve.md` for the user to approve the run: seven fixed sections in a fixed order (`appendix/approve.md`), checked by `design_record.py lint`.
_Avoid_: approval list, 承認の一覧, 下書き

**before / after flow (今の流れ・あとの流れ)**:
The two diagrams of an approval draft: the flow as the user sees it today, and the same flow after the run, with each changed line marked (`+`, `-`, `~`) and the PBI number in the node it changes.
_Avoid_: 設計図, 構成図

**internal term (内部の言葉)**:
A word only Atelier's insides use (route, tier, stage, a quality score's name, a component's name). Text the user reads rewords it or explains it on the same line.
_Avoid_: jargon, 専門用語

## Harness metrics

**event (イベント)**:
One checked line in the run's local `docs/pbi/events.jsonl`, schema `atelier.event/1`: `run_id`, `seq`, `stage`, `pbi`, a `type` and identifier-only `data`. The sender may copy it out; nothing of it is required for the run to go on.
_Avoid_: log line, telemetry

**sender (送り手)**:
The harness-log script that takes a checked event, writes it to its spool and, when the user enabled it, commits and pushes the lines to the metrics repository. Atelier imports it when it is installed and writes only the local file when it is not.
_Avoid_: collector, exporter

**allowlist (許可リスト)**:
The per-type list of fields and vocabularies in `harness.schema.json` that an event must pass before it is written or sent. A value outside it is dropped and its name goes in `dropped`; the rest of the line is kept.
_Avoid_: schema (the line names its own), filter

**spool (スプール)**:
The sender's local folder where events wait before `flush` turns them into one commit, under `<phase>/<skill>/<YYYY-MM>/<run_id>.jsonl`. An event in the spool is not yet pushed, and a failed push keeps the lines for the next flush.
_Avoid_: buffer, queue

**task profile (タスクの特徴)**:
The measured and tagged description of one PBI or of a whole run (`task.profiled`): size, spread, context weight, languages, frameworks and the designer's `task_type` / `domain` / `difficulty` tags.
_Avoid_: metadata, attributes

**anchor (アンカー)**:
A representative task sentence in `taxonomy.json` that a task profile's embedding is compared against. The top three matches are sent with their `anchor_id` and score; the vectors stay in the archive.
_Avoid_: exemplar, template

**development phase (開発段階)**:
The phase a sender or a report groups work under, `implement` at this skill. It is a different word from `stage` (工程), which is one of the nine steps of a run: it names the envelope's `phase` item only.
_Avoid_: stage, step

**split (分割)**:
The fixed train / heldout / regression assignment of cases, kept in a home manifest with a seed and a `split_set_id`. A case never moves once assigned, except from heldout to regression after it was read.
_Avoid_: partition, fold

**held-out**:
The cases a change-maker never reads: only the id, the size and the pass/fail count may be shown (`export_cases.py --view improver`). A held-out case whose failure was read is retired to `regression`.
_Avoid_: test set, validation set

**change (変更)**:
One edit to a worker prompt, designer prompt, appendix, routing, model, workflow, tool or grader, recorded in `changes.jsonl` (`harness.change/1`) with a `change_id` and its parent. A round judges one change.
_Avoid_: commit, patch

**trial (試行)**:
One replay of the cases under one change, numbered from 0 in the envelope's `trial`. A round aggregates the trials; `HARNESS_TRIAL` sets the number outside a replay.
_Avoid_: attempt (that is one build of a PBI), run

**round (判定)**:
The comparison of one change with its base over the trials (`round.judged`): `verdict` accept, overfit_suspect, regress, noise or invalid, with how train and held-out moved and a confidence interval.
_Avoid_: iteration, wave

**triage (根因の仕分け)**:
Labelling the root cause of a lineage (`triage.labeled`): task, harness, agent, env, grader or noise, by a human or by the analyzer. The atelier cause maps to it (`spec` to task, `convention`/`scope`/`memory` to harness, `model` to agent, `env` to env, `grader` to grader, `flaky` to noise).
_Avoid_: diagnosis, postmortem

**config fingerprint (構成の指紋, config_fp)**:
The first 12 characters of the sha256 of what defines a condition: skill version, the hashes of `worker.md` and `designer.md`, the resolved roles, the workflow name and the default routing. Every event of that condition carries it in the envelope.
_Avoid_: hash alone, version

**unit (試行の単位, unit_id)**:
One attempt as the analyzer reads it, `at:<run_id>:<pbi>:<attempt>`, carried in the envelope so a local row and a sent row join to the same attempt.
_Avoid_: record, row

**signature (シグネチャ)**:
The identifier of an attempt built from enum-only parts (outcome, causes, counts, flags, runner, tier), so attempts group without prose. `sig_id` is the first 12 characters of the sha256 of the parts.
_Avoid_: summary, hash of the attempt
