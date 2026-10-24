# Appendix: the adapters setting and the recommended set

Read this when agents.json has an `adapters` object, or the argument is `setup --recommended`.

The skills a stage may call are the components in `components/`. Each external one carries its link and license, and none calls this skill back (`calls_back` is false). The two built-in reviews ship with Claude Code and need no install.

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

Only mattpocock-skills is recommended: https://github.com/mattpocock/skills, MIT (Copyright (c) 2026 Matt Pocock). This skill does not ship it.

For `setup --recommended`:

1. Show the name, the stages it serves (design: grilling and domain-modeling; review: code-review), the link, the license, and the install command.
2. Ask once with AskUserQuestion: install now, or not.
3. On yes, run it yourself and wait for it to end:

   ```bash
   claude plugin install mattpocock-skills@claude-plugins-official --scope user
   ```

4. After it succeeds, tell the user to run `/reload-plugins` or restart Claude Code. Until then the skill is not used.
5. On no, or when the `claude` command is missing, only show the command and stop.
