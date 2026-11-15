# Appendix: final review and security review

Read this when every PBI has passed, and the review stage or its gate picks `review.code-review`, `review.builtin`, `gate.security-review`, or an external review component (`appendix/engine.md`, `## External components`). A `read-skill` review component (such as `review.pstack-interrogate`) runs as in `appendix/engine.md` and writes the same result file (see `## Skills that report without severity` below).

## Whole check (always first)

The lint recheck (`appendix/lint.md`) has run before this, and its confirmed regressions are reworked first. Whatever component is picked, look at the whole `git diff --stat` and read the parts that cross PBIs (shared types, settings, callers). The running part is the tester's whole check (`prompts/tester.md`, kind `final`): it runs every PBI's verify commands once more, with output to files as in `appendix/verify.md`, then the deferred commands (the unit tests). No E2E spec runs before the final review: E2E runs once at the very end, after the fix stage (`appendix/fix.md`). Reconcile its test-result and put its path on the reviewer's `Test results:` line (`prompts/final-review.md`), so the review sees the unit tests and the checks. The stat and the parts that cross PBIs stay yours.

The final review runs once per run, and nothing it finds goes back to build: every confirmed finding goes into one fix list, and the fix stage fixes the whole list at once (`## Findings`).

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
   - setting: `auto`, `off`, or a skill name from agents.json. A skill name replaces the template's review components for that phase when it is usable (`appendix/adapters.md`, `## Settings`).
3. Put the `warn` lines under the table together in one question result (kind `decision`) and end the episode. Inline, pass each one to the user in one line.

## Heavy review

Read this when the review stage has a row whose `when` is `heavy-ask` (pstack's `interrogate` in the recommended example). It costs several reviews at once, so it runs only when the finished diff is deep work and the user says yes on a question page. It is decided here, after the implementation and right before the final review: never at approval, never from a PBI's review `deep`, never without the page.

1. When the gate's `review` row says `skip` or `off`, the heavy review does not run either. Otherwise, after the `git add -N` of the gate, run from the repository root:

   ```
   python <skill>/heavy_check.py check docs/pbi --base <base> [--workflow <name>] --designer-model <your model family> --usual-model <the family of reviewer.deep> --lang <ja|en>
   ```

   `--lang` is the language of the user's goal. The script measures the working tree against the merge-base (`docs/pbi/` and `.claude/worktrees/` are left out), checks three criteria (a new application from scratch, a new subsystem with its own data model or external interface across 3 or more modules, a new architecture: a deployment target or a dependency swap), and writes `docs/pbi/verify/heavy.json`. A large diff, a `sensitive` PBI or a PBI with review `deep` is not deep by itself.
2. Read its first line:
   - `recorded run|skip ...`: a resume. Use that answer and ask nothing.
   - `verdict none`: no `heavy-ask` row. Go on.
   - `verdict light`: no criterion holds. Run `python <skill>/heavy_check.py record docs/pbi --decision skip --by auto` and go on without asking.
   - `verdict heavy`: open the files that the criteria in `docs/pbi/verify/heavy.json` name and confirm against the diff that the work is deep by that criterion (a new manifest for fixtures, a vendored folder or generated files is not a new application). Not deep: `record --decision skip --by auto --reason "<why>"` and go on. Deep: the question draft is on the `draft` line (`docs/pbi/notes/heavy-review.question.md`), already in the shape of a decision question (`appendix/questions.md`): why the diff looks deep, the numbers of the diff, the reviewers it starts with their models and the expected cost, and two choices, run it or the usual review. Change only its wording if you must, run `design_record.py lint --draft <it> --kind decision`, and end the episode with a question of kind `decision` with that path and the state. Inline, show it with `python <skill>/present.py <draft> --kind decision` and wait for the user's answer.
3. On the `Answer:`: run it → `record --decision run --by user`. Anything else (the usual review, no answer, an unclear one) → `record --decision skip --by user`. `record` refuses `run` unless the page was shown after the check (a `shown` row of kind `decision` in `docs/pbi/presentations.jsonl`), and refuses `run` by `auto`. The answer stays in `docs/pbi/adapters.json` under `heavy-review`, so a resume does not ask again.
4. With `heavy-review.decision` `run`, the heavy row is the final review of this run, ahead of `## Pick the review`; run it as in `appendix/engine.md`, `## read-skill components`. With `skip`, pass over every `heavy-ask` row; `## Pick the review` picks the review as usual (lens-review in the recommended example). Never run a `heavy-ask` row without `run` in that record.
5. Stage record: `stage review: <component> skip (heavy-review: light)`, `skip (heavy-review: not deep, <reason>)`, `skip (heavy-review: the user chose the usual review)`, or `run (heavy-review: the user said run; <criteria>)`.

## Pick the review

When the `review` row says `run` and no heavy review runs (`## Heavy review`), the `review-skill` row names what reviews, strongest first:

| decision | What reviews |
|---|---|
| `agents.json` | the skill `adapters.review` names (the user's choice) |
| `repo:<skill>` | the repository's own review skill (a folder named with `review` under `.claude/skills/` or `skills/`); it knows the repository's rules best |
| `lens-review:lens-review` | lens-review, when the repository has no review skill of its own |
| `template` | the template's review rows as they are (`review.code-review`, then `review.builtin`) |

Any of the first three replaces the template's review components for this run, as a skill name in agents.json does. Its reason lists the repository's review rules (`rules: <path>, ...`); put them on the reviewer's `Rules:` line together with the rules from the user's instructions and memory. `adapter_gate.py` found `repo:` and `lens-review` on disk, and the reviewer holds the Skill tool, so start it without asking `flow.py` first. Take the next row of this table only when the reviewer could not run the picked skill, as `## Run a review skill` describes.

## Run a review skill

1. Take a baseline: `git rev-parse HEAD` and `git status --porcelain -- . ':!docs/pbi/reviews' ':!.claude/worktrees'`. The numstat file must exist first; the pathspec leaves out the result file.
2. Before you start it, check that the stage records (`docs/pbi/stages.md`; a run started before keeps them in STATUS.md, `appendix/engine.md`, `## Stage records`) have one for every stage and optional stage `flow.py` placed before review (lint-recheck included). A missing record means the stage was skipped by mistake: run it now (its fallback when the tool cannot run) and then go on.
   Start the `atelier-reviewer` subagent with `prompts/final-review.md`, filled in. When `reviewer.deep` on your `Roles:` line ends in `@<runner>`, or your prompt says `Agent tool: no`, launch it through `role_run.py` as `final.review` (`final.security`) on that runner and model instead (`runners.md`, `## Roles on a runner`). Put the lint recheck's `report` rows and the path of `docs/pbi/jevlint/recheck.md` on its `Lint recheck` line (`appendix/lint.md`). The built-in `code-review` gets the argument `medium`; the others get none. Base, head and spec go in the prompt, never in the argument.
   - Fill its `Rules:` line: put the repository's review rule files from the `review-skill` row's reason, and, when the user's instructions or memory hold review rules for this repository, write those to `docs/pbi/notes/review-rules.md` and add that path. Write `none` only when there is neither.
   - The reviewer is at depth 2, so the children it starts through a skill are at depth 3 and have no Agent tool. A skill that starts one level of children and lets them work without children of their own runs fine here (lens-review does this, and runs alone when it has no Agent tool). When a skill needs its children to start more children, record `fallback` with the reason and use the next row. With `Agent tool: no`, use only a skill that can run alone.
3. When it returns, take the same two values again. When HEAD or the status changed, discard the result, record `fallback` with the reason, and use the next row.
4. Read line 1 of the result file. `BLOCKED` means the skill asked a question, and a reply that the skill was not found means the reviewer could not load it: in both cases record `fallback` and use the next row. Otherwise go to `## Findings`.
5. A skill named in agents.json is the user's choice; you do not check where it sends code. With `auto`, security runs only the built-in `security-review`, which stays inside Claude Code.

## Built-in review (`review.builtin`)

The whole check above is the built-in review; nothing more runs. The security phase has no built-in fallback. When the `gate.security-review` row is not usable by its status in the flow table (`appendix/engine.md`, `## Picking`), or the reviewer reports `security-review` as not found, record `fallback` and say in the completion report that no security review ran.

## Skills that report without severity

Some skills prove each finding by a run and give no severity (pstack's `interrogate`: Intent, Findings, Dropped). Write `docs/pbi/reviews/final.review.md` from that output yourself, in the format of `prompts/final-review.md`:

- Each verified finding is one line. It is `critical` when it breaks an acceptance criterion, loses or corrupts stored data, or opens a security hole; otherwise `major`. A finding the skill proved by a run is never `minor`.
- The repro is the finding's command. Keep its output in `docs/pbi/reviews/final.review.repro.md` when it is longer than one line.
- The Intent goes on line 2 as `intent: <one line>`, after the counts. Add the dropped count to the stage record: `findings 0,2,0 dropped 3`.

Reconcile only once every result is in: the tester's test-result, the review skill's result and, when it runs, the security review's result.

## Findings

The final review runs once per run. Its stage record is one line, `stage review: <component> run (<reason>) findings <c>,<m>,<n>` (`appendix/engine.md`, `## Stage records`). Before you start a final review, look for that line in the stage records (`docs/pbi/stages.md`, or STATUS.md in a run started before): when it is there (a resume), do not start another one; go on from its result file, and from `docs/pbi/fix/round-1.json` when that is written. A `run #1` line from a run started before this rule counts as that one review.

1. Check each finding yourself: open the file at the line and confirm the claim. A reviewer's report is not proof.
2. Write every confirmed finding into the fix list `docs/pbi/fix/round-1.json` (`appendix/fix.md`, `## The fix list`), one item each: the file and line, what is wrong, how to fix it, and the command that shows it fixed (the repro, when it is a command). The confirmed findings of the security review (the gate after review) go into the same list. Never send a finding back to build, whatever file it is in: a file no PBI owns (a spec, an ADR, a shared config) is fixed by the fix stage like any other, and needs no owning PBI.
   - critical and major: `decision` `fix`.
   - minor: `decision` `ask` with a recommendation (`## Minor findings`).
   - a finding the user already answered "report only" at the lint recheck: leave it out of the list; it stays in the report (`appendix/lint.md`, `## Lint recheck`).
   - Set the list's `checks` to the type check and the unit test commands of the PBIs' verify commands, so every fix runs them, and `tier` to the tier that builds the fixes (`sonnet` unless a fix needs the top tier).
3. A remark about taste or wording is not a finding. Drop it. A formatting or lint remark is not a finding either: the format and lint commands run in every PBI's verify (`appendix/plan.md`, `## Format and lint first`).
4. A confirmed conflict between the change and an accepted decision record (an ADR, in the repository's decisions folder) is never dropped, even when the user prefers the code. It ends one of two ways: fix the code, or write a new record that supersedes the old one. Writing that record is the user's call: make it an item with `decision` `ask` and `recommend` `code` or `adr`; the fix stage asks it together with the minors, in its one question. Inline, ask it with the minors.
5. End the episode: `done`, next `fix` when any item says `fix` or `ask`. With an empty list (or one that holds only drops), next `e2e` when the run has E2E (`investigate` or `final` in `docs/pbi/tests-at-end.json`, tests at the end on), otherwise `finish`. Ask the user nothing about the findings in this episode (the heavy review's page, `## Heavy review`, comes before the review and is the only question here): the fix stage's lead asks, before any worker starts, so the answer does not start the designer again.

## Minor findings

The confirmed minors of the final review are settled in this run, not handed to the completion report as they are. They go into the fix list with `decision` `ask` and a recommendation; the fix stage asks about all of them in one question before any fix starts (`appendix/fix.md`, `## Steps`), and the ones the user answers `fix` are fixed in the same wave as the critical and major findings.

- Recommend `fix` when the fix is small and the minor can mislead a reader or a later change: a wrong name, a stale comment or message, a missing check, a test that asserts too little.
- Recommend `skip` for taste, wording and style the repository's rules do not ask for.
- Each minor answered `skip` goes into the completion report under the skipped minors (`appendix/finish.md`).
