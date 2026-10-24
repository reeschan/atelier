# Glossary

Terms used by this skill's instructions, with the Japanese term used in the specs and with Japanese-speaking users. Each entry: **English (日本語)**, the meaning, and words to avoid.

## Roles

**orchestrator (設計者)**:
The main session that runs the skill. It designs, splits the work into PBIs, routes and accepts them. It does not build.
_Avoid_: parent agent, オーケストレーター (only as a transliteration)

**worker (実装担当)**:
A subagent or external CLI that builds one PBI from the PBI and its refs alone, following `worker.md`.
_Avoid_: child agent, ワーカー

**runner (実行先)**:
What runs a worker: `agent` (a Claude Code subagent), opencode, codex, agy, dsh, claude, or mcp.
_Avoid_: provider, backend

## Workflow

**workflow template (開発フローのテンプレート)**:
A file in `workflows/` that lists, for each of the nine stages, its candidate components in priority order, plus gates and settings. `flow.py` resolves and checks it.
_Avoid_: pipeline, preset (except for the bundled templates)

**stage (工程)**:
One of the nine fixed steps: understand, design, plan, route, build, verify, review, fix, finish. Each hands its artifact to the next.
_Avoid_: phase, 段 (that is a tier)

**component (コンポーネント)**:
A definition in `components/` that serves one or more stages: how it is invoked, where it runs, what it needs, how much memory it may use, and the doc that holds its procedure.
_Avoid_: plugin, module

**gate (関所)**:
Components that run after a stage, all of them that are usable (Jev gate 1, blast-radius, the security review). "Jev gate" names the Jev API checks inside some of them.
_Avoid_: hook, checkpoint

**fallback (後ろ盾)**:
The last component of every stage: builtin, or a script with no requirements, so every template runs with Claude Code alone.
_Avoid_: default component

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
The runner, model and effort candidates for one PBI, in the order to try. When the head is unusable, the next one runs.
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

## Adapters

**adapter setting (アダプターの設定)**:
The `adapters` object in agents.json: per design, review and security, `auto`, `off`, or a skill name that replaces the template's components.
_Avoid_: hook, integration

**callee (呼び出し先)**:
Another skill that a component calls. It never decides pass or fail, and never calls this skill back.
_Avoid_: external skill, dependency
