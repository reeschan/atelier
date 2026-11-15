# Appendix: the fix stage

Read this in the `fix` episode (the prompt says `Lead: fix`, or `Escalated:` when the lead passed a point to you), and in the review episode when you write the fix list. The fix stage picks `fix.list`; a home template copied before this stage existed names `fix.rework` there, which runs the same way. The design is `docs/specs/fix-stage-once.md` in the repository.

The final review runs once. Everything it confirmed is written into one fix list, and this stage fixes the whole list at once: not PBI by PBI, and not back through build and review. After the fixes, only the fast checks and the review of the fix's own diff run; the final review is never run again. E2E runs once at the very end, after this stage, and only the specs that fail come back here.

## The fix list

`docs/pbi/fix/round-<r>.json`, schema `atelier.fixes/1`. Round 1 is written by the review episode (`appendix/review.md`, `## Findings`); a later round by this stage from a failed E2E round (`## After E2E`).

```json
{"schema": "atelier.fixes/1", "round": 1, "source": "review", "tier": "sonnet",
 "checks": ["pnpm tsc --noEmit", "pnpm run test:unit"],
 "items": [
  {"id": "F1", "severity": "major", "file": "src/cart.ts", "line": 42, "files": [],
   "what": "a negative quantity is accepted", "fix": "reject quantity < 1 with the error of spec §3",
   "check": "node scripts/check-cart.mjs", "pbi": "PBI-003", "decision": "fix", "from": "review"},
  {"id": "F2", "severity": "minor", "file": "src/cart.ts", "line": 10, "what": "the comment names the old rule",
   "fix": "say the 1-item minimum", "pbi": "PBI-003", "decision": "ask", "recommend": "fix", "from": "review"},
  {"id": "F3", "severity": "major", "file": "docs/adr/0007-cart.md", "line": 12,
   "what": "the code rounds per line, the ADR says per order", "fix": "code: round per order / adr: a new ADR that supersedes 0007",
   "pbi": null, "decision": "ask", "recommend": "code", "from": "review"}
 ]}
```

- One item is one finding: the file and line, what is wrong, and the fix in words a worker can follow without the review. `files` names the other files the fix must touch. `pbi` is the PBI whose Targets name the file, or `null`: a file no PBI owns (a spec, an ADR, a shared config) is fixed here like any other.
- `checks` are the type check and the unit test commands that every fix runs (take them from the PBIs' verify commands). `check` is the command that shows the item is fixed (the review's repro), or `null`. Never an E2E spec or the whole suite: E2E runs only in the `e2e` episode.
- `decision`: `fix` (critical and major always), `skip`, or `ask` (every minor, with `recommend` `fix` or `skip`; a conflict with an accepted decision record, with `recommend` `code` or `adr`).
- `tier` is the tier that builds the fix PBIs (`sonnet` unless a fix needs the reasoning of the top tier).

## Steps

1. **Ask once.** When an item says `ask`, ask before any worker starts: one question of kind `decision` (`appendix/questions.md`, the fix-list row), one `ask` per item with the recommended choice first. When the answer comes back, write each answer into the item's `answer` and set `decision` to `fix` or `skip`. An ADR conflict always becomes `fix`: rewrite its `fix` to the way the user chose, either the code change, or a new decision record in the repository's decisions folder that supersedes the old one (then the item's `file` is the new record). No item may stay `ask`; `fix_plan.py` writes nothing while one does, so that every fix goes into one wave.
2. **Plan.** Run `python <skill>/fix_plan.py plan docs/pbi`. It groups the items that share a file, splits the groups over at most three workers so that no two workers touch the same file, writes one fix PBI per worker (`- Fix round:` and `- Fix items:` lines, Targets, Do not touch, one acceptance criterion per item, the format and lint of `docs/pbi/checks.json` and the `checks` as verify commands), adds one wave (`kind: fix`), and copies the Targets as they are before the fix to `docs/pbi/fix/round-<r>/base/`. Read only its `exception:` lines: fix the item in the list and run it again. Then run its `then:` lines (the work tickets, and `prepare.py --only <the fix PBIs> --tier <tier>`).
3. **Build.** Launch the fix PBIs of the wave together, with `worker.md` as usual (`runners.md`). A fix PBI may name a test file in its Targets (a test that asserts too little, a spec broken by design): leave that file out of the worker's `Test files you must not touch` line.
4. **Review the fix only.** Each fix PBI gets its own review (`prompts/pbi-review.md`): the reviewer reads `python <skill>/fix_plan.py diff docs/pbi PBI-<n>` (the change since the copy) instead of the whole diff of its files, and judges each item's criterion. Bundle the reviews as in any wave (`appendix/episodes.md`).
5. **Fast checks.** With tests at the end on, the tester of the wave runs the deferred commands (kind `wave`, `appendix/tests-at-end.md`, `## End of a wave`) for the fix PBIs; that is where the unit tests run. Reconcile it.
6. **Rework.** A fix PBI that fails verify or its review goes back as a rework of that fix PBI (`appendix/rework.md`), inside this episode. The limits are the same (two reviews per PBI, `fix.max_rework`). At the limit, do not stop the run and do not ask: the items it still fails go to the completion report as `left (limit)`, and the stage goes on.
7. **Record.** Write the fix PBIs in the table of STATUS.md (Note: `fix round <r>: F1, F3`) and one stage record: `stage fix: fix.list run (round <r>, <n> items, <k> workers) fixed <a>, left <b>, skipped <s>`.
8. **End** with `done`, next `e2e` when the run has E2E (`investigate` or `final` in `docs/pbi/tests-at-end.json`, tests at the end on), otherwise `finish`. A point you cannot decide from the list, the answer or the requirement (which of two fixes the spec means, whether a finding is real after all): next `{"episode": "fix", "needs_designer": "<the point>"}`. With `Escalated:` you are the designer: decide that point, write it into the list and the state, and end with next `fix` to hand the stage back.

Never run the final review again, and never send a finding back to build: build waves are over once the review starts.

## After E2E

The `e2e` episode ends with next `fix` when specs failed and reruns are left (`appendix/tests-at-end.md`, `## E2E`). Then this stage runs again with a new round:

1. Read `docs/pbi/verify/tests/e2e.test-result.json` and its failing rows. `intended` (a `Breaks by design:` line names it): an item on the spec, to update it to the new behaviour. `regression`: an item on the code the spec shows broken. `unknown`: start the tester with kind `investigate` on those rows first, and decide from its proposal. A `full suite` row: read its log, and put the specs that failed into `rerun`.
2. Write `docs/pbi/fix/round-<r>.json` with `source` `e2e`, `e2e_round` (the round of `docs/pbi/verify/e2e.rounds.json` that failed) and `rerun`. Items with an unclear cause that needs the user are `ask` as in step 1 above; most are not.
3. Go on from step 2 of `## Steps`. The next E2E round runs only the specs that failed and those in `rerun`.

## Re-entry

A fix episode without `Continue from:` reads the newest fix list, `docs/pbi/STATUS.md`, `docs/pbi/reports/`, `docs/pbi/reviews/` and `docs/pbi/runs.jsonl`, and starts at the first unfinished step: an `ask` with no answer → ask; a `fix` item with no fix PBI → plan (`fix_plan.py plan` keeps a round already in `docs/pbi/waves.json`, and writes nothing twice); a fix PBI with no report → its worker; a report with no review → its review. Never record again an attempt already in `docs/pbi/runs.jsonl`.
