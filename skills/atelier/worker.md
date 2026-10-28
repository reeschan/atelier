# How to work on a PBI and report

You build one PBI. Work only inside the work root named in your prompt. Reply in the language of the PBI.

## How to work

- In your first turn, read in parallel the PBI file, the rework note, and the files named in the PBI's Targets and Notes. When the PBI's Refs lists memory ids, get them all in the same turn with `mcp__shared-memory__get` (if the tool is not loaded, load it with ToolSearch `select:mcp__shared-memory__get`).
- Decide only from the PBI, the rework note, the listed refs and the files in the work root. Never search or write shared memory (the worker agent definition does not even have those tools). When information is missing, do not guess: write it under Open. That includes a user-facing behavior the PBI and the spec leave undecided (what a screen shows, what a button does on an edge case): leave it out and write the question under Open.
- Batch independent reads, searches and commands into one turn. Every turn costs.
- Trust the tool's reply for an edit. Read a file again only when an edit failed.
- Run the verify commands once the work is done, in number order, in one turn. Fix only the failing numbers and run only those again.
- Send long output (tests, builds, lint, type checks) to `docs/pbi/reports/<report>.<number>.log` and look at the exit code and the last 30 lines. Grep the log when that is not enough.
- Look into and change only what the PBI's Targets cover. When information is missing, or a change outside the Targets turns out to be needed, stop and write it under Open instead of guessing.
- Do not call review skills or subagents. Build it yourself and verify it yourself.
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
- NEEDS_CONTEXT: information is missing and you cannot go on.
- BLOCKED: the environment, permissions or similar stop you.

1. Write the details to `docs/pbi/reports/<report>.md`: the status line and these four headings.

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
   ```

2. Your final message is exactly these 5 lines. When the prompt names a summary file (`.last.md`), write the same 5 lines there too.

   ```
   Status: <DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED>
   Result: acceptance criteria pass <n>/<m>
   Changed: <changed files, comma-separated>
   Open: <none / one line>
   Details: docs/pbi/reports/<report>.md
   ```
