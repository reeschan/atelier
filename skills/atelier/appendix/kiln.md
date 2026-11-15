# Appendix: Kiln

**Name.** The English term is **Kiln**, from Compaction (working-context cool-down / strategic compaction). The Japanese reading is **窯**. Japanese prose introduces it as Compaction 文脈の Kiln（読みは窯）, and does not lead with 窯 as the source term.

The designer's steps (measure, log, act, the state tablet and its check) are `episode/_kiln.md`, the part every Read first file ends with; read this appendix for the rest: the lines, the companion skill, the run id, an inline cool-down and the implementer chamber, and before writing any subagent or worker prompt. Kiln is on by default; `docs/pbi/kiln.json` with `"enabled": false` turns it off, and then only `appendix/handoff.md` applies.

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

Under the conductor, follow `episode/_kiln.md`. It holds the measuring command (its full arguments are on the request's `Context probe:` line, which the conductor writes), the one measure with `--boundary start` once the Read first file is read, the actions, the boundaries where you cool, the state tablet (its six headings, its limit `tablet_max_chars` in `docs/pbi/kiln.json` and `kiln_log.py tablet-check`), and the run's dead ends.

Inline, the same steps, with these differences:

- With no `Context probe:` line, measure with `python <skill>/agent_stats.py --ctx` and pass its number to `kiln_log.py` with `--tokens <context>`.
- `suggest` or `compact`: write the state tablet, then run `/compact` with keep-instructions (what to keep: paths, ids, the next action), not a discard list. `suggest` may wait for the next boundary; `compact` may not.
- `handoff`: follow `appendix/handoff.md`, `## Hand off`, with the state tablet beside HANDOFF.md.
- After an inline resume, read the task, STATUS.md and the state tablet only. Do not redo finished stages from memory of the chat.

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
