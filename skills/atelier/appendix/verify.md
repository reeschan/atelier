# Appendix: verify one PBI

Read this when the verify stage picks `verify.builtin`: after a worker reports, for every attempt.

## Triage the report (gate 2)

Sort by the report's status line and by the exit codes from `verify.py`. Ask the Jev gate `report` only when the two disagree: DONE with a failing verify, or BLOCKED with every verify passing (`appendix/jev.md`).

- `DONE`: go on to `## Check`.
- `DONE_WITH_CONCERNS`: go on to `## Check`, and read the concerns in the details file.
- `NEEDS_CONTEXT`: supply what is missing and tell the same worker to continue (SendMessage).
- `NEEDS_CONTEXT` with `Open: kiln`: the worker stopped at its kiln peak and wrote `## State`. Copy the details file to `docs/pbi/reports/<report>.state.md`, then start a new worker on the same route with the same report name and the `Continue from:` line (`runners.md`). It counts as a continue, not a rework, and is recorded once with `record_run.py` when the PBI is judged. Log the stop: `python <skill>/kiln_log.py docs/pbi --role implementer --boundary attempt --phase <report name> --tokens <the attempt's tokens> --action handoff` (`appendix/kiln.md`).
- `BLOCKED`: read the cause, then continue, rework or stop (`appendix/rework.md`).
- A continue is not a rework. Allow 2 per PBI; needing a third is a stop (`appendix/rework.md`).

## Check

The reviewer (`prompts/pbi-review.md`) does this; it runs the verify commands, judges each acceptance criterion and reads the diff. Inline, you do it:

1. Run `python <skill>/verify.py PBI-<n> --name <report> --skip-deferred`, and read only the table; open a log only for the numbers whose output you need.
   - With tests at the end on, `--skip-deferred` leaves those numbers for the end of the wave (`appendix/tests-at-end.md`).
   - When `verify.py` exits 2 (it cannot read the commands), run them by hand in one call, each to its own log.
2. Judge each acceptance criterion: pass or fail.
3. Read `git diff --stat`, then the diff; list only what should block the merge: file, line, why and a repro.
   - A broken rule in a CLAUDE.md file that applies to the changed files is a finding with the cause `convention`.
4. A bug that the acceptance criteria cannot catch gets a small script in `docs/pbi/tools/`, and that script is the repro.

## Reconcile

Reconcile the wave from the bundle table. The designer does this (inline, you do it), once every review of the wave is written:

```bash
python <skill>/role_results.py bundle --wave <n> --out docs/pbi/reviews/wave-<n>.bundle.json
```

- Open `docs/pbi/reviews/<report>.review.json` only for a row with `open`.
- Verify passes or fails by the bundle's `verify` column, which comes from `docs/pbi/verify/<report>.json`; never by the review.
- Exit code 3: redo the review of that report only, then bundle again.
- Confirm each finding by opening the file yourself.
- The rows and the rest of the rules are in `appendix/episodes.md` (`## Reconcile`).

## Decide and record

Decide one of pass, rework, tier up or hold, and record it right away, once:

```bash
python <skill>/record_run.py <report> --attempt <n> [--step <position in the route>] --outcome pass|rework|tier-up|hold --findings <critical>,<major>,<minor> [--cause <cause>,...]
```

- Pass only the outcome, the finding counts and, for rework, tier up and hold, the cause (`appendix/cases.md`); the script reads everything else from the logs.
- `--attempt` is the n-th run of this report and `--step` the route position it ran. Leave `--step` out when they are the same; pass it after a retry of the same step or a step the user picked (`runners.md`, When a runner is unusable).
- To tier up, first rewrite the route with `prepare.py --only PBI-<n> --tier <tier>`; `record_run.py` reads the new `route.json`.
- When its output says the archive copy failed, keep working and mention it in the completion report.
- When it prints `memory violation`, the worker used shared memory beyond what the template allows. The result still counts as judged; write the runner and model in the PBI's STATUS.md note, and when the same runner does it again (with any model), route the rest with `--skip <runner>`. `violation` null means it could not be checked (no readable log, or a `get` whose id the log does not show); note that too.
- A verify command that fails because the PBI was written wrong is your mistake: fix the command and record, without rework.
- Rework only for a failing acceptance criterion, a change in the "Do not touch" list, or a bug you can reproduce. Never for taste or wording (`appendix/rework.md`).
