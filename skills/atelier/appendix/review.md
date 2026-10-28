# Appendix: final review and security review

Read this when every PBI has passed, and the review stage or its gate picks `review.code-review`, `review.builtin`, `gate.security-review`, or an external review component (`appendix/engine.md`, `## External components`). A `read-skill` review component (such as `review.pstack-interrogate`) runs as in `appendix/engine.md` and writes the same result file (see `## Skills that report without severity` below).

## Whole check (always first)

Whatever component is picked, look at the whole `git diff --stat`, read the parts that cross PBIs (shared types, settings, callers), and run every PBI's verify commands once more, with output to files as in `appendix/verify.md`.

## Gate

1. Run these from the repository root. `<base>` is the branch the work will merge into (usually `origin/main`). The PBIs' changes are not committed yet, so compare the working tree with the merge-base, not `..HEAD`. `git add -N` makes new files show in the diff without staging their content. Name only the new files from the PBIs' targets: `git add -N .` would also count unrelated untracked files.

   ```
   git add -N -- <each new file the PBIs created>
   git diff --numstat $(git merge-base <base> HEAD) > docs/pbi/verify/final.numstat
   python <skill>/adapter_gate.py docs/pbi --numstat docs/pbi/verify/final.numstat
   ```

2. The table has phase, setting, decision and reason per row. A component with `when` `adapter-gate` holds only when its phase (`review` or `security`) says `run`.
   - `skip`: run no review skill and record the reason.
   - `off`: the phase is turned off in agents.json. Record it.
   - setting: `auto`, `off`, or a skill name from agents.json (`appendix/adapters.md`). A skill name replaces the template's review components for that phase when it is in the skill list.
3. Pass each `warn` line under the table to the user in one line.

## Run a review skill

1. Take a baseline: `git rev-parse HEAD` and `git status --porcelain -- . ':!docs/pbi/reviews' ':!.claude/worktrees'`. The numstat file must exist first; the pathspec leaves out the result file.
2. Start the subagent with `prompts/final-review.md`, filled in. The built-in `code-review` gets the argument `medium`; the others get none. Base, head and spec go in the prompt, never in the argument.
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

## Findings

1. Check each finding yourself: open the file at the line and confirm the claim. A reviewer's report is not proof.
2. A confirmed finding in a file that a PBI's targets name becomes a rework of that PBI (`appendix/rework.md`), counted toward the limit. Review again once every reworked PBI passes.
3. A confirmed finding in a file that no PBI owns goes into the findings of the completion report.
4. A remark about taste or wording is not a reason for rework. Drop it.
5. A confirmed conflict between the change and an accepted decision record (an ADR, in the repository's decisions folder) is never dropped, even when the user prefers the code. It ends one of two ways: rework the code, or a new record that supersedes the old one. Writing that record is the user's call; put it under Waiting on you.
