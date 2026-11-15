# Episode: review

The steps of the review episode: the final review of the whole diff, once per run, and the fix list.

## The episode

The final review, once per run (a reviewer runs the review skill, `appendix/review.md`), reconcile it, and write every confirmed finding into one fix list, `docs/pbi/fix/round-1.json` (`appendix/fix.md`): critical and major to fix, minors and ADR conflicts to ask. A `heavy-ask` row runs only after `heavy_check.py` finds the diff deep and the user says run on its question page (`appendix/review.md`, `## Heavy review`); that page is the only question of this episode. Never send a finding back to build. A finding the user already answered "report only" at the lint recheck stays in the report (`appendix/lint.md`, `## Lint recheck`). End with `done`: next `fix` when the list has an item to fix or ask, else `e2e` (the run has E2E) or `finish`.
