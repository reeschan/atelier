# Episode: design

The steps of the design episode, from Start to the approval of the PBI list. The approval itself is `episode/approve.md`.

## The episode

`## Start`, then understand, the design decision, map, lint-survey, design, plan, route and its gates. At the end of plan write `docs/pbi/waves.json` (`{"waves": [{"n": 1, "pbis": [...]}, ...]}`). Where the design decision says "Otherwise" ask, where an `ask-user` option asks, end with a question (kind `decision`); the lint send approval is kind `send-approval`. Then the approval (`episode/approve.md`): end with kind `approve`. Once approved, end with `done`, next `{"episode": "build", "wave": 1}`. When STATUS.md has a PBI at `hold (stale; …)` (a re-approval after `stale`, which reached design as a revise request), redo only what the answer's text asks, raise the revision, and end with kind `approve`; once approved, the next is `{"episode": "build", "wave": <the wave of that PBI in docs/pbi/waves.json>}` without `pbis`.

## Start

Restate the user's goal, the problem it solves and the smallest change that reaches it in two or three lines: in the spec, in the summary of the design episode, and in the completion report.

1. Run `python <skill>/flow.py docs/pbi` (create `docs/pbi/` first). It resolves the template (`docs/pbi/workflow.json`, then home, then bundled; the default is `jev-routed`) and prints one row per candidate: stage, component, invoke, where, memory, when, status, doc. When the prompt's `Workflow:` names a template, add `--workflow <name>` here and to every later `flow.py` call (`--decide` too), and write it in STATUS.md.
   - On `error` lines, run again with `--workflow jev-routed`, and note the fallback and each error in one line in the approval list.
2. Write `docs/pbi/STATUS.md` (format in `appendix/engine.md`). Update one line with Edit each time a state changes.
3. Copy `<skill>/worker.md` to `docs/pbi/worker.md` and `<skill>/unchanged.py` to `docs/pbi/tools/unchanged.py`. External runners cannot read outside the repository.
4. Run the start-time sync of priority templates (`appendix/priority-templates.md`).
5. Write the missing run copies from the template: `tests-at-end.json` (`appendix/tests-at-end.md`), `kiln.json`, `lint.json` (`"enabled": false` turns an option off), and `checks.json` with the project's format and lint commands (`appendix/plan.md`, `## Format and lint first`). Run `python <skill>/tools_check.py` and keep its rows that are not `ok` for Approve.

## Run the stages

Go through the stages in this order. Take the design decision (`appendix/design.md`) right after understand; map and lint-survey run only when it is run. For each stage, take the first row that is usable and whose `when` holds (`appendix/engine.md`), follow its `doc`, and write its stage record. Then run every usable row of the optional stages placed after it (`<stage> after <stage>` rows, `appendix/optional.md`), then of the gates after it; after route, `flow.py docs/pbi --decide` says which PBIs each gate row runs for.

| Stage | Default component does | Artifact |
|---|---|---|
| understand | look at the code and memory in scope | `docs/pbi/notes/` |
| design | decide whether to design; write the spec | `docs/specs/<slug>.md` |
| plan | split into PBIs, write facts, briefs and waves | `docs/pbi/PBI-<n>.md`, `.facts.json`, `.brief.md`, `docs/pbi/waves.json` |
| route | Jev gate 1 and the decision table, through `prepare.py` | `docs/pbi/PBI-<n>.route.json` |
| build | run each PBI's worker on its route, wave by wave | the change and `docs/pbi/reports/<report>.md` |
| verify | the reviewer runs `verify.py` and writes its JSON; you reconcile and record | `docs/pbi/verify/<report>.json`, `docs/pbi/reviews/<report>.review.json` |
| review | review the whole diff once, after all PBIs and the test stage pass; write the fix list | `docs/pbi/reviews/final.review.md`, `docs/pbi/fix/round-1.json` |
| fix | fix the whole fix list at once (fix PBIs in one wave), then the failing E2E specs only | the fix PBIs, `docs/pbi/fix/round-<r>.json` |
| finish | promote refs, collect stats, write the completion report | the report |

Build, verify and fix repeat per PBI until each PBI passes or stops. With tests at the end on, the `e2e` episode (a script) and then the test stage of `appendix/tests-at-end.md` run after the last wave; lint-recheck (`after verify`) runs after that, before review. The running part of the Whole check of `appendix/review.md` is the tester's kind `final`; you reconcile its result. Review runs once, after all PBIs; a confirmed finding (from review, or from the security stage when it runs) sends its PBI back to fix. The rework limit is the template's `fix.max_rework`.

## Working memory

When the shared-memory MCP is connected (`designer.md`, `## Working memory`):

- Search at most once each in `repo:<repo>` and `global`, always with a scope.
- A ref holds one fact used by two or more PBIs, a handoff or a review warning. Write all refs in one parallel turn and put their ids in the PBIs' Refs section.
