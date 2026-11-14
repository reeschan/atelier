# Appendix: rework and stopping

Read this when the fix stage picks `fix.rework` (verify or the final review decided rework), or a stop condition is met.

## Rework

The limit is the template's `fix.max_rework` (2 in the bundled templates). Each rework starts a new worker from the head of the same route.

Reviews have their own limit: a PBI is reviewed at most twice, its first report and one rework, whatever `fix.max_rework` says. `role_results.py bundle` prints `round <k>/2` on each row, and `limit` on a row whose second review still does not pass. On `limit`, do not rework: stop the PBI (`## Stop`). Since every report is reviewed, a PBI gets at most one rework after a failed review, even when `fix.max_rework` is 2.

1. Write `docs/pbi/PBI-<n>.rework-<k>.md`:

   ```markdown
   ## Rework <k>: PBI-<n>

   - Criterion <number>: fail
     - Now: <what happens; the error log or the line>
     - Expected: <what passing looks like>
     - Repro: <a command or steps that show the failure>
   - Out-of-scope change: <file and what to revert, if any>
   ```

   For a test PBI of the test stage, list only the failing case ids, each with the failing assertion and its output, and add `Keep the other cases as they are.` (`appendix/tests-at-end.md`, `## Test stage`).

2. Record the attempt with its cause (`appendix/cases.md`) if you have not yet.
3. Check the note with the Jev gate `rework_quality` (names_condition, states_current_and_expected, has_repro; `appendix/jev.md`). Rewrite any item under 0.6 before sending.
4. Launch it with the prompt of `runners.md`, report name `PBI-<n>.rework-<k>`, and add one to the PBI's rework count in STATUS.md.

## Stop

Stop a PBI when it still fails after the last allowed rework, when its bundle row has `limit`, or when it needs a third continue. Raise its tier, rewrite the PBI or build it yourself only when the user picks that. An unusable runner is not a stop of this kind: it has its own question (`runners.md`, When a runner is unusable).

- PBIs of the same wave that do not depend on it keep running; PBIs that depend on it are held.
- End the episode with a question (kind `rework-limit`) once every running attempt in the same wave has returned and been reconciled; do not end the episode with an attempt still running. Lay it out as the `rework-limit` row of `appendix/questions.md` (`## What goes in each kind`). A stale case (a target file changed after the design) is the same kind, `rework-limit`, laid out as the stale row of that table (each of its columns differs from the plain row, not only the summary).
- Raising the tier: `python <skill>/prepare.py docs/pbi --only PBI-<n> --tier <tier>` rewrites its `route.json` from the routing in force at that time; start from the head of the new route.
