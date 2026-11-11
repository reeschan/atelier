# Appendix: Kiln

**Name.** The English term is **Kiln**, from Compaction (working-context cool-down / strategic compaction). The Japanese reading is **窯**. Japanese prose introduces it as Compaction 文脈の Kiln（読みは窯）, and does not lead with 窯 as the source term.

Read this at Start, at every stage and wave boundary, and before writing any subagent or worker prompt. Kiln is on by default; `docs/pbi/kiln.json` with `"enabled": false` turns it off, and then only `appendix/handoff.md` applies.

## Concept

**Kiln** (Japanese reading 窯) is the limit on working context for the whole run: the orchestrator (designer chamber), every subagent and every external worker (implementer chamber). Stages still hand off through their artifacts; the kiln only says how large a conversation may grow before it cools down. It never replaces verify, STATUS.md or `worker.md`.

| Line | Meaning | Default |
|---|---|---|
| Soft | Cool at the next stage or wave boundary | designer 120000 / implementer 60000 |
| Peak | Must cool before the next stage starts (implementer: stop at a clean point) | designer 180000 / implementer 90000 |
| Aux | Turn on rtk mode | 150000 |
| Handoff | End the episode with `handoff` (inline: write HANDOFF.md and stop for `/clear` and resume) | 250000 |

The lines come from the run copy `docs/pbi/kiln.json` (example `examples/kiln.json`), else the template's `kiln`. Write the run copy at Start only when it is missing; keep an existing `docs/pbi/kiln.json` unless the user explicitly approves replacing it. Claude Code's system prompt and this skill's body take a few tens of thousands of tokens before any work, so lines far below the defaults only make every boundary cool down.

## sac-strategic-compact

The companion skill (https://github.com/reeschan/sac-strategic-compact) holds the same recipe. When `tools_check.py` finds it, follow it for the designer's tablet and cool-down. When it is missing, follow this appendix; the rules below are its copy. Either way:

- record every decision with `python <skill>/kiln_log.py` (the log script the skill names, log_compact_event.py, is not installed with it; `kiln_log.py` writes the same line);
- give subagents and external workers the block in `prompts/kiln-chamber.md`, never the skill. Workers do not read skills.

When it is missing, offer the install once at Approve (one line with the URL). Never install it yourself.

## Run id

One run keeps one `run_id` on every line of `docs/pbi/.kiln/compact-events.jsonl`. `kiln_log.py` keeps it in `docs/pbi/.kiln/run_id` and creates it on the first line. Inline, on handoff, write it into `docs/pbi/HANDOFF.md` as `Run id: <run_id>` (`appendix/handoff.md`); under the conductor, do not write `Run id` into `HANDOFF.md`, and leave `docs/pbi/.kiln/run_id` on disk. Inline, on resume, pass it back once with `kiln_log.py --run-id <run_id>` when `docs/pbi/.kiln/run_id` is missing; never start another id after `/clear`.

## Designer (the orchestrator)

At every stage boundary and wave boundary, never while a worker attempt runs:

1. Measure with the command on the request's `Context probe:` line; when the request has no such line, `python <skill>/agent_stats.py --ctx`.
   - `context=unknown` (exit code 3) twice in a row: hand off after every 3 PBIs you reconcile.
2. Decide and log in one step: `python <skill>/kiln_log.py docs/pbi --role designer --boundary phase|wave --phase <stage> --tokens <context> --task-id <W-n>`. It prints the action from the lines: `noop`, `aux_on`, `suggest`, `compact` or `handoff`. Pass `--action` and `--note` when you choose differently (for example, waiting for a subagent before cooling).
3. Act:
   - `noop`: go on.
   - `aux_on`: turn on rtk mode (`appendix/handoff.md`, `## rtk mode`).
   - Under the conductor, `suggest`, `compact` and `handoff` all mean: write the state tablet `docs/pbi/CONTEXT_STATE.md` (below) and end the episode with status `handoff` (`appendix/episodes.md`, `## Kiln`). Never use `/compact`.
   - Inline, `suggest` or `compact`: write the state tablet, then run `/compact` with keep-instructions (what to keep: paths, ids, the next action), not a discard list. `suggest` may wait for the next boundary; `compact` may not.
   - Inline, `handoff`: follow `appendix/handoff.md`, `## Hand off`, with the state tablet beside HANDOFF.md.
4. After a `Continue from:` or an inline resume, read the task, STATUS.md and the state tablet only. Do not redo finished stages from memory of the chat.

### State tablet

40 to 80 lines, six headings:

1. **Objective**: one line
2. **Established facts**: paths, symbols, commands, observed values; mark guesses
3. **Applied changes**: files touched, and whether verified
4. **Verification**: checks, outcomes, obstacles
5. **Dead ends**: do not repeat
6. **Remaining plan**: ordered steps, the immediate next action, the done condition

### When to cool

| Boundary | Cool? |
|---|---|
| understand and map → plan | Yes, once the notes are on disk |
| plan → build | Yes, once the PBIs are on disk |
| In the middle of a worker attempt or a subagent | No |
| After a failed approach | Yes, after writing the dead ends |
| A wave, the test stage or the lint recheck is done | Measure, then follow the lines |

## Implementer (every subagent and worker)

- Start a fresh subagent or worker for every attempt (`runners.md`). Never pass the designer's conversation into it.
- Put the block of `prompts/kiln-chamber.md`, filled with the implementer lines, into every prompt: workers (`Kiln:` line in `runners.md`), reviewers (`prompts/final-review.md`), the map and lint subagents, plan drafters. External runners get it as text in their prompt file.
- A worker that stops at its peak reports `NEEDS_CONTEXT` with `Open: kiln` and a `## State` section. Continue it as `appendix/verify.md` says: a new worker with `Continue from:`, counted as a continue, not a rework.
- After each attempt is recorded, log it: `python <skill>/kiln_log.py docs/pbi --role implementer --boundary attempt --phase <report name> --tokens <the attempt's tokens from record_run.py> --action noop|handoff` (`handoff` for a kiln stop).

## Do not

- Cool down in the middle of a worker attempt or a running subagent.
- Pass designer chat history into an implementer attempt.
- Skip the log line when you measured at a boundary, even for `noop`.
- Use SAC (one action per conversation) unless the user asks for it.
