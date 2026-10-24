# Appendix: the workflow engine

Read this when you pick a component, cannot decide a `when`, run a `read-skill` component, or write STATUS.md. Templates live in `workflows/`, components in `components/` and `<home>/components/` (`## External components`); the contract is `docs/specs/atelier-workflow.md` in the repository.

## The flow table

`flow.py` prints the template it resolved, then one row per candidate, in stage order. Gate rows say `gate after <stage>` and follow their stage.

| Column | Meaning |
|---|---|
| stage | the stage, or `gate after <stage>` |
| component | the component id (`components/<id>.json`, or `<home>/components/<id>.json`) |
| invoke | `skill` (Skill tool), `read-skill` (read a plugin's SKILL.md and follow it), `script` (a bundled Python script), `builtin` (a procedure in this skill) |
| where | `main` (your session), `subagent-readonly`, `subagent`, `runner` (a worker on its route) |
| memory | how much shared memory the component may use: `full`, `refs-only`, `none` |
| when | the condition under which it runs (below) |
| status | `builtin`, `script ok`, `cli missing <name>`, `env missing <name>`, `plugin ok <SKILL.md path>`, `plugin missing <name>`, or `check skill list` |
| doc | the file in this skill that holds the procedure; read it when the component is picked |

## Picking

1. A stage takes the first row that is usable and whose `when` holds. The rows after it are not run.
2. A gate runs every row that is usable and whose `when` holds.
3. Usable:
   - `builtin` and `script ok`: always.
   - `plugin ok`: always; run it as in `## read-skill components`.
   - `cli missing`, `env missing` and `plugin missing`: never.
   - `check skill list`: the skill's name is in the skill list in your context. Do not scan the disk for skills.
   - `adapters` in agents.json can turn a design, review or security stage off, or name a skill for it (`appendix/adapters.md`).
4. Never use a skill that the model cannot invoke through the Skill tool; those run as `read-skill`.
5. A component's `then` lists components to run right after it, when each one is usable and its own `when` holds.
6. A component's `forbid` lists arguments never to pass and steps to skip. Put each item in the prompt when the component runs in a subagent.

## when

| Value | Holds when |
|---|---|
| `always` | always |
| `clearly-complex` | the request adds a new concept, an external interface or a data-model change, and likely spans 2 or more modules |
| `ask-or-decide` | the design decision is run (`appendix/design.md`) |
| `adapter-gate` | `adapter_gate.py` prints `run` for the phase (`appendix/review.md`) |
| `tier-opus` | the PBI's `route.json` has tier `opus`. Per PBI, so only in a gate after route |
| `coupling-high` | the PBI's `route.json` has `scores.coupling` 2. Per PBI, so only in a gate after route |
| `deep` | in a gate after route: the PBI's `route.json` has review `deep`. In the review stage: any PBI does |

A template entry `{"id", "when"}` replaces the component's own `when` in that template only.

The per-PBI values are decided by a script. Once every `route.json` exists, run `python <skill>/flow.py docs/pbi --decide` (with the same `--workflow <name>` as at the start, if any): it prints place, component, PBI, decision (`run`, `skip`, or `orchestrator` for a `when` you decide) and reason, for each row of the gates after route (one per PBI) and for each review row whose `when` is `deep` (PBI `all`). Follow it; do not judge these values yourself.

## where and memory

- `main`: run it yourself, in this session.
- `subagent-readonly`: an Agent tool subagent (`general-purpose`) told to write nothing but its output file in `docs/pbi/`, and never to commit or post. Take `git rev-parse HEAD` and `git status --porcelain -- . ':!docs/pbi'` before and after; if either changed, discard the result and record `fallback`.
- `subagent`: a `general-purpose` subagent that may write only the result file named in its prompt.
- `runner`: the worker on the PBI's route (`runners.md`).
- `full`: the component may search and read shared memory like you do. `refs-only`: it may `get` only the refs listed in the PBI. `none`: no memory tools.
- The template's `build.external_memory` says whether external runners may `get` refs themselves (`refs-only`) or not (`none`, the default). Either way you copy the listed refs into their prompt (`runners.md`).
- `record_run.py` checks every attempt against this: it counts the worker's memory calls (from the agent's transcript, or from the tool-call events in an external runner's JSON logs; `violation` is null when it cannot tell) and marks a search, a write, a `get` under `none`, or a `get` of a ref not in the PBI's Refs as `memory.violation` in `runs.jsonl`.

## External components

The bundled components cover only this skill and the skills built into Claude Code (`code-review`, `security-review`). This skill ships no one else's skill and no component for one. Any other skill, the user's own included, becomes an **external component**:

1. The user installs the skill with Claude Code's plugin commands (`/plugin marketplace add <repo>`, `/plugin install <plugin>@<marketplace>`), or keeps their own in `~/.claude/skills/<name>/`.
2. `python <skill>/component.py add <plugin>:<name> --stage <stage> [--after <stage>] [--when W] [--forbid TEXT]` reads the installed SKILL.md and writes `<home>/components/<stage>.<plugin>-<name>.json`: `skill` when the model can invoke it, `read-skill` when it has `disable-model-invocation` (plugin skills only; a personal skill must be model-invocable), with the license and link from the plugin's `plugin.json`. `--when` goes on the template's entry (`{"id", "when"}`), not into the shared component file, so one template's condition never changes another's; registering the same component again with other settings is refused. It puts the component first in that stage of `<home>/workflows/<workflow>.json` (default `jev-routed`, copied from the bundled one when missing), or last in the gate after `--after`. It checks the result like `flow.py` and leaves nothing on an error.
3. `component.py remove <id>` takes it out of that template and deletes the component, refusing while another home template still uses it; `component.py list` shows bundled and external components.
4. An example template (`examples/workflows/<name>.json`) carries a whole template plus, for each skill it uses, how to install and register it. `component.py example check <name>` shows which skills are missing and prints the install commands (exit 3 while some are missing; the script installs nothing). `component.py example use <name> [--as NAME] [--replace]` registers every skill and writes the template to home. `setup --recommended` drives this (`appendix/adapters.md`).

- A home component cannot reuse a bundled id; `flow.py` reports it as an error, so every stage keeps its fallback.
- Run `component.py` only when the user asks for that skill (or `setup --recommended`, `appendix/adapters.md`). The commands for the skills this skill was built around are in the README ("External components").

## read-skill components

A `read-skill` component points at a skill that the model cannot invoke (all pstack skills). Run it like this:

1. The status column gives the SKILL.md path (`plugin ok <path>`); `flow.py` looked under `~/.claude/plugins/` (or `PBI_PLUGINS_DIR`). With `plugin missing`, record `fallback (plugin not installed)` and take the next row.
2. Start a subagent of the row's `where` type. In its prompt: the SKILL.md path to read and follow, the harness notes to read first when the plugin has them (pstack: `docs/harness/claude-code.md`, two levels above `skills/`), the component's `forbid` items, the request or the PBI path, and the output file below.
3. Output: a note in `docs/pbi/notes/` (`<slug>.<name>.md`, or `PBI-<n>.<name>.md` per PBI), or the result file its `forbid` names. The subagent returns at most 5 lines and the path.
4. Link per-PBI notes from that PBI's Notes section, so its worker reads them. Workers never run plugin skills themselves.

## STATUS.md

Write it at the start and keep it current with Edit. Read states and counts only from this file.

```markdown
Skill version: <version and published commit>
Work key: <repo>:<branch>:W-001 / Workspace: project:work.<repo>.<branch>.W-001
Workflow: jev-routed (<path printed by flow.py>)
Skipped runners: <--skip value and reason / none>
stage design: design.builtin run (clearly complex)
stage review: review.code-review run (diff 412 lines > 300) findings 0,2,1

| PBI | Model | State | Rework | Tier up | Continue | Note |
|---|---|---|---|---|---|---|
| PBI-001 | agent sonnet high (sonnet / normal) | running | 1/2 | 0/1 | 0/2 | criterion 2 fails |
```

- States: not started, running, in review, done, held, limit reached. Add a row for new work found on the way.
- The rework limit after the slash is the template's `fix.max_rework`.
- The version line lets a resume notice that the skill changed in between.

## Stage records

Write one line per stage and gate row that ran or was passed over for a reason, in STATUS.md and in HANDOFF.md:

```
stage <stage|gate after stage>: <component> <run|skip|fallback|off> (<reason>) [findings <critical>,<major>,<minor>]
```

- `skip`: its `when` did not hold. `fallback`: it was not usable or failed, and the next row ran. `off`: turned off in agents.json.
- Add `findings` only for a review or security component.
- The approval list and the completion report copy these lines as they are.
