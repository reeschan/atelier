# Appendix: cases for an outer harness

Read this when you record a rework, a tier up or a hold (the cause), and at finish (`export_cases.py`). Atelier only records and writes. It does not build prompts for another harness or change its own instructions from these records. An outer harness (harness2, for example) reads the cases, maps each element onto its own components, replays the PBIs and proposes changes to the instructions; a person adopts a change by editing `worker.md` or the plan appendix in a pull request.

## Cause

Give `record_run.py --cause` for every rework, tier up and hold. Pick what would have prevented the failure, more than one when they all apply:

| Cause | When |
|---|---|
| `spec` | The PBI, its brief or the rework note said too little or something wrong: a missing Target, an undecided behavior, a wrong verify command that you did not fix as your own mistake |
| `convention` | The change breaks a convention of the repository: a rule in a CLAUDE.md that applies to the changed files, or a lint or format check |
| `scope` | A change outside the Targets, or in the "Do not touch" list |
| `memory` | A listed ref was not read (`missed_refs`), or shared memory was used beyond what the template allows (`memory violation`) |
| `model` | The PBI and refs were enough, and the model still fell short |
| `env` | Permissions, the network or a tool failed |

`spec`, `convention`, `scope` and `memory` can be fixed by the instructions; `model` and `env` cannot.

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
