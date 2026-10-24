# Appendix: the workflow engine

Read this when you pick a component, cannot decide a `when`, run a `read-skill` component, or write STATUS.md. Templates live in `workflows/`, components in `components/`; the contract is `docs/specs/atelier-workflow.md` in the repository.

## The flow table

`flow.py` prints the template it resolved, then one row per candidate, in stage order. Gate rows say `gate after <stage>` and follow their stage.

| Column | Meaning |
|---|---|
| stage | the stage, or `gate after <stage>` |
| component | the component id (`components/<id>.json`) |
| invoke | `skill` (Skill tool), `read-skill` (read a plugin's SKILL.md and follow it), `script` (a bundled Python script), `builtin` (a procedure in this skill) |
| where | `main` (your session), `subagent-readonly`, `subagent`, `runner` (a worker on its route) |
| memory | how much shared memory the component may use: `full`, `refs-only`, `none` |
| when | the condition under which it runs (below) |
| status | `builtin`, `script ok`, `cli missing <name>`, `env missing <name>`, or `check skill list` |
| doc | the file in this skill that holds the procedure; read it when the component is picked |

## Picking

1. A stage takes the first row that is usable and whose `when` holds. The rows after it are not run.
2. A gate runs every row that is usable and whose `when` holds.
3. Usable:
   - `builtin` and `script ok`: always.
   - `cli missing` and `env missing`: never.
   - `check skill list`: for `skill`, the skill's name is in the skill list in your context. For `read-skill`, the plugin's file is found (`## read-skill components`). Do not scan the disk for skills.
   - `adapters` in agents.json can turn a design, review or security stage off, or name a skill for it (`appendix/adapters.md`).
4. Never use a skill that the model cannot invoke through the Skill tool; those run as `read-skill`.
5. A component's `then` lists components to run right after it, when each one is usable and its own `when` holds (grilling, then domain-modeling).
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

## where and memory

- `main`: run it yourself, in this session.
- `subagent-readonly`: an Agent tool subagent (`general-purpose`) told to write nothing but its output file in `docs/pbi/`, and never to commit or post. Take `git rev-parse HEAD` and `git status --porcelain -- . ':!docs/pbi'` before and after; if either changed, discard the result and record `fallback`.
- `subagent`: a `general-purpose` subagent that may write only the result file named in its prompt.
- `runner`: the worker on the PBI's route (`runners.md`).
- `full`: the component may search and read shared memory like you do. `refs-only`: it may `get` only the refs listed in the PBI. `none`: no memory tools.
- The template's `build.external_memory` says whether external runners may `get` refs themselves (`refs-only`) or not (`none`, the default). Either way you copy the listed refs into their prompt (`runners.md`).

## read-skill components

A `read-skill` component points at a skill that the model cannot invoke (all pstack skills). Run it like this:

1. Find the plugin's install directory: the directory under `~/.claude/plugins/` whose path ends with the plugin name and holds the component's `path` (for pstack, `skills/<name>/SKILL.md`). Look only for that path. When it is not there, the component is not usable: record `fallback (plugin not installed)` and take the next row.
2. Start a subagent of the row's `where` type. In its prompt: the SKILL.md path to read and follow, the harness notes to read first when the plugin has them (pstack: `docs/harness/claude-code.md`), the component's `forbid` items, the request or the PBI path, and the output file below.
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
