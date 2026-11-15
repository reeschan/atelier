# Appendix: the workflow engine

Read this when you pick a component, cannot decide a `when`, run a skill or `read-skill` component, or write STATUS.md or a stage record. Templates live in `workflows/`, components in `components/` and `<home>/components/` (`## External components`); the contract is `docs/specs/atelier-workflow.md` in the repository.

## The flow table

`flow.py` prints the template it resolved, then one row per candidate, in stage order. Optional stage rows say `<optional stage> after <stage>` (`ui-design after design`) and gate rows `gate after <stage>`; both follow their stage, optional stages first.

| Column | Meaning |
|---|---|
| stage | the stage, `<optional stage> after <stage>`, or `gate after <stage>` |
| component | the component id (`components/<id>.json`, or `<home>/components/<id>.json`) |
| invoke | `skill` (a skill: through the Skill tool, or by reading its SKILL.md, `## Skills and the Skill tool`), `read-skill` (read a plugin's SKILL.md and follow it), `script` (a bundled Python script), `builtin` (a procedure in this skill) |
| where | `main` (your session), `subagent-readonly`, `subagent`, `runner` (a worker on its route); the template entry's `where` when it has one |
| memory | how much shared memory the component may use: `full`, `refs-only`, `none` |
| when | the condition under which it runs (below) |
| status | `builtin`, `script ok`, `cli missing <name>`, `env missing <name>`, `plugin ok <SKILL.md path>`, `plugin missing <name>`, `skill ok <SKILL.md path>`, `skill builtin` or `skill missing` |
| doc | the file in this skill that holds the procedure; read it when the component is picked |
| models | the models of the subagents the component starts, from the template entry (`opus xhigh,sonnet high`), or `-` for the skill's own choice |

## Picking

1. A stage takes the first row that is usable and whose `when` holds. The rows after it are not run.
2. A gate, and the optional stages `ui-design`, `marketing`, `security` and `model-check` (`appendix/optional.md`), run every row that is usable and whose `when` holds. The optional stages `map`, `lint-survey` and `lint-recheck` pick like a stage (rule 1): their last row is the fallback prompt, which runs only when the rows before it are not usable.
3. Usable:
   - `builtin` and `script ok`: always.
   - `plugin ok`: always; run it as in `## read-skill components`.
   - `cli missing`, `env missing` and `plugin missing`: never.
   - `skill ok` and `skill builtin`: always; run it as in `## Skills and the Skill tool`.
   - `skill missing`: never.
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
| `deep` | the PBI's `route.json` has review `deep`. Gates after route only; the review stage cannot use it (`## Heavy review`) |
| `ui-change` | the request adds a screen or a user flow, or gives screens a new look from a new direction; not when `ui-retouch` holds (`appendix/optional.md`) |
| `ui-retouch` | the request only fixes how existing screens look (AI-looking signs, polish), adding no screen or flow. Never with `ui-change` in the same run (`appendix/optional.md`) |
| `user-facing` | the request adds or changes something end users see or pay for (`appendix/optional.md`) |
| `ask-user` | the user said yes when asked this run; always ask, never decide alone. Optional stages only (`appendix/optional.md`) |
| `heavy-ask` | the finished diff is deep work by `heavy_check.py`, and the user said run on its question page right before the final review (`appendix/review.md`, `## Heavy review`). The review stage only |

A template entry `{"id", "when", "where", "models"}` replaces the component's own `when` and `where` in that template only, and `models` names the models of the subagents the component starts (`## read-skill components`). The component file is shared by every template, so these never go into it. `flow.py` accepts `models` only on an entry whose `where` is `main`, because a subagent cannot start subagents.

The per-PBI values are decided by a script. Once every `route.json` exists, run `python <skill>/flow.py docs/pbi --decide` (with the same `--workflow <name>` as at the start, if any): it prints place, component, PBI, decision (`run`, `skip`, or `orchestrator` for a `when` you decide) and reason, for each row of the gates after route (one per PBI) and for each review row whose `when` is `heavy-ask` (PBI `all`). Follow it; do not judge these values yourself. A `heavy-ask` row is `orchestrator` until `heavy_check.py record` has written the answer to `docs/pbi/adapters.json`, and then that `run` or `skip`.

## Heavy review

A review component with `when` `heavy-ask` (pstack's `interrogate` in the recommended example) costs much more than the usual review, so it runs only on deep work, and only when the user says run. It is not asked at approval: right before the final review, `heavy_check.py` checks the finished diff, and only a deep one becomes a question page (`appendix/review.md`, `## Heavy review`). With no answer it does not run, and the next usable row of the review stage runs (`lens-review` in the recommended example).

`flow.py` refuses every template that would run such a review without that check: the review stage cannot use `deep` (a PBI's review `deep` says how closely to check that PBI, not that the diff is deep), and a review row that runs in `main` (a skill that starts its own reviewers; builtin rows aside) must use `heavy-ask`.

## where and memory

- `main`: run it yourself, in this session.
- `subagent-readonly`: an Agent tool subagent (`general-purpose`) told to write nothing but its output file in `docs/pbi/`, and never to commit or post. Take `git rev-parse HEAD` and `git status --porcelain -- . ':!docs/pbi' ':!.claude/worktrees'` before and after; if either changed, discard the result and record `fallback`.
- `subagent`: a `general-purpose` subagent that may write only the result file named in its prompt.
- `runner`: the worker on the PBI's route (`runners.md`).
- `full`: the component may search and read shared memory like you do. `refs-only`: it may `get` only the refs listed in the PBI. `none`: no memory tools.
- The template's `build.external_memory` says whether external runners may `get` refs themselves (`refs-only`) or not (`none`, the default). Either way you copy the listed refs into their prompt (`runners.md`).
- `record_run.py` checks every attempt against this: it counts the worker's memory calls (from the agent's transcript, or from the tool-call events in an external runner's JSON logs; `violation` is null when it cannot tell) and marks a search, a write, a `get` under `none`, or a `get` of a ref not in the PBI's Refs as `memory.violation` in `runs.jsonl`.

## External components

The bundled components cover only this skill and the skills built into Claude Code (`code-review`, `security-review`). This skill ships no one else's skill and no component for one. Any other skill, the user's own included, becomes an **external component**:

1. The user installs the skill with Claude Code's plugin commands (`/plugin marketplace add <repo>`, `/plugin install <plugin>@<marketplace>`), or keeps their own in `~/.claude/skills/<name>/`.
2. `python <skill>/component.py add <plugin>:<name> --stage <stage> [--after <stage>] [--when W] [--forbid TEXT]` (`--stage` is a stage, `gate`, or an optional stage: `ui-design`, `marketing`, `security`) reads the installed SKILL.md and writes `<home>/components/<stage>.<plugin>-<name>.json`: `skill` when the model can invoke it, `read-skill` when it has `disable-model-invocation` (plugin skills only; a personal skill must be model-invocable), with the license and link from the plugin's `plugin.json`. `--when`, `--where` and `--models` go on the template's entry (`{"id", "when", "where", "models"}`), not into the shared component file, so one template's settings never change another's (a later `add` keeps the entry's `where` and `models` unless given again; `--no-models` drops the models); registering the same component again with other settings is refused. It puts the component first in that stage of `<home>/workflows/<workflow>.json` (default `jev-routed`, copied from the bundled one when missing), last in the gate after `--after`, or last in the optional stage (placed after `--after`, or its default place, the first time). It checks the result like `flow.py` and leaves nothing on an error.
3. `component.py remove <id>` takes it out of that template and deletes the component, refusing while another home template still uses it; `component.py list` shows bundled and external components.
4. An example template (`examples/workflows/<name>.json`) carries a whole template plus, for each skill it uses, how to install and register it. `component.py example check <name>` shows which skills are missing and prints the install commands (exit 3 while some are missing; the script installs nothing). `component.py example use <name> [--as NAME] [--replace]` registers every skill and writes the template to home. `setup --recommended` drives this (`appendix/adapters.md`).

- A home component cannot reuse a bundled id; `flow.py` reports it as an error, so every stage keeps its fallback.
- Run `component.py` only when the user asks for that skill (or `setup --recommended`, `appendix/adapters.md`). The commands for the skills this skill was built around are in the README ("External components").

## read-skill components

A `read-skill` component points at a skill that the model cannot invoke (all pstack skills). Run it like this:

1. The status column gives the SKILL.md path (`plugin ok <path>`); `flow.py` looked under `~/.claude/plugins/` (or `PBI_PLUGINS_DIR`). With `plugin missing`, record `fallback (plugin not installed)` and take the next row.
2. When `where` is `main` (a skill that starts its own subagents, such as pstack's `interrogate`), read the harness notes and the SKILL.md yourself and follow them, with the component's `forbid` items. Go to step 3. Otherwise, start a subagent of the row's `where` type. In its prompt: the SKILL.md path to read and follow, the harness notes to read first when the plugin has them (pstack: `docs/harness/claude-code.md`, two levels above `skills/`), the component's `forbid` items, the request or the PBI path, and the output file below.
   - With `models`, start the skill's subagents on those models, one subagent per entry, in that order, in place of the models the skill picks (pstack reads them from a role line such as `interrogate reviewers`; the entries already have the Claude Code form `<model> <effort>`, and `inherit` means omit `model`). Without `models`, the skill picks as it says.
   - Worktrees the skill makes for its subagents (`isolation: "worktree"`, under `.claude/worktrees/`) are theirs to write in; the forbid on other files still holds in the work tree. When the skill is done, remove them with `git worktree remove --force <path>`; first copy into the output any repro that a finding cites.
   - Its subagents report to you with commands and output; keep only what the output file needs.
3. Output: a note in `docs/pbi/notes/` (`<slug>.<name>.md`, or `PBI-<n>.<name>.md` per PBI), the result file its `forbid` names, or, for an optional stage, its artifact folder (`appendix/optional.md`). Name the output in the prompt; it is "the output file named in the prompt" of the read-skill forbid, and a scratch folder outside the repository that the skill asks for is not a repository file. The subagent returns at most 5 lines and the path.
4. Link per-PBI notes from that PBI's Notes section, so its worker reads them. Workers never run plugin skills themselves.

## Skills and the Skill tool

Your usual definition (`agents/atelier-designer.md`) has no Skill tool, so the list of installed skills is not in your context. Whether a kind `skill` row is usable comes from the status `flow.py` printed: `skill ok <SKILL.md path>` (found on disk; a plugin's from its installed version) and `skill builtin` (built into Claude Code, such as `code-review`) are usable, `skill missing` is not. Do not scan the disk for skills.

- A component with `needs_skill_tool: true` calls the Skill tool inside, relies on `$ARGUMENTS`, or is a built-in skill in `main`. When an episode runs such a row in `main`, the conductor launches it on `atelier-designer-skills`, the same instructions with the Skill tool, and you invoke the skill with the Skill tool. `component.py add` sets it on every kind `skill` component (`--no-skill-tool` leaves it out); `flow.py` counts it only on rows in `main`. When the Skill tool says such a skill is not found, take the next row and record `fallback (<name>: not found)`.
- A kind `skill` row in `main` without `needs_skill_tool`: read the SKILL.md at the path of its status and follow it, as steps 2 to 4 of `## read-skill components` say.
- A `skill builtin` row has no SKILL.md to read. Without the Skill tool, take the next row and record `fallback (no Skill tool)`.
- A row in a subagent (`subagent`, `subagent-readonly`): the reviewer and the tester have the Skill tool and call the skill. The next row (in the end the fallback) runs only when one returns `BLOCKED` or says the skill is not found.

## STATUS.md

Write it at the start and keep it current with Edit. Read states and counts only from this file. It holds the head lines and the PBI table; the stage records go to `docs/pbi/stages.md` (`## Stage records`).

```markdown
Skill version: <version and published commit>
Work key: <repo>:<branch>:W-001 / Workspace: project:work.<repo>.<branch>.W-001
Workflow: jev-routed (<path printed by flow.py>)
Skipped runners: <--skip value and reason / none>
Tests at end: on (deferred: pnpm run test:unit, test_paths: e2e/**)
Kiln: on (designer 120000/180000, implementer 60000/90000, handoff 250000)
Tools missing: <none / tools_check.py rows that are not ok, and the fallback used>

| PBI | Model | State | Rework | Tier up | Continue | Note |
|---|---|---|---|---|---|---|
| PBI-001 | agent sonnet high (sonnet / normal) | running | 1/2 | 0/1 | 0/2 | criterion 2 fails |
```

- States: not started, running, in review, done, held, limit reached. Add a row for new work found on the way.
- The rework limit after the slash is the template's `fix.max_rework`.
- The version line lets a resume notice that the skill changed in between.
- `Tools missing` is a snapshot of one `tools_check.py` run, not the truth for later. On resume, and right before a stage whose rows need a tool (map, lint-survey, lint-recheck), run `flow.py` again (and `tools_check.py` when a row needs a CLI) and rewrite the line from that output. When STATUS.md and a fresh run disagree, the fresh run wins.

## Stage records

Write one line per stage and gate row that ran or was passed over for a reason, appended to `docs/pbi/stages.md` (inline, in HANDOFF.md too):

```
stage <stage|gate after stage>: <component> <run|skip|fallback|off> (<reason>) [findings <critical>,<major>,<minor>]
```

For example:

```
stage map after understand: map.builtin run (cache hit <folder>)
stage lint-survey after understand: lint-survey.jevlint run (model, 41 files, $0.02) leads 3, confirmed 1
stage design: design.builtin run (clearly complex)
stage lint-recheck after verify: lint-recheck.jevlint run (ast-only) worse 4, rework 1
stage review: review.code-review run (diff 412 lines > 300) findings 0,2,1
stage fix: fix.list run (round 1, 3 items, 2 workers) fixed 3, left 0, skipped 0
```

- A run whose STATUS.md already holds stage lines and that has no `docs/pbi/stages.md` (a run started before stage records moved out of STATUS.md) keeps them there: read the stage records from STATUS.md, and write the new ones to `docs/pbi/stages.md`.
- What the whole run tried and dropped goes to `docs/pbi/dead-ends.md`, not here (`episode/_kiln.md`, `## Run dead ends`).

- `skip`: its `when` did not hold. `fallback`: it was not usable or failed, and the next row ran. `off`: turned off in agents.json.
- Add `findings` only for a review or security component.
- The final review runs once: one `stage review` line, with no `#1` or `#2` (`appendix/review.md`, `## Findings`). The fix stage writes one line per round: `stage fix: fix.list run (round <r>, <n> items, <k> workers) fixed <a>, left <b>, skipped <s>` (`appendix/fix.md`). Finish copies one line per E2E round from `docs/pbi/verify/e2e.rounds.json`: `stage e2e: run #<k> (<n> specs, <f> failing, full suite <result>) next <fix|finish>`.
- Whether Jev can be reached is a check from this run, the same way a tool's presence is the fresh `tools_check.py` row and not the `Tools missing` line above. The `jev` row without `--probe` only says whether the key can be read; it does not call Jev. Connectivity is `tools_check.py --probe`, or a `prepare.py` line from calls made in that run: `Jev: ok n/n (<model>)` or `Jev: unavailable (<code>)`. `Jev: not-checked (cached ...)` means those gate files were reused. STATUS.md, HANDOFF.md, `docs/handoff` and memory are not evidence. On resume, ask again with `prepare.py --only <PBI ids>` and no `--tier`, and append a gate line; the earlier line stays. Example: `gate.jev run #2 (local Jev ok n/n)`.
- The approval list and the completion report copy these lines as they are.
