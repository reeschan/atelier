---
name: atelier
description: Use for multi-file implementation work; not for a one-file fix. Arguments: setup.
---

# Atelier caller loop

For a small task such as a one-file fix, say Atelier is not used and stop this skill. For `setup`, run `python setup.py runners`, then `python setup.py models <runner>` for each desired runner; use `--home <config-directory>` or `--<runner> <path>` as needed.

For other work, write the user's goal to a UTF-8 file, then run `python conductor.py start --root <work-root> --goal-file <file>`, adding `--spec <file>` when supplied. Use absolute paths to this skill's scripts when needed.

Repeat using the returned action (or `python conductor.py next --root <work-root>`):

- `launch` with runner `agent`: launch an agent subagent at depth 1 with the specified prompt, model and effort; then run `python conductor.py done --root <work-root> --launch-id <launch-id>`.
- `launch` with an external runner: run `python conductor.py run --root <work-root> --launch-id <launch-id>` and use its returned action.
- `check`: run the commands, write a JSON object with integer `exit_code` to the specified result path, then run `python conductor.py done --root <work-root>`.
- `ask`: show the page using the file-send tool when a page exists, and write the summary lines in chat (or the question text when no summary is provided). Pass the user's reply to `python conductor.py answer --root <work-root> --text <reply>`, `--reply <file>`, or `--choice <id>`.
- `done`: hand over the specified `REPORT.md` and stop.

G12: Before deleting data or changing anything outside the repository, stop and ask the user.
G13: Never route around a refusal.

See [GLOSSARY.md](GLOSSARY.md) and [GUARDS.md](GUARDS.md).
