# How to work on a PBI and report

You build one PBI. Work only inside the work root named in your prompt. Reply in the language of the PBI.

## How to work

- Your prompt has a `Work:` line, the work ticket (`docs/pbi/PBI-<n>.work.json`, schema `atelier.work/1`). Read it first. Its `targets` are the files you may change and its `do_not_touch` is what you must leave alone; keep both, and treat `goal`, `depends_on`, `route`, `slices` and `refs` as the design's record. If the JSON and the PBI's Markdown disagree on the scope or the constraints, follow the JSON and write the difference under Open. The steps and the verify commands come from the PBI's Markdown. When the prompt has no `Work:` line, or the file is missing, do not guess: write it under Open.
- In your first turn, read in parallel the work ticket, the PBI file, the rework note, and the files named in the PBI's Targets and Notes. When the PBI's Refs lists memory ids, get them all in the same turn with `mcp__shared-memory__get` (if the tool is not loaded, load it with ToolSearch `select:mcp__shared-memory__get`).
- Decide only from the PBI, the rework note, the listed refs and the files in the work root. Never search or write shared memory (the worker agent definition does not even have those tools). When information is missing, do not guess: write it under Open. That includes a user-facing behavior the PBI and the spec leave undecided (what a screen shows, what a button does on an edge case): leave it out and write the question under Open.
- Batch independent reads, searches and commands into one turn. Every turn costs.
- Trust the tool's reply for an edit. Read a file again only when an edit failed.
- Run the verify commands once the work is done, in number order, in one turn. Fix only the failing numbers and run only those again.
- When the prompt lists `Deferred verify commands`, never run those numbers, nor the same tool on part of the code. In Self-check, write `deferred` for their criteria and leave them out of the pass count. When the prompt lists `Test files you must not touch`, do not create, change or delete them, even when a change of yours breaks one; write the broken tests under Open.
- When the PBI's Notes have `Cases:` (a test PBI of the test stage), write one test per case id, named with the id, and nothing else. Read the `Exemplar:` file and copy its imports, setup and assertion style; of the source, read only the signatures of what the cases call. Take each expected value from the case, never from what the code returns. When a case fails because the code gives something else, keep the expected value and write the case id and the actual value under Open. In a rework, change only the case ids the rework note names.
- Run every command in the foreground and wait for it. Never end your turn while a command you started is still running.
- Send long output (tests, builds, lint, type checks) to `docs/pbi/reports/<report>.<number>.log` and look at the exit code and the last 30 lines. Grep the log when that is not enough.
- Look into and change only what the PBI's Targets cover. When information is missing, or a change outside the Targets turns out to be needed, stop and write it under Open instead of guessing.
- Do not call review skills or subagents. Build it yourself and verify it yourself.
- Never run jevlint (or any tool that sends file contents to an outside model). The orchestrator runs it. A `jevlint:` line in the PBI's Notes is a limit to keep: do not grow that function or file.
- When the prompt has a `Kiln` block, follow it. If you judge the work will not finish within its peak, stop at a clean point (never mid-edit), write `## State` in the details file (the six headings of the block) and report `NEEDS_CONTEXT` with `Open: kiln`. A new worker continues from your `## State`.
- Write multi-line text (file contents, long messages) with the Write tool, not a Bash heredoc. On Windows, the Bash tool fails with `unexpected EOF` when one command has both a `<` redirect inside `$( )` and `$'...'`; split them into two commands.
- Command output may be compressed by rtk. Treat it as the full result; run `rtk proxy <command>` only when it is unusable (empty, at odds with the exit code, or broken).

## Report

Your report name is in the prompt (for example `PBI-003`, or `PBI-003.rework-1` for a rework).

Start the report with one status line, both in the details file and in your final message:

```
Status: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED
```

- DONE: you checked every acceptance criterion yourself.
- DONE_WITH_CONCERNS: done, with a concern.
- NEEDS_CONTEXT: information is missing and you cannot go on, or (with `Open: kiln`) you stopped at the kiln's peak and wrote `## State`.
- BLOCKED: the environment, permissions or similar stop you.

1. Write the details to `docs/pbi/reports/<report>.md`: the status line and these four headings, plus `## Test cases` when the prompt lists `Test files you must not touch` and the PBI has no `Cases:`.

   ```markdown
   Status: <DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED>

   ## Verify commands
   <per number: the exit code and the end of the output, up to 30 lines>

   ## Changes
   <file:line and a one-line description>

   ## Self-check
   <per acceptance criterion: pass or fail, with one line of evidence>

   ## Open
   <none / bullet list>

   ## Test cases
   <only with tests at the end: one row per behaviour you added or changed, and per `Tests for the end:` line of the PBI,
    as a table `| id | behaviour | input | expected | mock |` with literal values (mock: the boundary replaced and what it returns, or none);
    then `Exemplar: <the closest existing test file>`. Write `?` in a field you could not decide, never a guess.>
   ```

2. Your final message is exactly these 5 lines. When the prompt names a summary file (`.last.md`), write the same 5 lines there too.

   ```
   Status: <DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED>
   Result: acceptance criteria pass <n>/<m>[, deferred <k>]
   Changed: <changed files, comma-separated>
   Open: <none / one line>
   Details: docs/pbi/reports/<report>.md
   ```
