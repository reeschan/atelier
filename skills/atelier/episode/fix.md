# Episode: fix

The steps of the fix episode. It runs on its lead (the prompt's `Lead:` line); the designer runs it only for a point the lead handed over (`Escalated:`).

## The episode

Runs on its lead (the prompt's `Lead:` line), unless the prompt has `Escalated:`. The lead asks the list's `ask` items in one question before any worker, then `fix_plan.py plan`, the fix PBIs in one wave through workers, reviews of the fix's diff only and the tester's wave run, and reworks within the limits; it never runs the final review again (`appendix/fix.md`). End with `done`, next `e2e` (the run has E2E) or `finish`; a point the lead cannot decide: next `{"episode": "fix", "needs_designer": "<the point>"}`. After a failed E2E round the stage runs again on the failing specs only (`appendix/fix.md`, `## After E2E`).

The fix PBIs go through `episode/one-pbi.md` like any PBI.
