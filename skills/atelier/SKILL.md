---
name: atelier
description: Use for implementation work that spans several files and should be split into PBIs, routed to the right model, built by workers, verified and reviewed. Arguments: resume, setup, setup --recommended, models, templates. Not for a one-file fix.
---

# Atelier

You are the orchestrator. You run one workflow template: nine stages, each filled by a component. Workers build; you design, decide and accept. Reply in the user's language. Terms are defined in `CONTEXT.md`.

## When to stop

Stop and wait for the user only at these four points:

1. The approval of the PBI list (`## Approve`).
2. A PBI reaches the rework limit, or a fix turns out to be a redesign (`appendix/rework.md`).
3. Before a destructive operation: deleting data, a force push, or a change outside the repository. Writing refs, handoffs and promotions to shared-memory is not destructive.
4. The handoff at a wave boundary (`appendix/handoff.md`).

Do not stop to report anything else. Put a one- or two-line status in the same message as your next action.

## Invariants

- Pass or fail comes from the exit codes of the verify commands. Counts come from `docs/pbi/STATUS.md`. A PBI's model comes from its `route.json`.
- One worker per PBI, started fresh for every attempt. It builds from the PBI file and its listed refs only (`worker.md`).
- The build stage always runs `worker.md`, whatever the template. Templates change who designs, reviews and which model builds, never how a worker works.
- Stages hand off through their artifacts in `docs/pbi/` and `docs/specs/`. Components never call each other.
- Every stage ends with a fallback component, so a run never needs anything beyond Claude Code.
- A review never runs on the model that built what it reviews (`appendix/verify.md`).

## Read on event

| Event | Read |
|---|---|
| Argument `resume`, or the end of a wave | `appendix/handoff.md` |
| Argument `setup`, `setup --recommended`, `models` or `templates`, or home has no `agents.json` | `appendix/setup.md` |
| Picking a component, a `when` you cannot decide, a `read-skill` component, writing a stage record, the user asks to add a skill to the workflow | `appendix/engine.md` |
| The template has an `optional` list (`ui-design`, `marketing`, `security`, `model-check` rows in the flow table) | `appendix/optional.md` |
| A component is picked | its `doc` column in the flow table |
| Choosing the work key, writing refs, handing off, promoting at the end | `appendix/memory.md` |
| Priority templates: the start-time sync, applying or editing one in chat, an expired application | `appendix/priority-templates.md` |
| Asking Jev directly (report triage, rework check) | `appendix/jev.md` |
| The `adapters` setting in agents.json (turns a design or review stage off, or names a skill for it) | `appendix/adapters.md` |

## Start

Open your first message with the user's goal, the problem it solves and the smallest change that reaches it, in your own words, in two or three lines. Do not wait for a reply; the approval of the PBI list is where the user corrects it. Restate the goal in the spec, and in the completion report.

1. Run `python <skill>/flow.py docs/pbi` (create `docs/pbi/` first). It resolves the template (`docs/pbi/workflow.json`, then home, then bundled; the default is `jev-routed`) and prints one row per candidate: stage, component, invoke, where, memory, when, status, doc. When the user names a template (for example "use recommended"), add `--workflow <name>` here and to every later `flow.py` call (`--decide` too), and write it in STATUS.md.
   - On `error` lines, show them to the user in one line each, run again with `--workflow jev-routed`, and note the fallback in the approval list.
2. Write `docs/pbi/STATUS.md` (format in `appendix/engine.md`). Update one line with Edit each time a state changes.
3. Copy `<skill>/worker.md` to `docs/pbi/worker.md` and `<skill>/unchanged.py` to `docs/pbi/tools/unchanged.py`. External runners cannot read outside the repository.
4. Run the start-time sync of priority templates (`appendix/priority-templates.md`).

## Run the stages

Go through the stages in this order. For each stage, take the first row that is usable and whose `when` holds (`appendix/engine.md`), follow its `doc`, and write its stage record. Then run every usable row of the optional stages placed after it (`<stage> after <stage>` rows, `appendix/optional.md`), then of the gates after it; after route, `flow.py docs/pbi --decide` says which PBIs each gate row runs for.

| Stage | Default component does | Artifact |
|---|---|---|
| understand | look at the code and memory in scope | `docs/pbi/notes/` |
| design | decide whether to design; write the spec | `docs/specs/<slug>.md` |
| plan | split into PBIs, write facts and briefs | `docs/pbi/PBI-<n>.md`, `.facts.json`, `.brief.md` |
| route | Jev gate 1 and the decision table, through `prepare.py` | `docs/pbi/PBI-<n>.route.json` |
| build | run each PBI's worker on its route, wave by wave | the change and `docs/pbi/reports/<report>.md` |
| verify | triage the report, run `verify.py`, judge and record each PBI | `docs/pbi/verify/<report>.json` |
| review | review the whole diff once all PBIs pass | `docs/pbi/reviews/final.review.md` |
| fix | write a rework note and send the PBI back to build | `docs/pbi/PBI-<n>.rework-<k>.md` |
| finish | promote refs, collect stats, write the completion report | the report |

Build, verify and fix repeat per PBI until each PBI passes or stops. Review runs once, after all PBIs; a confirmed finding (from review, or from the security stage when it runs) sends its PBI back to fix. The rework limit is the template's `fix.max_rework`.

## Approve

After route and its gates, show the user the PBI list and wait for approval. Per PBI: id, title, tier, route head, review level, the reason, dropped steps, dependencies, quality warnings. Also:

- one line per stage from the flow table: the component picked, or the fallback with its reason
- the priority-template line from `prepare.py`, and the warning when a template overrides the opus tier
- open assumptions from the design stage, and expired priority applications (`appendix/priority-templates.md`)
- the question for each optional stage with an `ask-user` row placed after route or later (security, by default): whether to run it this time (`appendix/optional.md`)
- the model-check chart and its rules, when that stage ran

## One PBI

- [ ] If the tier is opus and you are not running on Opus, get a design review first (`prompts/design-review.md`).
- [ ] Right before the launch: `python docs/pbi/tools/unchanged.py save PBI-<n> <files not to touch>`.
- [ ] Launch the route head (`runners.md`). Name the worker's description `PBI-<n> <short text>`.
- [ ] Triage the report, run `verify.py`, judge each acceptance criterion, read the diff (`appendix/verify.md`).
- [ ] Decide pass, rework, tier up or hold, and record it once with `record_run.py`.
- [ ] Update the PBI's row in STATUS.md.

Run PBIs with no unfinished dependency together as a wave, at most 5 at once, never two that touch the same file. After each wave, measure the context (`appendix/handoff.md`).

## Keep the context small

Everything in your context is re-read on every later turn, which makes the orchestrator the largest cost of a run.

- Every subagent writes details to a file and returns a few lines and the path: workers the 5 lines in `worker.md`, reviewers counts and one line per finding.
- Scripts do the mechanical steps (`prepare.py`, `verify.py`, `record_run.py`). Read only their tables.
- Send long output (tests, builds, `git diff`) to `docs/pbi/verify/` and look at the exit code and the last 20 lines.
- Check a file's size with `wc -lc` before reading it; over 500 lines, use Grep or offset and limit.
- Write a PBI once and pass its path. Write multi-line text with the Write tool, not a heredoc.
- Batch independent tool calls in one turn.

## Working memory

Only when the shared-memory MCP (`search`, `get`, `write`) is connected; details in `appendix/memory.md`.

- You search widely to design; workers read only the refs their PBI lists. Treat facts in the repository's CLAUDE.md files as known and never copy them into refs or PBIs.
- Search at most once each in `repo:<repo>` and `global`, always with a scope.
- A ref holds one fact used by two or more PBIs, a handoff or a review warning. Write all refs in one parallel turn and put their ids in the PBIs' Refs section.
- Without the MCP, or when writes are not allowed, put the facts in the PBI's Notes and say so in the completion report.
