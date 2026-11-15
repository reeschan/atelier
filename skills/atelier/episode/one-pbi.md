# Episode part: One PBI

How one PBI is launched, reviewed and reconciled. The build episode runs it for each PBI of its wave; the leads of the test and fix episodes run it for the test PBIs and the fix PBIs.

## One PBI

- [ ] If the tier is opus and you are not running on Opus, get a design review first (`prompts/design-review.md`).
- [ ] Only before the first attempt of the PBI (never for a rework, a `PBIs:` rework build, or an attempt after a tier up): `python <skill>/design_record.py stale PBI-<n>`. It compares the targets in `PBI-<n>.work.json` with the working tree; files a `depends_on` PBI changes do not count. With a design record it first checks that this ticket and its `depends_on` tickets are the ones recorded; exit 3 means a ticket changed after the approval (an error, below). Exit 0: go on. Exit 7 (it prints `stale<TAB>kind<TAB>path`): do not launch this PBI. Treat it as a redesign:
  1. Keep the state in the file: in `docs/pbi/STATUS.md` set that PBI's State to `hold (stale; 再承認待ち)` and write the stale lines (kind and path) in its Note. The held PBI, its wave and the PBIs still to run are then read from STATUS.md and `docs/pbi/waves.json`, because the approve answer of step 3 carries no `Continue from:`.
  2. Reconcile the PBIs of the wave that do not depend on it, then end the episode with a question of kind `rework-limit` (`state` and the stale lines in the summary).
  3. On the `Answer:` (with `Continue from:`), redo only that PBI's design (the PBI file, `route.json`), run `design_record.py work PBI-<n>`, run `design_record.py write` again with every work ticket (the revision rises). Then rewrite `docs/pbi/notes/approve.md` to match the redone PBI (as `episode/approve.md` and `appendix/approve.md` say), and add the envelope (`design_record.py markdown`) as the last change to that file, so it points at the new revision and record_hash. With the `confirm` adapter `off` there is no design record: run `design_record.py work PBI-<n>` and rewrite `approve.md` only, never `write` or `markdown`, and leave `check` out of the question. Before you ask, change the STATUS.md State to `hold (stale; 再承認を出した、revision <r>)` (with `confirm` `off`, `hold (stale; 再承認を出した)`), then end with a question of kind `approve` again.
  4. The approve answer comes back in a build episode with `Answer:` and no `Continue from:` (approved), or in a design episode (a revise request): `designer.md` (`### Re-entry`) says how each continues. Either way the held PBI goes through `stale` again as a first launch, and a pass or another exit 7 follows this step.
  Any other exit code is an error: stop and report it, never launch.
- [ ] Right before the launch: `python docs/pbi/tools/unchanged.py save PBI-<n> <files not to touch>`.
- [ ] Launch the route head (`runners.md`). Name the worker's description `PBI-<n> <short text>`.
- [ ] When every worker of the wave has returned, launch one reviewer per report (`prompts/pbi-review.md`, `agents/atelier-reviewer.md`; the fallback when it is not installed is in `appendix/episodes.md`). A reviewer whose role value ends in `@<runner>` runs through `role_run.py` (`runners.md`, `## Roles on a runner`). Pass the runner and model that actually built it on the `Built on: <runner> <model>` line.
- [ ] Run `python <skill>/role_results.py bundle --wave <n> --out docs/pbi/reviews/wave-<n>.bundle.json`. On exit 3, redo the review of the failing reports only.
- [ ] Reconcile (`appendix/episodes.md`, `## Reconcile`). Decide pass, rework, tier up or hold, and record it once with `record_run.py`.
- [ ] Update the PBI's row in STATUS.md.

Run PBIs with no unfinished dependency together as a wave (`docs/pbi/waves.json`), at most 5 at once, never two that touch the same file. With tests at the end on, reviewers verify with `--skip-deferred`, and you record at the end of the wave after the tester's kind `wave` (`appendix/tests-at-end.md`, `prompts/tester.md`). A failure whose cause nobody can tell goes to the tester's kind `investigate`; you decide from its proposal and do not run the tests yourself. After each wave, measure your context (`episode/_kiln.md`).
