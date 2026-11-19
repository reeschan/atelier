<p align="center"><img src="assets/atelier-banner.jpg" alt="Atelier: a workshop with a girl stirring a glowing cauldron" width="100%"></p>

# Atelier

[![Tests](https://github.com/reeschan/atelier/actions/workflows/test.yml/badge.svg)](https://github.com/reeschan/atelier/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
[![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-D97757?logo=claude&logoColor=white)](https://claude.com/claude-code)

English | [日本語](README.ja.md)

Atelier is a Claude Code skill for implementation work that spans several files. It turns a request into an agreed spec, splits it into small work items (PBIs), routes each PBI to a model, builds and checks them, reviews the whole change once, and reports what it cost. It is not meant for a one-file fix.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/architecture-dark.svg">
  <img src="assets/architecture-light.svg" alt="Atelier v8 architecture. The developer talks to the caller, their Claude Code session. The caller loops over the conductor (conductor.py, a script with no model) with start, next, done, answer and run, and launches every model role at depth 1: surveyor, interviewer, planner, worker and reviewer. The conductor holds the run state machine and uses one template (difficulty levels L1 to L5 mapped to routes, plus roles and limits), the guard rules G1 to G14 and the run record. It runs external runners (Codex, Antigravity, opencode, dsh, claude -p) itself and reads only runner registration from ~/.config/atelier/agents.json. Roles and the conductor write to docs/pbi in the target repository (run.json, the fact sheet, the design tree, PBIs, record.jsonl and REPORT.md), and workers change the source code after reading the repository's rule documents. Approval pages are drawn with answer-me-with-html, and the surveyor updates the OMM map with omm-pr-scan --cache from a cache outside the repository; when the cache has no usable scan, the caller runs a full omm-scan if you ask. The caller also runs the repository's review skill once as the final review, and copies approved data from outside the repository into it.">
</picture>

The diagram source is `assets/architecture.d2` (rendered with [D2](https://d2lang.com) through the arch-diagram skill).

## How a run goes

1. **Survey.** A cheap model writes a fact sheet about the repository, every item with its source (`path:line` or an OMM element). The fact sheet also lists the rule documents the repository says to read before implementing, its check commands, and its own review skills. The OMM map comes from a cache outside the repository (the same place omm-pr-scan uses) and is updated from the nearest commit by diff. When no usable scan exists, you are asked first whether to scan the whole repository.
2. **Interview (optional).** When no complete spec is given, an interviewer asks numbered questions with recommended answers, round by round, until nothing is open, then writes the spec. You decide whether to skip this step.
3. **Plan.** The planner turns the approved spec, the fact sheet and the rule documents into PBIs. Before it fixes the files a PBI may change, it reads the code for callers, exhaustive tables and existing mechanisms with the same role, and records where it looked. Each PBI has a difficulty level (L1 to L5) with its reason, its checks, any outside data it needs with source and destination, and, when it needs care, a sensitive-candidate flag with a reason. The plan also names the repository-wide checks, the E2E commands and the review skill for the final review.
4. **Approve.** One page shows the roles and models, every PBI with its level, reason, rule and route, the sensitive candidates, the PBIs that go to external models, the limits and an estimate, and what is left to decide. Sensitive parts you mark leave the run and get a hand-off note.
5. **Build in waves.** If you approved outside data, the caller copies it into the repository first. Workers read the rule documents and build the PBIs whose dependencies are done. Each wave gets machine checks only (the wave's PBI checks and the plan's checks); exit codes decide. Workers run their checks themselves and fix what fails, at most twice; outside their targets they may change existing files (not `do_not_touch`), such as a test whose expected values the intended change made stale, and the report lists each such file and why. A failed check does not stop the run: it becomes a carry-over, and a fix PBI goes first in the next wave, before the PBIs that wait for it (after the last wave, the fix stage fixes it with the review's findings). A fix PBI may not change test files. Only when the same carry-over's fix PBI fails twice (the second one level up) do you choose: same route again, one level up, or hold. When a worker reports that the plan's assumptions failed, you choose: re-plan (passed PBIs stay, and you approve again), retry the same PBI, or stop. A run that ends with a carry-over left is reported as unfinished. See [docs/specs/carry-over-failures.md](docs/specs/carry-over-failures.md).
6. **One final review.** The caller runs the repository's review skill when it has one; otherwise a reviewer checks the change against the rule documents. Either way the whole change is reviewed once. Findings carry severity and confidence; the planner sorts the confident ones into fix, skip or ask. The fix-list page also shows skipped, low-confidence and style-only findings as candidates, and you can add any of them to the fix list.
7. **Fix and E2E.** Workers fix the approved list in groups that share no file. E2E runs the plan's E2E commands with no model (or is skipped, and the report says so, when there are none); failures go back to fixing up to the template's limit, then you are asked.
8. **Report and hand-off.** `docs/pbi/REPORT.md` lists tokens and cost by PBI, role and model, every guard rule's counts, and the first-pass rate by level and model. `docs/pbi/HANDOFF.md` names the work root, branch, base commit, goal, spec and report, so a new conversation can take the change to a pull request without the long run in its context; the caller asks whether to start one.

The agreed state diagram and its TLC check are in [docs/specs/atelier-v8.md](docs/specs/atelier-v8.md). The version with rule documents, check commands, re-planning, outside data and the OMM question is [docs/specs/fewer-review-loops.md](docs/specs/fewer-review-loops.md).

## Two layers

- **Caller**: your session. It runs the conductor's commands, launches the model roles it is told to, shows pages and passes your answers back. It designs nothing. It leaves the steps that need no model (checks, external runners, copying approved data) to `conductor.py drive` in the background, and comes back only when a model role, a skill or your answer is needed.
- **Conductor** (`conductor.py`): a script with no model. It keeps the run state in `docs/pbi/run.json` and returns one next action at a time. It counts retries, checks the budget, runs external runners with before/after checks, and turns anything it cannot decide by rule into a question for you.

The planner is called at four fixed points only: writing the PBIs, revising them when you ask at approval, re-planning when a worker reports a broken plan, and sorting the final review's findings.

## Template and levels

One template holds everything you can tune:

- `routes`: for each level L1 to L5, the runners, models and efforts to try, in order. Neighbouring levels may share a model with different efforts.
- `roles`: the model of each role (surveyor, interviewer, planner, reviewer, and optionally a separate model for fixes).
- `limits`: E2E fix rounds, and an optional token or cost budget.
- `parts` and `reading`: which parts run, and which rule texts and prompt sections a model reads.

Levels are defined by an expert's time estimate and whether design judgment is needed (`skills/atelier/atelier/levels.json`); they never name a model. You can switch to another template until a date. The shipped default is `skills/atelier/template.default.json`.

## Guard rules and the record

- [GUARDS.md](skills/atelier/GUARDS.md) lists the fourteen guard rules (G1 to G14), each with how it is stopped: before launch, refused by the conductor, checked afterwards, or, for two of them only, an instruction.
- `docs/pbi/record.jsonl` gets one line per launch (role, model, tokens or `unknown`, and the tokens read per rule and prompt section), per check and per fix. The template can switch a rule text or a section off, so runs with and without it can be compared by counts.

Terms are in [GLOSSARY.md](skills/atelier/GLOSSARY.md).

## Requirements

| Tool | Required | Used for |
|---|---|---|
| [Claude Code](https://claude.com/claude-code) | yes | the caller and the model roles |
| Python 3.10+ | yes | the conductor and its modules (standard library only) |
| Node.js and the answer-me-with-html skill | recommended | drawing the approval pages |
| oh-my-mermaid and the omm-pr-scan skill (v1.2+) | no | the OMM map for the survey; without them the surveyor reads the code |
| [Codex CLI](https://github.com/openai/codex), Antigravity CLI (`agy`), [opencode](https://opencode.ai), DeepSeek Harness (`dsh`), `claude` | no | external runners named in the template's routes |

## Install

```sh
git clone https://github.com/reeschan/atelier.git
cp -r atelier/skills/atelier ~/.claude/skills/
python ~/.claude/skills/atelier/setup.py runners
python ~/.claude/skills/atelier/setup.py models codex
```

`setup.py runners` finds the CLIs and records their paths and versions in `~/.config/atelier/agents.json` (runner registration only). `setup.py models <runner>` stores the runner's model list under `~/.config/atelier/models/`. Use `--home <directory>` for another folder, and for example `runners --codex <path>` for a CLI outside the `PATH`.

## Usage

In Claude Code, ask for the work and invoke `/atelier`. The caller writes your goal to a file and runs:

```sh
python skills/atelier/conductor.py start --root <work root> --goal-file <file> [--spec <spec file>]
```

then follows the actions of `drive`, `answer` and `start` as [SKILL.md](skills/atelier/SKILL.md) says. `drive` does every action that needs no model and stops at the first one that needs the caller; `drive --done <launch id>` first completes a subagent launch. PBIs of a wave whose `targets` do not overlap, and the fix groups, start together as one launch batch (`launches`), up to the template's `limits.parallel` (3 by default); `drive` runs an external batch at the same time, and the caller starts a batch of subagents in one message and completes it with one `drive --done`. The lower-level `next`, `done` and `run` remain for one step at a time. The run's files are in `<work root>/docs/pbi/`.

## Tests

```sh
cd skills/atelier
python run_tests.py
```

## License

[MIT](LICENSE)
