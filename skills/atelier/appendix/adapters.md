# Appendix: the adapters setting and the recommended set

Read this when agents.json has an `adapters` object, or the argument is `setup --recommended`.

The skills a stage may call are the components in `components/` and `<home>/components/`. Each external one carries its link and license, and none calls this skill back (`calls_back` is false). The two built-in reviews ship with Claude Code and need no install.

## Settings

`agents.json` in home may hold an `adapters` object. A missing key means `auto`.

```json
{"adapters": {"review": "off"}}
```

- Keys: `design`, `review`, `security`. Values: `auto` (default), `off`, or a skill name matching `^[A-Za-z0-9][A-Za-z0-9:_./-]{0,127}$`.
- `auto` keeps the template's components. `off` skips that stage's design or review components (the whole check of `appendix/review.md` still runs). A skill name runs that skill in place of the template's components when it is in the skill list; otherwise the template's order applies and the fallback is recorded.
- `load_adapters` in `agents.py` reads this; other keys in agents.json are left alone.
- An invalid entry is ignored and its phase uses `auto`. The warnings are:
  - `adapters: not an object, using auto for every phase`
  - `adapters.<key>: unknown phase, ignored`
  - `adapters.<key>: invalid value, using auto`
- A missing, unreadable or broken agents.json means `auto` for every phase.
- `adapter_gate.py` prints the warnings as `warn <warning>` lines after its table. Give each one to the user in one line.

## Recommended set

The bundled templates call no one else's skills (`appendix/engine.md`, `## External components`). `setup --recommended` is the opt-in for the author's own setup, the example template `examples/workflows/recommended.json`: jev-routed plus pstack (MIT, https://github.com/cursor/plugins), mattpocock-skills (MIT, https://github.com/mattpocock/skills) and yomiyasu (MIT, https://github.com/nanaism/yomiyasu). The example holds only the template and how to install and register each skill; this skill ships none of them. `setup --recommended <name>` uses another example from `component.py example list`. Below, `<name>` is that name, or `recommended` when none was given.

For `setup --recommended`:

1. Run `python <skill>/component.py example check <name>`. It prints one row per skill (skill, stage, installed or missing, registered or not, license, link) and one `install <command>` line per command still needed. Show the table to the user.
2. If there are `install` lines, ask once with AskUserQuestion: install the missing skills now, or not. Name each plugin with its license and link.
   - On yes, run each `install` command yourself, in order, and wait for each to end. On a failure, show it and stop.
   - On no, or when the `claude` command is missing, show the commands and stop.
3. Ask once with AskUserQuestion how to use the template:
   - make it the default: `python <skill>/component.py example use <name> --as jev-routed --replace`;
   - keep it as its own template: `python <skill>/component.py example use <name>` (then the user asks for it by name; `## Start` in `designer.md` passes `--workflow <name>`).
   Run the choice and show any `error` line. A row with `differs` means a component of that id was registered with other settings: show the `component.py remove <id> --workflow <name>` command for each and stop.
4. After it succeeds, tell the user to run `/reload-plugins` or restart Claude Code when anything was installed. Until then the new skills are not listed.

## Confirm

`confirm` is not a stage. It only changes how `## Approve` in `designer.md` shows the PBI list, and it is separate from Kiln's `kiln.json`. Its values are the same: `auto` (default) looks for the skill `answer-me-with-any` (and, while the old name lasts, `answer-me-with-html` with a `warn: old name`), a skill name looks for that skill, `off` keeps today's list in chat with no record: an approve question without `check`, where any reply approves.

0. The work tickets come first, whatever the value of `confirm` (`off` too): after route and its gates, before the approval, run `python <skill>/design_record.py work PBI-<n>` for every PBI (`docs/pbi/PBI-<n>.work.json`, `docs/specs/work-json.md`). Exit 6 writes nothing: fix the PBI file and run it again. With `confirm` `off` there is no record and no check, but the tickets are still made, and `design_record.py stale PBI-<n>` still runs before a PBI's first launch (`designer.md`, `## One PBI`); a rework skips it.
1. Write one slice per concern the design left open: `python <skill>/design_record.py slice <id> --question <q> --read <path>:<start>-<end> --unknown <u>` (`docs/pbi/notes/<id>.slice.json`: the question, the lines read and what is unknown, never file contents). Then the record, passing every work ticket: `python <skill>/design_record.py write --id <design id> --target PBI-<n> ... --concern notes/<id>.slice.json ... --work PBI-<n>.work.json ...` (one `--work` per PBI, a path under `docs/pbi/`; `docs/pbi/design-record.json`, schema `atelier.design-record/2`). The revision goes up when the targets, concerns, the list of works or the content of a work ticket change; the record hash covers the works, so a ticket changed after approval fails the reply check.
2. Write the approval draft `docs/pbi/notes/approve.md` in the seven sections of `appendix/approve.md`, every question as an `ask` block under its decisions, and run `python <skill>/design_record.py markdown --draft docs/pbi/notes/approve.md`. It checks the draft first, as `design_record.py lint` does: on exit 6 it prints each `✗ L<line>` and adds nothing; fix those lines and run it again. Then it adds the `reply-meta-` lines (design_id, revision, record_hash, draft_hash).
3. End the episode with a question of kind `approve`, `check: design-record`, its path `docs/pbi/notes/approve.md`. With the `confirm` adapter `off` there is no design record: leave `check` out. Showing the Markdown in chat, or following the `confirm` skill's route (`answer-me-with-any`), belongs to the caller; never run or write the skill's CLI yourself.
   The conductor prints which way to show it in the `confirm:` line of every question (`SKILL.md`, `## Questions`).
4. The reply check (`design_record.py check-reply`) is the caller's, through `conductor.py answer`. The next launch has an `Answer:` line and no `Continue from:`, holding either a reply that passed or a revise request from `answer --revise`. On a reply, the list is approved: go on to build. On a revise request, change what it asks, raise the revision, and end with kind `approve` again.

When `tools_check.py` shows `answer-me-with-any` as missing and confirm is not `off`, put its install line once in the approve text (`designer.md`, `## Approve`), with its fallback, as a choice. When the approve answer chooses to install, end with a question of kind `decision`, with `state`, for the install; after its `Answer:`, install it and end with `done`, next build wave 1. Otherwise the chat fallback runs.
