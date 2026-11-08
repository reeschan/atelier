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

**optional stage (オプションの工程)**:
A step placed after one of the nine stages by the template's `optional` list: ui-design, marketing, security, map, lint-survey, lint-recheck. The bundled templates place map, lint-survey and lint-recheck.
_Avoid_: extra stage, 段 (that is a tier)

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
