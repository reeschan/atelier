# Appendix: cases for an outer harness

Read this when you record a rework, a tier up or a hold (the cause), and at finish (`export_cases.py`). Atelier only records and writes. It does not build prompts for another harness or change its own instructions from these records. An outer harness (harness2, for example) reads the cases, maps each element onto its own components, replays the PBIs and proposes changes to the instructions; a person adopts a change by editing `worker.md` or the plan appendix in a pull request.

## Cause

Give `record_run.py --cause` for every rework, tier up and hold. Pick what would have prevented the failure, more than one when they all apply:

| Cause | When | Root cause |
|---|---|---|
| `spec` | The PBI, its brief or the rework note said too little or something wrong: a missing Target, an undecided behavior, a wrong verify command that you did not fix as your own mistake | `task` |
| `convention` | The change breaks a convention of the repository: a rule in a CLAUDE.md that applies to the changed files, or a lint or format check | `harness` |
| `scope` | A change outside the Targets, or in the "Do not touch" list | `harness` |
| `memory` | A listed ref was not read (`missed_refs`), or shared memory was used beyond what the template allows (`memory violation`) | `harness` |
| `model` | The PBI and refs were enough, and the model still fell short | `agent` |
| `env` | Permissions, the network or a tool failed | `env` |
| `grader` | The reviewer, the tester or a verify rule was wrong: the send-back itself was a mistake | `grader` |
| `flaky` | A rerun passed; the attempts split between pass and fail | `noise` |

`spec` maps to the task, `convention`, `scope` and `memory` to the harness, and `model` to the agent. `grader` is the reviewer, the tester or a verify rule being wrong, and `flaky` is noise. The root cause column is what an outer analyzer (harness2-analyzer) records as `triage.labeled`.

`spec`, `convention`, `scope`, `memory` and `grader` can be fixed by the instructions (for `grader`, the reviewer and tester definitions and the verify rules); `model` and `env` cannot. `flaky` is noise, so no instruction fixes it: a rerun decides.

## The case

`export_cases.py` writes one JSON line per judged PBI to `docs/pbi/cases.jsonl` and copies it next to `runs.jsonl` in the archive. Passed PBIs are included, so a later comparison has the cases that went well too. Schema `atelier.case/1`:

| Key | Holds |
|---|---|
| `id`, `repo`, `work_key`, `pbi` | Which PBI. `id` is `<repo>:<work key>:<PBI>` |
| `base_commit` | `git rev-parse HEAD` when the first attempt was recorded. A PBI with `task.depends_on` also needs its dependencies' changes to replay |
| `harness.system_prompt` | `docs/pbi/worker.md`: path, version (the first 12 characters of its sha256) and text |
| `harness.guardrails` | The CLAUDE.md files that applied: the root one and those in the parent folders of the Targets. Kind `convention`, path and version; no text |
| `harness.memory` | The PBI's Refs: id, gist, and `fetched` (null when it could not be checked) |
| `task` | The PBI's sections: `title`, `goal`, `targets`, `do_not_touch`, `acceptance` (numbered), `verify` (number and command), `notes`, `depends_on` |
| `size` | How big the PBI was: `facts` (the estimate in `PBI-<n>.facts.json`), the first `tier`, the number of `targets`, `acceptance`, `verify` and `refs`, and `chars`: the length of `worker.md`, of the PBI when its first attempt was recorded (`input.pbi_chars` in `runs.jsonl`), and of the refs' gists written in the PBI (`refs`; the bodies the worker fetches are not counted) |
| `result` | `first_pass`, `passed`, `sent_back`, and per attempt: report, runner, model, effort, tier, outcome, cause, findings, verify exit codes, memory and "Do not touch" violations, worker version, `tokens` (total) and `token_parts`, `seconds`, `steps`, `cost` (reported, else the API equivalent), archive folder. `totals` adds up seconds, steps, tokens and cost over every attempt, reworks and unusable ones included, with the number of attempts whose value is unknown |
| `private.rework` | The Expected and Repro items of each rework note. For scoring only: an outer harness must not show them to the worker it replays |

The rows of `runs.jsonl` also carry `input.pbi_chars`, `judged.cause`, `prompt.worker` (the version of `docs/pbi/worker.md`), `base_commit` and `memory.missed_refs`, so a change to `worker.md` can be compared before and after from the records alone. `size` and `totals` let an outer harness compare time and tokens against the size of the PBI, the number of refs and the length of the instructions, to decide whether PBIs should be smaller and what the always-on text and the refs should carry.

## Hillclimb

Read this when you improve the instructions across rounds. An outer analyzer (harness2-analyzer) judges each change by replaying cases, so every step below leaves a record; the bodies stay local.

1. Make the split before you compare rounds. `python <skill>/events.py split init --seed <seed>` writes `~/.config/pbi-orchestration-jev/splits/<name>.json` once. At each finish, `python <skill>/events.py split assign --dir docs/pbi` gives a new case its split from that seed, and a case never changes split.
2. A held-out case that you read is no longer held-out. `python <skill>/events.py split retire --case <case_id> --dir docs/pbi` moves it to `regression` (reason `read-heldout`) and emits `split.retired`.
3. Hand the improver the improver view only. `python <skill>/export_cases.py --view improver` writes `docs/pbi/cases.improver.jsonl`: a held-out case keeps only `id`, `size` and `result.passed`, and its `task`, `harness.memory` and `private` are dropped. The default view (`owner`) keeps the whole case for you.
4. Change one kind per round. When you raise the skill version, record the change: `python <skill>/events.py change apply --dir docs/pbi --kind <kind> --target <target> --hypothesis <lineage_id>`. A change whose `config_fp` moved in two or more kinds is `multi`: it is left out of the judgement, so split it and try again.
5. Check the diff before the adopt PR: `python <skill>/events.py leakcheck --dir docs/pbi --change <change_id> --diff <patch>`. It compares the added lines with the rework notes, the held-out cases and the review notes, writes `change.checked` and prints the verdict as JSON. Exit 0 is `pass` or `warn` (`warn` means it overlaps a train failure: reread the lines first). Exit 3 is `block`: held-out or rubric text leaked, so do not adopt that change. Any other exit code (2: a usage error, an unreadable source or split manifest, a held-out case that cannot be found, or a crash), or no JSON on stdout, means the check did not run: do not adopt the change; fix the cause and run it again.
6. Take the analyzer's return in with `python <skill>/events.py ingest --file <judgement.jsonl>`. It writes `round.judged`, and the analyzer's cluster assignments as `cluster.assigned`.
7. Keep the trial number and the change ids with the record, so a later round compares the same condition and a stalled round can be triaged (see `appendix/finish.md`).
