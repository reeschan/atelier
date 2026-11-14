# Appendix: final review and security review

Read this when every PBI has passed, and the review stage or its gate picks `review.code-review`, `review.builtin`, `gate.security-review`, or an external review component (`appendix/engine.md`, `## External components`). A `read-skill` review component (such as `review.pstack-interrogate`) runs as in `appendix/engine.md` and writes the same result file (see `## Skills that report without severity` below).

## Whole check (always first)

The lint recheck (`appendix/lint.md`) has run before this, and its confirmed regressions are reworked first. Whatever component is picked, look at the whole `git diff --stat` and read the parts that cross PBIs (shared types, settings, callers). The running part is the tester's whole check (`prompts/tester.md`, kind `final`): it runs every PBI's verify commands once more, with output to files as in `appendix/verify.md`, then the deferred commands. Reconcile its test-result. The stat and the parts that cross PBIs stay yours.

## Gate

1. Run these from the repository root. `<base>` is the branch the work will merge into (usually `origin/main`). The PBIs' changes are not committed yet, so compare the working tree with the merge-base, not `..HEAD`. `git add -N` makes new files show in the diff without staging their content. Name only the new files from the PBIs' targets: `git add -N .` would also count unrelated untracked files.

   ```
   git add -N -- <each new file the PBIs created>
   git diff --numstat $(git merge-base <base> HEAD) > docs/pbi/verify/final.numstat
   python <skill>/adapter_gate.py docs/pbi --numstat docs/pbi/verify/final.numstat
   ```

2. The table has phase, setting, decision and reason per row. A component with `when` `adapter-gate` holds only when its phase (`review` or `security`) says `run`. The `review-skill` row picks the review (`## Pick the review`).
   - `skip`: run no review skill and record the reason.
   - `off`: the phase is turned off in agents.json. Record it.
   - setting: `auto`, `off`, or a skill name from agents.json (`appendix/adapters.md`). A skill name replaces the template's review components for that phase when it is in the skill list.
3. Put the `warn` lines under the table together in one question result (kind `decision`) and end the episode. Inline, pass each one to the user in one line.

## Pick the review

When the `review` row says `run`, the `review-skill` row names what reviews, strongest first:

| decision | What reviews |
|---|---|
| `agents.json` | the skill `adapters.review` names (the user's choice) |
| `repo:<skill>` | the repository's own review skill (a folder named with `review` under `.claude/skills/` or `skills/`); it knows the repository's rules best |
| `lens-review:lens-review` | lens-review, when the repository has no review skill of its own |
| `template` | the template's review rows as they are (`review.code-review`, then `review.builtin`) |

Any of the first three replaces the template's review components for this run, as a skill name in agents.json does. Its reason lists the repository's review rules (`rules: <path>, ...`); put them on the reviewer's `Rules:` line together with the rules from the user's instructions and memory. When the picked skill cannot run (it is not in the skill list, it asks a question, or it changed the tree), record `fallback` and take the next row of this table.

## Run a review skill

1. Take a baseline: `git rev-parse HEAD` and `git status --porcelain -- . ':!docs/pbi/reviews' ':!.claude/worktrees'`. The numstat file must exist first; the pathspec leaves out the result file.
2. Before you start it, check that STATUS.md has a stage record for every stage and optional stage `flow.py` placed before review (lint-recheck included). A missing record means the stage was skipped by mistake: run it now (its fallback when the tool cannot run) and then go on.
   Start the `atelier-reviewer` subagent with `prompts/final-review.md`, filled in. When `reviewer.deep` on your `Roles:` line ends in `@<runner>`, or your prompt says `Agent tool: no`, launch it through `role_run.py` as `final.review` (`final.security`) on that runner and model instead (`runners.md`, `## Roles on a runner`). Put the lint recheck's `report` rows and the path of `docs/pbi/jevlint/recheck.md` on its `Lint recheck` line (`appendix/lint.md`). The built-in `code-review` gets the argument `medium`; the others get none. Base, head and spec go in the prompt, never in the argument.
   - Fill its `Rules:` line: put the repository's review rule files from the `review-skill` row's reason, and, when the user's instructions or memory hold review rules for this repository, write those to `docs/pbi/notes/review-rules.md` and add that path. Write `none` only when there is neither.
   - The reviewer is at depth 2, so the children it starts through a skill are at depth 3 and have no Agent tool. A skill that starts one level of children and lets them work without children of their own runs fine here (lens-review does this, and runs alone when it has no Agent tool). When a skill needs its children to start more children, record `fallback` with the reason and use the next row. With `Agent tool: no`, use only a skill that can run alone.
3. When it returns, take the same two values again. When HEAD or the status changed, discard the result, record `fallback` with the reason, and use the next row.
4. Read line 1 of the result file. `BLOCKED` means the skill asked a question: record `fallback` and use the next row. Otherwise go to `## Findings`.
5. A skill named in agents.json is the user's choice; you do not check where it sends code. With `auto`, security runs only the built-in `security-review`, which stays inside Claude Code.

## Built-in review (`review.builtin`)

The whole check above is the built-in review; nothing more runs. The security phase has no built-in fallback. When `security-review` is not in the skill list, record `fallback` and say in the completion report that no security review ran.

## Skills that report without severity

Some skills prove each finding by a run and give no severity (pstack's `interrogate`: Intent, Findings, Dropped). Write `docs/pbi/reviews/final.review.md` from that output yourself, in the format of `prompts/final-review.md`:

- Each verified finding is one line. It is `critical` when it breaks an acceptance criterion, loses or corrupts stored data, or opens a security hole; otherwise `major`. A finding the skill proved by a run is never `minor`.
- The repro is the finding's command. Keep its output in `docs/pbi/reviews/final.review.repro.md` when it is longer than one line.
- The Intent goes on line 2 as `intent: <one line>`, after the counts. Add the dropped count to the stage record: `findings 0,2,0 dropped 3`.

Reconcile only once every result is in: the tester's test-result, the review skill's result and, when it runs, the security review's result.

## Findings

1. Check each finding yourself: open the file at the line and confirm the claim. A reviewer's report is not proof.
2. A confirmed finding in a file that a PBI's targets name becomes a rework of that PBI (`appendix/rework.md`), counted toward the limit. Review again once every reworked PBI passes.
   - The final review runs at most twice per run. Each run gets its own stage record with its number, `stage review: <component> run #1|#2 (<reason>) findings <c>,<m>,<n>` (`appendix/engine.md`, `## Stage records`); never replace the first line. Before a final review, count those lines: with two already, do not start a third. After the second, rework no critical or major finding more: put those that are left into the findings of the completion report. Minors follow `## Minor findings`.
   - A PBI that has used both reviews gets no rework from the final review either: its findings go into the completion report. This applies when its last bundle row shows `limit`, or when it shows `round 2/2` with a pass (`appendix/rework.md`).
3. A confirmed finding in a file that no PBI owns goes into the findings of the completion report.
4. A remark about taste or wording is not a reason for rework. Drop it. A formatting or lint remark is not a finding either: the format and lint commands run in every PBI's verify (`appendix/plan.md`, `## Format and lint first`).
5. A confirmed conflict between the change and an accepted decision record (an ADR, in the repository's decisions folder) is never dropped, even when the user prefers the code. It ends one of two ways: rework the code, or a new record that supersedes the old one. Writing that record is the user's call: end the episode with a question (kind `decision`). Inline, put it under Waiting on you.

## Minor findings

The confirmed minors of the final review are settled in this run, not handed to the completion report as they are.

1. Once no confirmed critical or major finding is left to rework (fixed, or past the limits above), collect every confirmed minor of the final reviews of this run. A minor whose PBI is already at `fix.max_rework` or at its second review (`appendix/rework.md`, `## Minor fixes`) cannot be fixed: put it straight into the skipped minors with the reason `limit`, and leave it out of the choices. A minor in a file no PBI owns has nothing to rework either: skip it the same way with the reason `no owning PBI`. Ask about the rest in one question (kind `decision`, `appendix/questions.md`): one row per minor with its file, line, what is wrong, the PBI that owns the file, and a recommendation.
   - Recommend `fix` when the fix is small and the minor can mislead a reader or a later change: a wrong name, a stale comment or message, a missing check, a test that asserts too little.
   - Recommend `skip` for taste, wording and style the repository's rules do not ask for.
   - With no confirmed minor, ask nothing.
2. Each minor the user answers `fix` becomes a rework of its PBI (`appendix/rework.md`, `## Minor fixes`). `next` names one wave, so group the fixes by wave: end with next `build`, the first wave with fixes and the `pbis` of its PBIs. The PBI's own review checks the fix; when that build comes back, run no final review again, and send the next wave with fixes the same way. Go on to finish when no wave with accepted fixes is left. Keep the accepted fixes and the waves done in the state tablet, so a later review episode knows what is left.
3. Each minor answered `skip`, and each one skipped before the question (`limit`, `no owning PBI`), goes into the completion report under the skipped minors (`appendix/finish.md`).
