---
name: atelier
description: Use for multi-file implementation work; not for a one-file fix. Arguments: setup.
---

# Atelier caller loop

For a small task such as a one-file fix, say Atelier is not used and stop this skill. For `setup`, run `python setup.py runners`, then `python setup.py models <runner>` for each desired runner; use `--home <config-directory>` or `--<runner> <path>` on `runners` as needed.

For other work, write the user's goal to a UTF-8 file, then run `python conductor.py start --root <work-root> --goal-file <file>`, adding `--spec <file>` when supplied (and `--agents <file>`, `--template <file>`, `--force` as needed). If your memory records where this repository's OMM cache lives, add `--omm-cache-dir <dir>`; otherwise leave it out. Use absolute paths to this skill's scripts when needed. To continue a run that is still open (for example in a new conversation), skip `start` and run `drive` below.

## Drive in the background

`python conductor.py drive --root <work-root>` does every action that needs no model by itself: it runs `check` commands and writes their result, runs `launch` and `launches` actions with an external `runner` (the launches of a `launches` action at the same time), and copies `prepare` data. It stops at the first action that needs you and prints `{"drove": [...], "next": <action>}`. Run it in the background where your host can (in Claude Code, the Bash tool with `run_in_background`), then end your turn and wait for its completion notice; where it cannot, run it in the foreground. Add `--shell <path to a POSIX shell>` when the check commands use POSIX syntax and the system shell is not one (Windows `cmd.exe`).

While `drive` or a subagent runs, do not run `status` or `next`, read result files, or poll. If the user asks how it is going, run `status` once.

## The loop

Act on the returned action (from `start`'s `next`, `answer`, or `drive`'s `next`):

- `check`, `launch` or `launches` with an external `runner`, or `prepare`: do not do it yourself; run `drive` in the background.
- `launch` or `launches` with `runner == "agent"`: in one message, launch one agent subagent at depth 1 per launch (a `launch` is one; `launches` lists them under `launches`), each with its `prompt` file, `model`, and `effort`. Launch them in the foreground, so they run at the same time and your turn waits until every one has finished. Then run `python conductor.py drive --root <work-root> --done <launch_id>` in the background, with the action's own `launch_id`, and end your turn.
- `omm_scan`: run the `omm-scan` skill at `repository`, then `store_command` there; then run `drive --done <launch_id>` in the background.
- `review_skill`: run `skill` with `args` through the Skill tool on the diff from `base` to the working tree, follow `instructions` to write `result`, then run `drive --done <launch_id>` in the background.
- `ask`: show `page` (and `attachments`, if any) using the file-send tool; when `page` is null (answer-me-with-html is not installed), send `draft` instead. In chat, write only the `chat` lines (the conclusion and the decision) and the `choices`. When `chat` is absent, write the contents of the `question` file and the `choices`. Do not copy the page or `summary` into chat. Pass the user's reply to `python conductor.py answer --root <work-root> --choice <id>` (from `choices`), `--text <reply>` (for `interview_round`), or `--reply <file>`.
- `ask` with `kind` `plan_return`: a worker stopped its PBI in one of its four exceptions; that PBI and the PBIs that depend on it wait, and the rest of the wave is already built. Show it as above, then pass the user's choice: `--choice replan` (the planner changes only the PBIs the stop concerns, and the changed plan comes back for approval) or `--choice continue` (the stop goes on the retrospective list as an objection, and the run goes on). The run goes back to the planner in the middle of building only on the user's answer (this one, or `replan` for a broken plan).
- A failed check, a fix PBI that fails twice, a failed E2E and a failed final check ask nothing: the run holds or records them, puts them on the retrospective list or the leftovers, and goes on. Do not ask the user about them yourself.
- With `acceptance_first` on, the Red check and the locking of keys are steps `drive` runs as the conductor's own work, and the planner's confirmation of the acceptance tests is an ordinary `launch`. Nothing extra is needed from the caller. The `done` report can show workers' objections, stops and unresolved requirements.
- `done`: send `report` (`REPORT.md`) and `handoff` (`HANDOFF.md`) using the file-send tool; do not restate them in chat. When `complete` is false, say in one line what is not done: the PBIs in `unfinished_pbis`, and the number of `leftovers` and that they went to the backlog (`backlog` is its path; when it is null, say they are only in the report). Then hand off (below).

For `fix_approval`, findings the user decides to fix (including ones held back by confidence or skipped by triage) go in a reply file: `{"choice": "approve", "include_ids": [...]}`.

When the user approves a change to the spec, glossary or another file and you make it while a run is open, run `python conductor.py approve-edit --root <work-root> --path <file> --reason <what the user approved>` right after the edit; otherwise G1 stops the role that is running.

## Hand off after the run

The next stage (finishing touches and the pull request) does not need this conversation: `HANDOFF.md` names the work root, branch, base commit, goal, spec, report and run state. Ask the user one question:

- **New conversation (recommended)**: where your host can start a new session, start one whose only prompt is the contents of `HANDOFF.md`, and stop here. Otherwise ask the user to run `/clear` and paste `HANDOFF.md` (or its path).
- **Compact**: ask the user to run `/compact` and keep only what `HANDOFF.md` names.
- **Continue here**: go on in this conversation.

You cannot clear or compact the conversation yourself; say so when you ask.

G12: Before deleting data or changing anything outside the repository, stop and ask the user.
G13: Never route around a refusal.

See [GLOSSARY.md](GLOSSARY.md) and [GUARDS.md](GUARDS.md).
