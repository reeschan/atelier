# Appendix: events and task profiles

Events are the identifier-only record of a run: one checked line per event in `docs/pbi/events.jsonl` (schema `atelier.event/1`), with no prose and no file contents. Sending is off until the user runs `enable`; the sender reads its settings from `~/.claude/harness-log.json` and, once enabled, spools and pushes the same lines, while a machine without it keeps the local file only.

## When to emit

| Moment | Command |
|---|---|
| A stage record is written (`appendix/engine.md`, `## Stage records`) | `python <skill>/events.py stage --dir docs/pbi --component <id> --decision <decision> --by <by> [--reason <reason>] [--stage <stage>] [--pbi <PBI>]` |
| The plan is settled, once per PBI (`size.lines` is still 0 then) | `python <skill>/events.py profile --pbi PBI-<n> --task-type <type> --domain <domain> --difficulty <low\|mid\|high> [--base <commit>]` |
| finish, for the whole run | `python <skill>/events.py profile --run --task-type ... --domain ... --difficulty ...`, then `python <skill>/events.py backfill --dir docs/pbi`, which measures the size of every PBI again from the churn of its Targets since the base commit and emits `task.profiled` again when the size or the anchors change |
| Inline, after the user answers a question | `python <skill>/events.py answer --dir docs/pbi --kind <kind> --topic <topic> [--choice <choice>] [--wait-s <seconds>] [--revise]` |

- `--decision`: run, skip, fallback, off, repo, cache, report-only, fix, next-runner, stop, send, ast-only. `--by`: user, default, auto, fallback. `--reason`: when-true, when-false, tool-missing, user-off, spec-given, design-skip, approved, declined, error, other.
- `--task-type`: feature, bugfix, refactor, test, docs, infra, migration, perf. `--domain`: frontend, backend, cli, library, data, infra, docs, skill. `--difficulty`: low, mid, high. A body that cannot be embedded leaves `anchors` null; `backfill` re-embeds those from `docs/pbi/cases.jsonl`.
- `--kind`: approve, rework-limit, destructive, send-approval, decision, handoff-loop, launch-failure. `--topic`: see below. `--choice`: one lowercase letter (a to z), `yes`, `no` or `revise`, else leave it out. Under the conductor the conductor records the answer: emit `answer` only inline.
- `profile` and `backfill` go through `task_profile.py`.

## Question topics

An episode result's `question` may carry an optional `topic` (`appendix/episodes.md`, `## Episode result`); without one the conductor counts `other`.

| Question | `topic` |
|---|---|
| An unusable runner (another route step, wait, or hold) | `runner-unavailable` |
| An OMM cache miss (repo, cache or skip) | `omm` |
| A lint recheck rework candidate | `recheck` |
| A design unknown the code does not settle | `unknown` |
| The `ask` blocks of Approve | `approve-ask` |
| Approve itself (kind `approve`) | `design-approval` |
| The conductor's own no-agent-tool decision | `headless` |
| Anything else | `other` |

## The conductor and settle

The conductor emits `run.started`, `run.ended`, `episode.started`, `episode.ended`, `question.asked` and `question.answered`; `record_run.py`, `role_results.py`, `kiln_log.py`, `design_record.py` and `lint_run.py` add their own type beside their write. After an episode the conductor starts `events.py settle` in the background: it adds `episode.cost` from `agent_stats.episode_cost` (`method` `unmeasured` when it cannot measure) and calls the sender's `flush`. `ATELIER_EVENTS_SETTLE` chooses: `off` never, `sync` in the same process, anything else (the default) in the background. No event, sender, settle or flush failure stops or delays the run; the failure is one line in `docs/pbi/.events/errors.log`.

## Envelope

The optional envelope keys ride at the top of a line, not in `data`: `split`, `change_id`, `config_fp`, `trial`, `unit_id` (`split_set_id` comes with `split`). `events.emit` fills them from the active change (`changes.current_envelope`), the case's split (`splits.split_of`), the environment `HARNESS_TRIAL` and an explicit `envelope=` argument; a value that fails the allowlist is dropped and named in `dropped`. The conductor's events carry `unit_id` `at:<run_id>:<pbi>:<attempt>`.

## Hillclimb

The five commands below are the atelier side of the hillclimb loop; the steps that use them are in `appendix/cases.md`. Each one delegates from `events.py` to its own script.

| Command | What it does |
|---|---|
| `python <skill>/events.py split init --name default --seed <n>` | Makes the frozen train / heldout / regression split in home; the next two keep it. |
| `python <skill>/events.py split assign --dir docs/pbi --name default` | Assigns the new cases of `docs/pbi/cases.jsonl` and emits `split.assigned`. |
| `python <skill>/events.py split retire --dir docs/pbi --case <case_id> --reason read-heldout` | Moves a read held-out case to regression and emits `split.retired`. |
| `python <skill>/events.py change apply --dir docs/pbi --kind <kind> --target <id> --hypothesis <lineage>` | Records one change in `changes.jsonl` and emits `change.applied`; later events carry its `change_id`. |
| `python <skill>/events.py leakcheck --dir docs/pbi --change <ch> --diff <file>` | Compares a prompt or appendix diff with held-out task text and failure prose; exit 0 is `pass` or `warn`, exit 3 is `block`, and it emits `change.checked`. Any other exit code (2: a usage error, an unreadable source or split manifest, a held-out case that cannot be found, or a crash), or no JSON on stdout, means the check did not run: do not adopt the change; fix the cause and run it again. |
| `python <skill>/events.py ingest --dir docs/pbi --file <jsonl>` | Takes the analyzer's `round.judged`, `triage.labeled` and `cluster.assigned` lines and emits them here; the same script says whether accepts have stalled. |
| `python <skill>/events.py failure-embed --dir docs/pbi` | Embeds reworks as failure vectors in the archive for clustering; it emits no event. |

Do not put prose, a path, a prompt or a secret in `data`: only identifiers and numbers pass the allowlist, and anything else is dropped. Do not wait for the sender.
