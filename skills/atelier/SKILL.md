---
name: atelier
description: Use for multi-file implementation work; not for a one-file fix. Arguments: setup.
---

# Atelier caller loop

For a small task such as a one-file fix, say Atelier is not used and stop this skill. For `setup`, run `python setup.py runners`, then `python setup.py models <runner>` for each desired runner; use `--home <config-directory>` or `--<runner> <path>` on `runners` as needed.

For other work, write the user's goal to a UTF-8 file, then run `python conductor.py start --root <work-root> --goal-file <file>`, adding `--spec <file>` when supplied (and `--agents <file>`, `--template <file>`, `--force` as needed). If your memory records where this repository's OMM cache lives, add `--omm-cache-dir <dir>`; otherwise leave it out. Use absolute paths to this skill's scripts when needed.

Repeat using the returned action (from `start`'s `next`, or `python conductor.py next --root <work-root>`):

- `launch` with `runner == "agent"`: launch an agent subagent at depth 1 with the `prompt` file, `model`, and `effort`; then run `python conductor.py done --root <work-root> --launch-id <launch_id>`.
- `launch` with an external `runner`: run `python conductor.py run --root <work-root> --launch-id <launch_id>` and use its returned action.
- `check`: run `commands`, write a JSON object with integer `exit_code` (and optional `output`) to `result`, then run `python conductor.py done --root <work-root>`.
- `omm_scan`: run the `omm-scan` skill at `repository`, then `store_command` there; then run `python conductor.py done --root <work-root> --launch-id <launch_id>`.
- `prepare`: copy each of `copies` from `source` to `dest` (the user approved these with the plan); then run `done` with `--launch-id`.
- `review_skill`: run `skill` with `args` through the Skill tool on the diff from `base` to the working tree, follow `instructions` to write `result`, then run `done` with `--launch-id`.
- `ask`: show `page` (and `attachments`, if any) using the file-send tool; when `page` is null (answer-me-with-html is not installed), send `draft` instead, and write `summary` lines in chat (or the contents of the `question` file when `summary` is absent). Pass the user's reply to `python conductor.py answer --root <work-root> --choice <id>` (from `choices`), `--text <reply>` (for `interview_round`), or `--reply <file>`.
- `done`: hand over `report` (`REPORT.md`) and stop.

G12: Before deleting data or changing anything outside the repository, stop and ask the user.
G13: Never route around a refusal.

See [GLOSSARY.md](GLOSSARY.md) and [GUARDS.md](GUARDS.md).
