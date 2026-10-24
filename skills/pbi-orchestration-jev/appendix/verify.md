# Appendix: verify one PBI

Read this when the verify stage picks `verify.builtin`: after a worker reports, for every attempt.

## Triage the report (gate 2)

Sort by the report's status line and by the exit codes from `verify.py`. Ask the Jev gate `report` only when the two disagree: DONE with a failing verify, or BLOCKED with every verify passing (`appendix/jev.md`).

- `DONE`: go on to `## Check`.
- `DONE_WITH_CONCERNS`: go on to `## Check`, and read the concerns in the details file.
- `NEEDS_CONTEXT`: supply what is missing and tell the same worker to continue (SendMessage).
- `BLOCKED`: read the cause, then continue, rework or stop (`appendix/rework.md`).
- A continue is not a rework. Allow 2 per PBI; needing a third is a stop (`appendix/rework.md`).

## Check

Do not take the worker's report, or Jev's triage, on trust.

1. Run the verify commands and read only the table. Read a log, with Grep or tail, only for the numbers whose output you need.

   ```bash
   python <skill>/verify.py PBI-<n> --name <report> [--only 2,4]
   ```

   When `verify.py` cannot read the commands (exit code 2), run them in one Bash call, each redirected to its own log.
2. Judge each acceptance criterion: pass or fail.
3. Look at `git diff --stat`, then read the diff. List only the problems that should block the merge: file, line, why, how to reproduce.
   - A PBI with review `deep`: read the whole diff, focused on authentication, permissions, input validation, how data can be lost, and contracts between modules.
   - A diff over 300 lines, and a `deep` PBI when you are not running on Opus: give the review to an Opus `pbi-worker` subagent with `prompts/pbi-review.md`. Use its result as input, not as the decision.
4. Passing criteria do not prove the absence of bugs that the criteria cannot catch. When you find one, put a small script that shows it in `docs/pbi/tools/` and use it as the repro in the rework note.

## Decide and record

Decide one of pass, rework, tier up or hold, and record it right away, once:

```bash
python <skill>/record_run.py <report> --attempt <position in the route> --outcome pass|rework|tier-up|hold --findings <critical>,<major>,<minor>
```

- Pass only the outcome and the finding counts; the script reads everything else from the logs.
- To tier up, first rewrite the route with `prepare.py --only PBI-<n> --tier <tier>`; `record_run.py` reads the new `route.json`.
- When its output says the archive copy failed, keep working and mention it in the completion report.
- When it prints `memory violation`, the worker used shared memory beyond what the template allows. The result still counts as judged; write the runner and model in the PBI's STATUS.md note, and when the same runner does it again, route the rest with `--skip <model>`.
- A verify command that fails because the PBI was written wrong is your mistake: fix the command and record, without rework.
- Rework only for a failing acceptance criterion, a change in the "Do not touch" list, or a bug you can reproduce. Never for taste or wording (`appendix/rework.md`).
