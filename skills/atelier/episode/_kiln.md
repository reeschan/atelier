# Kiln: the designer's part

Kiln (窯) limits how large your context grows before you hand off. This part holds what you do in every episode: measure, log, act and write the state tablet. The lines, the companion skill, the run id, inline cool-downs and the implementer chamber are in `appendix/kiln.md`. When `docs/pbi/kiln.json` says `"enabled": false`, skip this part.

## Measure

Measure and log in one step with the command on your prompt's `Context probe:` line. The conductor writes it in full (`kiln_log.py` with `--role designer`, `--seq` and `--probe-agent` or `--probe-session`, so the script measures your context itself); you fill `--boundary`, `--phase` and `--task-id <W-n>` (the Work key in STATUS.md):

```bash
python <skill>/kiln_log.py docs/pbi --role designer --seq <seq> --probe-agent "ATELIER.<run8>.<seq> " --boundary start|phase|wave --phase <stage> --task-id <W-n>
```

- Once you have read this file and the files your prompt names first (`Continue from:`, `Answer:`), measure once with `--boundary start --phase <episode>`. It records the context the work starts from.
- Then at every stage boundary (`--boundary phase`) and wave boundary (`--boundary wave`), never while a worker attempt or a subagent runs.
- When the `Context probe:` line is only `agent_stats.py --ctx ...` (a run started before), run it and pass its number to `kiln_log.py` with `--tokens <context>` in place of `--probe-agent` or `--probe-session`.
- `context=unknown` (exit code 3) twice in a row: hand off after every 3 PBIs you reconcile, so the next designer still starts small.
- Pass `--action` and `--note` when you choose differently from the printed action (for example, waiting for a subagent before cooling).

## Act

`kiln_log.py` prints one action:

- `noop`: go on.
- `aux_on`: turn on rtk mode (`appendix/handoff.md`, `## rtk mode`).
- `suggest`, `compact` and `handoff`: write the state tablet, check it, and end the episode with status `handoff` and the tablet in `state`. Never use `/compact`. The conductor relaunches the same episode with `Continue from:` at once.

| Boundary | Cool when the action says so? |
|---|---|
| understand and map → plan | Yes, once the notes are on disk |
| plan → build | Yes, once the PBIs are on disk |
| In the middle of a worker attempt or a subagent | No |
| After a failed approach | Yes, after writing the dead ends |
| A wave, the test stage or the lint recheck is done | Measure, then follow the action |

## State tablet

Write it to `docs/pbi/CONTEXT_STATE.md` whenever your result carries `state` (a handoff, and the questions of `designer.md`, `## When to stop`). An optional H1, then these six headings in this order (a number in front of each is allowed) and no other `##` heading:

```markdown
# <optional title>

## Objective
One line.

## Facts
Paths, symbols, commands, observed values. Mark guesses.

## Changes
Files touched, and whether verified.

## Verification
Checks, outcomes, obstacles.

## Dead ends
What this episode tried and dropped.

## Remaining plan
Ordered steps, the immediate next action, the done condition.
```

- Keep it within `tablet_max_chars` of `docs/pbi/kiln.json` (default 6000 characters), so the next designer starts from a small file. Name files by path instead of copying them.
- Right after writing it, run `python <skill>/kiln_log.py tablet-check docs/pbi/CONTEXT_STATE.md` and fix the tablet until it exits 0. `done` runs the same check on the `state` of a handoff or a question and refuses a tablet that fails.
- After a `Continue from:`, read the task, STATUS.md and the tablet only, and go on from its next action. Do not redo finished steps from memory of the chat.

## Run dead ends

What the whole run should not try again goes to `docs/pbi/dead-ends.md`, one line each: what was tried, why it failed, and the PBI or stage. Append; never rewrite earlier lines. It is read by the build, test, review and fix episodes, and it does not count toward the tablet's limit.
