# Appendix: episodes, roles and their files

The contract between the conductor (`conductor.py`), the designer (`designer.md`) and the roles the designer launches. Every role hands over through files at fixed paths; a reply may be empty. Examples of every JSON are in `examples/roles/`.

## Roles

| Role | Launched by | Model | Writes |
|---|---|---|---|
| Caller (Claude Code, Codex, Grok …) | the user | the user's session | nothing but `conductor.py` calls and answers |
| Conductor (`conductor.py`, a script) | the caller | none | `docs/pbi/conductor/run.json`, the episode prompts, `answers/<seq>-<kind>.md` |
| Designer (`agents/atelier-designer.md`) | the caller (agent) or `conductor.py run` (headless; always when `roles.designer.runner` is codex or claude); description `ATELIER.<run>.<seq> <episode>` (`<run>`: the first 8 characters of `run_id`, printed by the conductor) | `roles.designer` (opus on agent), from run, repo or home (`appendix/setup.md`, `## roles`) | the spec, the PBIs, `docs/pbi/waves.json`, records, the episode result, the `.alive` file |
| Worker (`agents/pbi-worker.md`) | the designer; description `PBI-<n> <short text>` | the PBI's `route.json` | the change and `docs/pbi/reports/<report>.md` |
| Reviewer (`agents/atelier-reviewer.md`) | the designer, one per report, once every worker of the wave has returned; description from `prompts/pbi-review.md` | `roles.reviewer`: `deep` (opus) when the review stage is deep, else `other` (sonnet, effort high); never the model that built it. A stage with a runner other than agent runs through `role_run.py` (`runners.md`, `## Roles on a runner`) | `docs/pbi/reviews/<report>.review.json` |
| Tester (`agents/atelier-tester.md`) | the designer; description from `prompts/tester.md` | `roles.tester` (sonnet); a runner other than agent runs through `role_run.py` | `docs/pbi/verify/tests/<name>.test-result.json` |

## Launch

- `agent` (default; the caller is a Claude Code main session): the caller launches `atelier-designer` with the Agent tool. When the Agent tool runs in the background, the caller calls `done` only after the completion notice.
- `headless` (Codex, Grok, or running in the background): `conductor.py run` (mostly `--detach` and `wait`) launches the designer with `claude -p`, or with `codex -a never exec --json` when `roles.designer.runner` is codex. The designer is the main session there, one level shallower.
- A designer whose runner is not `agent` always runs headless: `next` prints `launch: headless (designer runner <runner>)` and `then: ... run --detach` even when the run's launch is `agent`.
- A codex designer has no Agent tool (its prompt says `Agent tool: no`): it launches every role and worker through the scripts of `runners.md` (`## Without the Agent tool`). Its sandbox is `danger-full-access` unless `roles.designer.permission_mode` names another codex sandbox; `workspace-write` blocks the network and the folders outside the repository, so dsh workers and nested codex runs fail there. Its token counts go to the episode's `history` entry (`tokens`), with no cost, and its context probe prints `context=unknown` (`## Kiln`).
- `inline` (fallback, neither nested agents nor `claude`): the caller follows `designer.md` itself, asks the user directly instead of ending with a question, and uses `appendix/handoff.md` for `/clear` and resume.
- When you need the Agent tool and do not have it, end the episode with status `error` and reason `no-agent-tool`. The conductor turns it into a question (kind `decision`) that offers `run --launch headless --detach`.

## Depth

- Caller 0 → designer 1 → worker, reviewer, tester 2 → the children of a skill a reviewer or tester calls 3.
- An agent at depth 3 has no Agent tool and cannot launch anything below it.
- When a skill's child would need its own child, do not use that skill; run the built-in fallback (`appendix/review.md`, `appendix/tests-at-end.md`).
- A headless designer is one level shallower, so every role below it is too.

## Fallback when a definition is missing

- When `atelier-reviewer` or `atelier-tester` is not a subagent_type of the Agent tool, launch with subagent_type `general-purpose` and put this as the first line of the prompt: `Read <skill>/agents/atelier-<role>.md and follow it as your instructions.`
- The conductor does the same for the designer (`warn agents: atelier-designer not installed; using fallback`); headless, it builds `--agents` from the definition file.

## Episode result

Write the file on the prompt's `Result:` line as your last action, schema `atelier.episode/1`. It is `docs/pbi/conductor/episodes/<run>-<seq>-<episode>.result.json` (`<run>`: the first 8 characters of `run_id`) for every episode, headless or agent, so a designer of an earlier run (even one `start --force` could not stop) cannot answer for a new one; always use the path the prompt gives:

- `episode`, `seq`: from the prompt.
- `status`: `done`, `question`, `handoff` or `error`.
- `summary`: up to 5 lines, 200 characters each. Your reply is these lines only.
- `next`: with `done`, `{"episode": ..., "wave": n, "pbis": [...]}`; `wave` must be in `docs/pbi/waves.json`; `pbis` only for a rework build.
- `question`: with `question`, `{"kind", "path", "summary", "check"}`. It may also carry an optional `topic` (an identifier such as `omm` or `design-approval`); the values and which question takes which are in `appendix/events.md`. Kinds: `approve`, `rework-limit`, `destructive`, `send-approval`, `decision` (a grill round is one) and `full-run` (whether to run the whole E2E suite once, `appendix/tests-at-end.md`).
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
| build | build, test, review, finish |
| test | build, review |
| review | build (with `pbis`), finish |
| finish | none (closes the run) |

A missing or broken result, or a `next` not in this table, counts as a failure: the conductor relaunches the same episode without `Continue from:` (`designer.md`, `### Re-entry`), and asks after two in a row.

A `PBIs:` build carries a `Sent by:` line naming the episode that sent those PBIs back (`test` or `review`); its next must be that episode, and the conductor refuses anything else.

## Questions

| Kind | When | Next launch |
|---|---|---|
| `approve` | the PBI list (`check: design-record` with `confirm` `html` or `md`; no `check` with `confirm` `off`) | `Answer:` (approved, or a revise request) |
| `rework-limit` | a PBI at the rework limit or a redesign | `Answer:` and `Continue from:` |
| `destructive` | before deleting data, a force push, a change outside the repository | `Answer:` and `Continue from:` |
| `send-approval` | right before jevlint sends file contents out | `Answer:` and `Continue from:` |
| `decision` | anything an appendix says to ask the user | `Answer:` and `Continue from:` |

A question file holds only the text for the user. The command for the answer is printed in the `then:` line of `present.py`.

An answer to a conductor question with no state to continue from (`launch-failure`, `handoff-loop`) arrives on a `Conductor answer:` line, not `Answer:`. A conductor question (`launch-failure`, `handoff-loop`, or the `decision` for `no-agent-tool`) has four sections, `## Summary`, `## Before`, `## After` and `## Decisions`, in fixed English sentences.

A precondition of `answer`: when `confirm` is not `off` and `docs/pbi/presentations.jsonl` has no `shown` line with the same run_id and seq, it exits 2 with the one line `error: not shown: run python <skill>/present.py <path> --kind <kind> first`.

## Done preconditions

`done` refuses an episode that lacks what its kind must leave behind. The table has one row per entry of `DONE_REQUIRES` in the conductor.

| Episode | Must be there |
|---|---|
| any episode (design, or a build re-approval) that ends with a question of kind `approve`, `confirm` not `off` | `check: design-record` on the question, and the `reply-meta-` lines in the draft (run `design_record.py markdown` last) |
| any episode that ends with a question of kind `approve`, `confirm` is `off` | no `check` on the question |
| finish | `docs/pbi/REPORT.md`, or `report_body` in the result |

When one is missing, `done` exits 2 with the one line `error: missing <what>` and relaunches the same episode; two in a row make a `launch-failure` question. A hash that does not match still exits 12, as before.

## Status

`done` on a headless episode whose designer or runner is still alive (and ours) settles nothing: it prints `state: running` and `then: wait --timeout 300` and exits 2; only the runner settles a headless episode.

`status` and `wait` add `stale: no live runner` and `relaunch: next --relaunch` to a `running` run whose `runner_pid` and `child_pid` are both dead; `wait` returns at once in that case.


## Review result

`docs/pbi/reviews/<report>.review.json`, schema `atelier.review/1`: `report`, `pbi`, `reviewer.model`, `verify.failed` (numbers), `criteria` (per number `pass`, `fail` or `deferred` with the reason), `findings` (`critical`, `major` or `minor`, file, line, why, repro), `recommendation` (`pass`, `rework`, `tier-up` or `hold`), and `built` (`{runner, model}`, copied from `Built on:`).

## Test result

`docs/pbi/verify/tests/<name>.test-result.json`, schema `atelier.test-result/1`: `kind` (`wave`, `survey`, `final` or `full`), `commands` (command and exit code), `failing` (test, file, pbi, `cause` `intended`, `regression` or `unknown`, why), `recommendation` (`pass`, `rework` or `needs-designer`). The tester's `cause` is a proposal.

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
- A test result: you decide each `cause`. `intended` goes to the test stage; `regression` sends its PBI back to build.

## Kiln

- At every boundary, run the command on the prompt's `Context probe:` line (`agent_stats.py --ctx --agent ATELIER.<run>.<seq> ` for agent, `--ctx --session <id>` for headless), then `kiln_log.py` (`appendix/kiln.md`).
- `suggest`, `compact` and `handoff` all mean: write the state tablet, then end with status `handoff`. Never use `/compact`.
- When the probe prints `context=unknown` twice in a row, hand off after every 3 PBIs you reconcile.
- The conductor relaunches the same episode with `Continue from:` at once; no `/clear` and no resume.
