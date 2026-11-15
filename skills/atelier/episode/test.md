# Episode: test

The steps of the test episode. It runs on its lead (the prompt's `Lead:` line); the designer runs it only for a point the lead handed over (`Escalated:`).

## The episode

Runs on its lead (the prompt's `Lead:` line), not on you, unless the prompt has `Escalated:`. The lead: the tester's survey (kind `survey`), `tests_plan.py` and its exceptions, the test PBIs through workers and reviewers, the tester's final run (kind `final`: the verify and deferred commands, no E2E spec), reconcile it, then lint-recheck (`appendix/tests-at-end.md`, `## Test stage`). End with `done`, next `review`, or `build` with the wave and `pbis` of the PBIs a regression sends back. A point the lead cannot decide: next `{"episode": "test", "needs_designer": "<the point>"}`. With `Escalated:` (you are the designer): decide that point, write the decision in the state, and end with next `test` to hand the stage back (or `build` or `review` when your decision settles the stage).

The test PBIs go through `episode/one-pbi.md` like any PBI. Lint-recheck follows `appendix/lint.md` (`## Lint recheck`), read when the stage runs.
