# Appendix: the conductor and the caller

The caller's and the conductor's part of the episode contract of `appendix/episodes.md`: how the designer is launched, the conductor's own questions, what `done` checks and the run's status. The designer does not need this file; the caller and an inline run do.

## Launch

- `agent` (default; the caller is a Claude Code main session): the caller launches the definition the `launch:` line names with the Agent tool (`## Definitions`). When the Agent tool runs in the background, the caller calls `done` only after the completion notice.
- `headless` (Codex, Grok, or running in the background): `conductor.py run` (mostly `--detach` and `wait`) launches the designer with `claude -p`, or with `codex -a never exec --json` when `roles.designer.runner` is codex. The designer is the main session there, one level shallower.
- A designer whose runner is not `agent` always runs headless: `next` prints `launch: headless (designer runner <runner>)` and `then: ... run --detach` even when the run's launch is `agent`. `next --relaunch` does the same for such a designer.
- A codex designer runs with the sandbox `danger-full-access` unless `roles.designer.permission_mode` names another codex sandbox; `workspace-write` blocks the network and writes outside the repository (the run's records in home, each runner's own home; reading is not limited), and a codex started inside it cannot start commands, so its workers fail there. The workers and roles it launches on codex still run in their own sandbox (`runners.md`, `## codex`).
- `inline` (fallback, neither nested agents nor `claude`): the caller follows `designer.md` (`## Inline`) itself, asks the user directly instead of ending with a question, and uses `appendix/handoff.md` for `/clear` and resume.

## Definitions

The definitions come from `agents/` (`appendix/setup.md`, `## Agent definitions`). `next` picks how the designer is launched, the first of these that holds:

1. On a launch `agent`, the caller's session has loaded the definition: the Agent tool, with `subagent_type` the definition's name.
2. Otherwise, with the `claude` CLI: headless. `next` prints `launch: headless (<reason>)` and `then: ... run --detach`. The `run` passes the bundled definition with `--agents` when its file is not installed or the file read first is older than the bundled one.
3. Otherwise, or after two headless designers in a row that failed (a non-zero exit, `is_error`, or a launch that could not start the process): the `general-purpose` fallback, with the definition file to read first in the prompt and a `warn agents:` line.

Loaded means three things together. The reason in the `launch: headless` line names the first one that fails:

- The file is in `~/.claude/agents/` (or `CLAUDE_CONFIG_DIR`) or in the work root's `.claude/agents/`; otherwise `no agent definition`. Claude Code reads the work root's file first, so when that file's `atelier-defs` is older than the bundled set, it is `older agent definition`.
- `agent_defs` in `agents.json` was not copied in the caller's session (`CLAUDE_CODE_SESSION_ID`); otherwise `definition installed this session`. A file found with no record counts as loaded.
- In this run, the caller has not run `next --relaunch --agent-type-missing` in this session (it does after the Agent tool answered `Agent type not found`, `SKILL.md`, `## The loop`); otherwise `agent type not found`.

- Which definition: `atelier-designer`, with no Skill tool, by default. An episode in which `flow.py` finds a usable row in `main` that needs the Skill tool (`flow.py <pbi-dir> --skill-tool-episodes`) launches on `atelier-designer-skills`, the same instructions with the Skill tool (`appendix/engine.md`, `## Skills and the Skill tool`). The `subagent_type` of the Agent tool and `--agent` and `--agents` of a headless run use the same name. When the template cannot be read, it is `atelier-designer-skills` with a `warn agents:` line, so no usable skill is dropped.
- A headless designer gets `ATELIER_EPISODE=<run>.<seq>` (`<run>` the first 8 characters of `run_id`) in its environment, so hooks can tell an Atelier episode.
- The designer judges the reviewer and the tester the same way (`appendix/episodes.md`, `## Fallback when a definition is missing`).

## Conductor questions

A conductor question (`launch-failure`, `handoff-loop`, or the `decision` for `no-agent-tool`) has four sections, `## Summary`, `## Before`, `## After` and `## Decisions`, in fixed English sentences.

A precondition of `answer`: when `confirm` is not `off` and `docs/pbi/presentations.jsonl` has no `shown` line with the same run_id and seq, it exits 2 with the one line `error: not shown: run python <skill>/present.py <path> --kind <kind> first`.

## Done preconditions

`done` refuses an episode that lacks what its kind must leave behind. The table has one row per entry of `DONE_REQUIRES` in the conductor.

| Episode | Must be there |
|---|---|
| any episode (design, or a build re-approval) that ends with a question of kind `approve`, `confirm` not `off` | `check: design-record` on the question, and the `reply-meta-` lines in the draft (run `design_record.py markdown` last) |
| any episode that ends with a question of kind `approve`, `confirm` is `off` | no `check` on the question |
| finish | `docs/pbi/REPORT.md`, or `report_body` in the result |

When one is missing, `done` exits 2 with the one line `error: missing <what>` and relaunches the same episode; two in a row make a `launch-failure` question. A hash that does not match still exits 12, as before.

The state tablet of a `handoff` and of every question but `approve` must pass `kiln_log.py tablet-check` (`episode/_kiln.md`). When it does not, `done` exits 2 with `error: state tablet (<reason>)` and relaunches the same episode with that tablet on `Continue from:` and a `Fix the tablet first:` line, so the work is not lost; two in a row make a `launch-failure` question, and the episode after its answer also starts from that tablet with the `Fix the tablet first:` line. With Kiln off (`kiln.json` `enabled: false`) the tablet is not checked.

## Status

`done` on a headless episode whose designer or runner is still alive (and ours) settles nothing: it prints `state: running` and `then: wait --timeout 300` and exits 2; only the runner settles a headless episode.

`status` and `wait` add `stale: no live runner` and `relaunch: next --relaunch` to a `running` run whose `runner_pid` and `child_pid` are both dead; `wait` returns at once in that case.
