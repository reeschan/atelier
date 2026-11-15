# Appendix: episodes, roles and their files

The contract between the conductor (`conductor.py`), the designer (`designer.md`) and the roles the designer launches. Every role hands over through files at fixed paths; a reply may be empty. Examples of every JSON are in `examples/roles/`. The caller's and the conductor's own parts (how the designer is launched, the run's status, what `done` checks) are in `appendix/conductor.md`.

## Roles

| Role | Launched by | Model | Writes |
|---|---|---|---|
| Caller (Claude Code, Codex, Grok …) | the user | the user's session | nothing but `conductor.py` calls and answers |
| Conductor (`conductor.py`, a script) | the caller | none | `docs/pbi/conductor/run.json`, the episode prompts, `answers/<seq>-<kind>.md` |
| Designer (`agents/atelier-designer.md`) | the caller (agent) or `conductor.py run` (headless; always when `roles.designer.runner` is codex or claude); description `ATELIER.<run>.<seq> <episode>` (`<run>`: the first 8 characters of `run_id`, printed by the conductor) | `roles.designer` (opus on agent), from run, repo or home (`appendix/setup.md`, `## roles`) | the spec, the PBIs, `docs/pbi/waves.json`, records, the episode result, the `.alive` file |
| Worker (`agents/pbi-worker.md`) | the designer; description `PBI-<n> <short text>` | the PBI's `route.json` | the change and `docs/pbi/reports/<report>.md` |
| Lead of an episode (`agents/atelier-designer.md`, the same definition) | the caller or `conductor.py run`, for the test and fix episodes; the prompt says `Lead: test` or `Lead: fix` | `roles.episodes.test` and `roles.episodes.fix`: the first usable step of the routing template's middle tier unless a layer sets it (`appendix/setup.md`, `## roles`); the designer's model when the episode was handed over (`Escalated:`) | the same files as the designer |
| `e2e_run.py` (a script) | `conductor.py run`, for the e2e episode | none | `docs/pbi/verify/tests/e2e.test-result.json`, `docs/pbi/verify/e2e.scope.md`, `docs/pbi/verify/e2e.rounds.json`, the episode result |
| Reviewer (`agents/atelier-reviewer.md`) | the designer, one per report, once every worker of the wave has returned; description from `prompts/pbi-review.md` | `roles.reviewer`: `deep` (opus) when the review stage is deep, else `other` (the routing template's middle tier, sonnet at high effort with the bundled routing). Only a deep review avoids the model that built it: it switches to `other` when `deep` is that model. A stage with a runner other than agent runs through `role_run.py` (`runners.md`, `## Roles on a runner`) | `docs/pbi/reviews/<report>.review.json` |
| Tester (`agents/atelier-tester.md`) | the designer; description from `prompts/tester.md` | `roles.tester` (sonnet); a runner other than agent runs through `role_run.py` | `docs/pbi/verify/tests/<name>.test-result.json` |

## No Agent tool

- A codex designer has no Agent tool (its prompt says `Agent tool: no`): it launches every role and worker through the scripts of `runners.md` (`## Without the Agent tool`). Its token counts go to the episode's `history` entry (`tokens`), with no cost, and its context probe prints `context=unknown` (`episode/_kiln.md`).
- When you need the Agent tool and do not have it, end the episode with status `error` and reason `no-agent-tool`. The conductor turns it into a question (kind `decision`) that offers `run --launch headless --detach`.

## Depth

- Caller 0 → designer 1 → worker, reviewer, tester 2 → the children of a skill a reviewer or tester calls 3.
- An agent at depth 3 has no Agent tool and cannot launch anything below it.
- When a skill's child would need its own child, do not use that skill; run the built-in fallback (`appendix/review.md`, `appendix/tests-at-end.md`).
- A headless designer is one level shallower, so every role below it is too.

## Fallback when a definition is missing

- When `atelier-reviewer` or `atelier-tester` is not a subagent_type of the Agent tool, launch with subagent_type `general-purpose` and put this as the first line of the prompt: `Read <skill>/agents/atelier-<role>.md and follow it as your instructions.`

## Episode result

Write the file on the prompt's `Result:` line as your last action, schema `atelier.episode/1`. It is `docs/pbi/conductor/episodes/<run>-<seq>-<episode>.result.json` (`<run>`: the first 8 characters of `run_id`) for every episode, headless or agent, so a designer of an earlier run (even one `start --force` could not stop) cannot answer for a new one; always use the path the prompt gives:

- `episode`, `seq`: from the prompt.
- `status`: `done`, `question`, `handoff` or `error`.
- `summary`: up to 5 lines, 200 characters each. Your reply is these lines only.
- `next`: with `done`, `{"episode": ..., "wave": n, "pbis": [...]}`; `wave` must be in `docs/pbi/waves.json`; `pbis` only for a rework build. The lead of the test episode adds `"needs_designer": "<the point>"` with next `test` to hand a point to the designer.
- `question`: with `question`, `{"kind", "path", "summary", "check"}`. It may also carry an optional `topic` (an identifier such as `omm` or `design-approval`); the values and which question takes which are in `appendix/events.md`. Kinds: `approve`, `rework-limit`, `destructive`, `send-approval`, `decision` (a grill round is one) and `full-run` (whether to run the whole E2E suite once; only a run started before the full suite moved to the approval asks it).
- `question.path` (every kind but `approve`): the draft is written in the form of `appendix/questions.md`. `done` runs the same check as `lint --kind` on it, and a draft with an error is not accepted (`state: retry`).
- `report_body` (finish only, optional): the completion report's text, when you could not write `docs/pbi/REPORT.md`; the conductor writes it.
- `state`: the path of the state tablet; required with `handoff` and with every question kind but `approve`.
- `report`: the path of the completion report (finish).
- `reason`: required with `error`.

`python <skill>/role_results.py check <file>` checks the result, review and test-result schemas.

## Links

| Episode | `next` it may name |
|---|---|
| design | build (wave 1) |
| build | build, test, review, finish (e2e only in a run started before E2E moved to the end) |
| test | build, review, test (only with `needs_designer` from the lead, or from the designer to hand the stage back) |
| review | fix, e2e, finish (never build: the final review sends nothing back) |
| fix | e2e, finish, fix (only with `needs_designer` from the lead, or from the designer to hand the stage back) |
| e2e | fix, finish (written by `e2e_run.py`; test with `--before-review`) |
| finish | none (closes the run) |

A missing or broken result, or a `next` not in this table, counts as a failure: the conductor relaunches the same episode without `Continue from:` (`designer.md`, `### Re-entry`), and asks after two in a row.

A `PBIs:` build carries a `Sent by:` line naming the episode that sent those PBIs back (`test`, or `review` in a run started before the final review stopped sending back); its next must be that episode, and the conductor refuses anything else.

## Questions

| Kind | When | Next launch |
|---|---|---|
| `approve` | the PBI list (`check: design-record` with `confirm` `html` or `md`; no `check` with `confirm` `off`) | `Answer:` (approved, or a revise request) |
| `rework-limit` | a PBI at the rework limit or a redesign | `Answer:` and `Continue from:` |
| `destructive` | before deleting data, a force push, a change outside the repository | `Answer:` and `Continue from:` |
| `send-approval` | right before jevlint sends file contents out | `Answer:` and `Continue from:` |
| `decision` | anything an appendix says to ask the user | `Answer:` and `Continue from:` |

A question file holds only the text for the user. The command for the answer is printed in the `then:` line of `present.py`.

An answer to a conductor question with no state to continue from (`launch-failure`, `handoff-loop`) arrives on a `Conductor answer:` line, not `Answer:`.

## Review result

`docs/pbi/reviews/<report>.review.json`, schema `atelier.review/1`: `report`, `pbi`, `reviewer.model`, `verify.failed` (numbers), `criteria` (per number `pass`, `fail` or `deferred` with the reason), `findings` (`critical`, `major` or `minor`, file, line, why, repro), `recommendation` (`pass`, `rework`, `tier-up` or `hold`), and `built` (`{runner, model}`, copied from `Built on:`).

## Test result

`docs/pbi/verify/tests/<name>.test-result.json`, schema `atelier.test-result/1`: `kind` (`wave`, `investigate`, `survey`, `e2e`, `final` or `full`), `commands` (command and exit code), `failing` (test, file, pbi, `cause` `intended`, `regression` or `unknown`, why), `recommendation` (`pass`, `rework` or `needs-designer`). The tester's `cause` is a proposal.

## Bundle

`python <skill>/role_results.py bundle --wave <n> --out docs/pbi/reviews/wave-<n>.bundle.json` picks the reports from `docs/pbi/waves.json` and `docs/pbi/reports/` and prints one row per report. It checks by itself, not from the reviewer's word:

- the review JSON is there, fits its schema and judges every acceptance criterion
- `docs/pbi/verify/<report>.json` is there, newer than the report, and covers every verify number but the deferred ones; the `verify` column comes from its exit codes
- `unchanged`: the result of `python docs/pbi/tools/unchanged.py check PBI-<n>`
- the model that built it (from `built`, then `docs/pbi/runs.jsonl`, then `route.json`) differs from `reviewer.model`

A row reads `<report>  verify ok|fail <n,..>  unchanged <state>  review <recommendation>  findings <critical,major,minor>  round <k>/2`, then `mismatch` when the review disagrees with the verify JSON, then `limit` when the second review still does not pass (`appendix/rework.md`), then `open` when you must look. The bundle file is a copy for handing over, not a schema.

## Reconcile

- Exit 3 (an `error` row): redo the review of that report only, then bundle again.
- A row without `open`: record pass from the table alone.
- An `open` row: open its review JSON. Pass or fail of verify comes from `docs/pbi/verify/<report>.json` (the `verify` column), never from the review.
- Then decide pass, rework, tier up or hold, and record once with `record_run.py` (`appendix/verify.md`, `appendix/rework.md`).
- A test result: you decide each `cause`. `intended` goes to the test stage; `regression` sends its PBI back to build; `unknown` goes to the tester's kind `investigate`, and you decide from its proposal.

## Kiln

The designer's part of Kiln (measure, log, act, the state tablet and the handoff) is `episode/_kiln.md`, which every Read first file carries; the lines, inline cool-downs and the implementer chamber are `appendix/kiln.md`.
