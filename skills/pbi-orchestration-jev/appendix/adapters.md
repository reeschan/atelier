# Appendix: adapters

Read this before the design, at the approval, after all PBIs are done, and when the argument is `setup --recommended`.

An adapter runs a callee skill that the user installed, at a fixed phase. When no callee is available, the phase runs its built-in procedure, so every run works without any adapter.

## Phases

1. **Design**: before the work is split into PBIs.
2. **Final review**: after all PBIs are done.
3. **Security review**: with the final review, when a sensitive or deep PBI exists.

Do not use adapters in the per-PBI review (SKILL.md section 8).

## Known callees

Only the rows below are used automatically. Within a phase, the order is the priority.

| Phase | Callee | Order | Pass in | Get back | Never pass | Link | License | Interactive | Calls back |
|---|---|---|---|---|---|---|---|---|---|
| design | `mattpocock-skills:grilling` | 1 | the request; runs in the main session | decisions, in the conversation | `--fix`, `--comment`, `ultra`, `--max-findings` | https://github.com/mattpocock/skills | MIT | yes | no |
| design | `mattpocock-skills:domain-modeling` | after grilling | the request; only if GLOSSARY.md or CONTEXT.md exists in the target repository | terms, in the conversation | `--fix`, `--comment`, `ultra`, `--max-findings` | https://github.com/mattpocock/skills | MIT | yes | no |
| review | `mattpocock-skills:code-review` | 1 | base (merge-base), head and the spec path, in the subagent prompt | findings; the subagent writes the result file | `--fix`, `--comment`, `ultra`, `--max-findings` | https://github.com/mattpocock/skills | MIT | no | no |
| review | `code-review` (built-in) | 2 | the argument `medium` (level only); base and head in the prompt | findings; the subagent writes the result file | `--fix`, `--comment`, `ultra`, `--max-findings` | built-in | n/a | no | no |
| security | `security-review` (built-in) | 1 | no argument | findings; the subagent writes the result file | `--fix`, `--comment`, `ultra`, `--max-findings` | built-in | n/a | no | no |

`Calls back` says whether the callee calls this skill back. A callee that does would loop, so none is in the table. Built-in rows ship with Claude Code, so they need no install.

## Finding callees

1. Find a callee by its name in the skill list in your context. Do not scan the disk.
2. A name that is not in the table is used only when `agents.json` names it (see `## Settings`).
3. Never use a skill that the model cannot invoke (`disable-model-invocation`).
4. When a name is not in the skill list, that callee is not available. Go to the next row, or the built-in procedure, and record `fallback` with the reason.

## Settings

`agents.json` in home may hold an `adapters` object. A missing key means `auto`.

```json
{"adapters": {"review": "off"}}
```

- Keys: `design`, `review`, `security`. Values: `auto` (default), `off`, or a skill name matching `^[A-Za-z0-9][A-Za-z0-9:_./-]{0,127}$`. A skill name replaces the table for that phase.
- `load_adapters` in `agents.py` reads this. Other keys in `agents.json` are left alone.
- An invalid entry is ignored and its phase uses `auto`. The warnings are:
  - `adapters: not an object, using auto for every phase`
  - `adapters.<key>: unknown phase, ignored`
  - `adapters.<key>: invalid value, using auto`
- A missing, unreadable or broken `agents.json` means `auto` for all phases.
- `adapter_gate.py` prints the warnings as `warn <warning>` lines after its table. Give each one to the user in one line.

## Design phase

Entry rule:

1. If this run starts with a spec path (a `docs/specs/*.md` file in the request, or a handoff from a design skill that passes one), the design is done. Skip this phase and record `built-in skip (spec given)`.

Read the setting next: run `python <skill>/adapter_gate.py docs/pbi` (no PBIs are needed yet) and take the `design` row's setting.

- `off`: skip this phase without asking, and record `built-in off (turned off in agents.json)`.
- A skill name: in step 5, use that skill instead of the table's callees when it is in the skill list. When it is not, use the table's order and record the fallback with its reason.
- `auto`: use the table's order.

Decide before the decomposition, from the request and a short look at the repository:

2. **Clearly complex** (run without asking): the request adds a new concept, an external interface, or a data-model change, and the work likely spans 2 or more modules.
3. **Clearly simple** (skip without asking): the direction is already set (a spec exists, or the work continues earlier work), or the work stays in one module with 3 or fewer PBIs and adds no new concept.
4. **Otherwise**: ask once with AskUserQuestion, before the decomposition, whether to run the design step. Save the answer in `docs/pbi/adapters.json` as `{"design": {"decision": "run|skip", "asked": true}}`. On resume, read it and do not ask again. Do not store it in `docs/pbi/state.json`, which the web sync overwrites.

When the decision is run:

5. When the setting names a skill that is in the skill list, run that skill in the main session, and skip the table's callees. Otherwise, if `mattpocock-skills:grilling` is in the skill list, run it in the main session, and run `mattpocock-skills:domain-modeling` after it only when GLOSSARY.md or CONTEXT.md exists in the target repository.
6. Then write `docs/specs/<slug>.md` yourself, where `<slug>` is a short lowercase name for the work. Use these headings in this order: Problem Statement, Solution, User Stories, Implementation Decisions, Testing Decisions, Out of Scope, Further Notes. The callees do not write the spec.
7. When step 5 ran no callee (neither the named skill nor grilling is in the skill list), write the design yourself in the same file: the decisions you made and the assumptions you put in. Record `built-in fallback (grilling not in the skill list)`. List the assumptions in the approval list under "Open assumptions", a few lines each, so the user can correct them there.
8. When step 7 applies and the work is clearly complex, add one line to the approval list, in the user's language: "To deepen the design, run `setup --recommended` to install mattpocock-skills." Then write `<home>/notices.json` as `{"recommended_hint_shown": "<ISO datetime>"}`. If that key is already there, do not show the line again.

## Final review and security review

1. Before the review, run these from the repository root. `<base>` is the branch the work will merge into (usually `origin/main`). The PBIs' changes are not committed yet, so compare the working tree with the merge-base, not `..HEAD`. `git add -N` makes the new files show up in the diff without staging their content. Name only the new files from the PBIs' targets: `git add -N .` would also count unrelated untracked files.

```
git add -N -- <each new file the PBIs created>
git diff --numstat $(git merge-base <base> HEAD) > docs/pbi/verify/final.numstat
python <skill>/adapter_gate.py docs/pbi --numstat docs/pbi/verify/final.numstat
```

2. Read the table: phase, setting, decision, reason.
   - `run`: go on to steps 3 to 6.
   - `skip`: do not call a callee. Record the reason.
   - `off`: the phase is turned off in `agents.json`. Record it.
   - `manual` (design only): apply `## Design phase`.
   - setting: `auto`, `off`, or the skill name from `agents.json`.
3. Pick the callee. For review, use the setting's skill name when it is in the skill list. Otherwise use the first review row of `## Known callees` whose name is in the skill list. If none is, use the built-in procedure (step 7). For security, use the setting's skill name, or `security-review`. With `auto`, security uses only the built-in `security-review`, which runs inside Claude Code. A skill named in `agents.json` is the user's choice: the orchestrator does not check where that skill sends code. The argument is `medium` for `code-review` (built-in), and none for the others. Base, head and spec go in the prompt, never in the argument.
4. Take the baseline: record `git rev-parse HEAD` and `git status --porcelain -- . ':!docs/pbi/reviews'`. The numstat file from step 1 must exist first. The pathspec leaves out the result file the subagent writes.
5. Run the callee in a subagent with the template below, filled in with the real values. The subagent type is `general-purpose`, not `pbi-worker`, which has no Skill tool.

```text
description: FINAL.review <short description>   (FINAL.security for security)
subagent_type: general-purpose
prompt:
Final review of this repository's work. Report only: do not edit, commit, push or post anything.
- base: <merge-base sha>; head: <HEAD sha>; spec: <docs/specs/<slug>.md, or none>
- Call the Skill tool with <callee name>. For the built-in code-review, pass the argument medium; for the others, pass no argument.
- Never pass --fix, --comment, ultra or --max-findings.
- If the skill asks you a question, do not answer it. Stop and write BLOCKED.
- Write <docs/pbi/reviews/final.review.md, or docs/pbi/reviews/final.security.md for security>.
  Line 1: findings: <重大>,<中>,<軽微>
  Then one line per finding: - [重大|中|軽微] <file>:<line> — <why> — <repro>
  If you stopped on a question, line 1 is BLOCKED: <reason>, with no finding lines.
- Your final message: the three counts and one line per finding, nothing else.
```

6. When the subagent returns, take the same two values again. If HEAD or the status differs, discard the result, use the built-in procedure (step 7), and record the reason. Otherwise read line 1 of the result file. `BLOCKED` means the callee asked a question: use the built-in procedure. Otherwise go to `## Findings`.
7. Built-in procedure: for review, the whole-diff check at completion in `appendix/finish.md`. For security, there is no other fallback. When `security-review` is not in the skill list, record `fallback` and say in the completion report that no security review ran.

## Findings

1. Check each finding yourself before acting: open the file at the line and confirm the claim. Do not accept it because the callee reported it.
2. A confirmed finding in a file that a PBI's target list names becomes a rework of that PBI. Count it toward the limit of 2 (see `appendix/rework.md`).
3. A confirmed finding in a file that no PBI owns goes into the findings list of the completion report.
4. A remark about taste or wording is not a reason for rework. Drop it.

## Records

1. Write one line per phase in STATUS.md and in HANDOFF.md:

```
adapter <design|review|security>: <callee or built-in> <run|skip|fallback|off> (<reason>) findings <重大>,<中>,<軽微>
```

2. Add `findings` only when a review or security callee ran.
3. Example: `adapter review: built-in code-review run (diff 412 lines > 300) findings 0,2,1`
4. The approval list shows each adapter planned to run, one line each, with its phase, callee and gate reason.

## Recommended set

Only mattpocock-skills is recommended. The two built-in reviews need no install. The link is https://github.com/mattpocock/skills. The license is MIT (Copyright (c) 2026 Matt Pocock). This skill does not ship it.

For `setup --recommended`:

1. Show the name, the phases it serves (design: grilling and domain-modeling; review: code-review), the link, the license, and the install command.
2. Ask once with AskUserQuestion: install now, or not.
3. On yes, run `claude plugin install mattpocock-skills@claude-plugins-official --scope user` yourself. Do not put the install in a bundled script.
4. After it succeeds, tell the user to run `/reload-plugins` or restart Claude Code. Until then the skill is not used.
5. On no, or when the `claude` command is missing, only show the command and stop.
