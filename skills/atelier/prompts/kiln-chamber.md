# Prompt block: the implementer chamber of the kiln

Put this block, filled in, into every subagent and external worker prompt while kiln is on (`appendix/kiln.md`): workers, reviewers, the map and lint subagents, plan drafters. External runners cannot read skills, so the text itself goes into their prompt file. It is the same whether or not sac-strategic-compact is installed.

```text
Kiln (context budget for this attempt): soft <implementer_soft> tokens, peak <implementer_peak> tokens.
- Keep your context small: send long output (tests, builds, diffs, logs) to a file and read only the exit code and the last 30 lines. Read a large file by its parts.
- Never compact or summarise your own conversation in the middle of the work.
- If you judge that the work will not finish under the peak, stop at a clean point (never mid-edit, never with a command still running). Write `## State` in your output file with these six headings, then report NEEDS_CONTEXT with `Open: kiln`:
  1. Objective (one line)
  2. Established facts (paths, symbols, commands, observed values; mark guesses as guesses)
  3. Applied changes (files touched, and whether verified)
  4. Verification (checks, outcomes, obstacles)
  5. Dead ends (do not repeat)
  6. Remaining plan (ordered steps, the immediate next action, the done condition)
- When your prompt has `Continue from:`, read that `## State` first and go on from its next action. Do not redo finished steps.
```
