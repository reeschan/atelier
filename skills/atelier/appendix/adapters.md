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
   - keep it as its own template: `python <skill>/component.py example use <name>` (then the user asks for it by name; `## Start` in SKILL.md passes `--workflow <name>`).
   Run the choice and show any `error` line. A row with `differs` means a component of that id was registered with other settings: show the `component.py remove <id> --workflow <name>` command for each and stop.
4. After it succeeds, tell the user to run `/reload-plugins` or restart Claude Code when anything was installed. Until then the new skills are not listed.

## Confirm

`confirm` is not a stage. It only changes how `## Approve` in SKILL.md shows the PBI list, and it is separate from Kiln's `kiln.json`. Its values are the same: `auto` (default) looks for the skill `answer-me-with-html`, a skill name looks for that skill, `off` keeps today's list in chat with no record and no check.

1. Write one slice per concern the design left open: `python <skill>/design_record.py slice <id> --question <q> --read <path>:<start>-<end> --unknown <u>` (`docs/pbi/notes/<id>.slice.json`: the question, the lines read and what is unknown, never file contents). Then the record: `python <skill>/design_record.py write --id <design id> --target PBI-<n> ... --concern notes/<id>.slice.json ...` (`docs/pbi/design-record.json`, schema `atelier.design-record/1`). The revision goes up only when the targets or concerns change.
2. Write the Approve list as one Markdown draft in `docs/pbi/notes/approve.md`, open questions as `ask` blocks, and run `python <skill>/design_record.py markdown --draft docs/pbi/notes/approve.md`. It adds the `reply-meta-` lines (design_id, revision, record_hash, draft_hash).
3. Read the `confirm` row of `adapter_gate.py`. On `skill`, and only when that skill is in your skill list, follow its steps with that Markdown. Otherwise show the same Markdown in chat. Never run or write the skill's CLI yourself.
4. Save the reply the user pastes to `docs/pbi/notes/reply-<revision>.md` and run `python <skill>/design_record.py check-reply --reply <that file> --draft docs/pbi/notes/approve.md`:
   - exit 0: approved and recorded in `docs/pbi/design-approvals.jsonl`; go on to build.
   - exit 3: a value differs or the revision is old. Stop, show the current revision again, and say the reply belongs to another version. It is not an authentication failure.
   - exit 4: no fence; the reply is not tied to any revision. Treat it as a comment and ask again.
   - exit 5: an ask was left unselected. That is no answer and no approval; ask those questions.

When `tools_check.py` shows `answer-me-with-html` as missing and confirm is not `off`, show its install line once at Approve. Install only after the user says yes; until then, and on no, the chat fallback runs.
