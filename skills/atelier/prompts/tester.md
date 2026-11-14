# Prompt: test run

Give this to an `atelier-tester` subagent to run the deferred checks (`appendix/tests-at-end.md`) and write `docs/pbi/verify/tests/<name>.test-result.json`. Fill in the brackets. Keep only the line of the kind you start and delete the others. It runs on the tester of the `Roles:` line (sonnet by default). A value ending in `@<runner>` (or any value when your prompt says `Agent tool: no`) runs through `role_run.py --role tester` with the `prompt:` part below, named `test-<name>` (`runners.md`, `## Roles on a runner`). E2E after the last wave is not the tester's: the `e2e` episode runs it as a script.

```text
description: TEST.<kind> <short text>   (kind: wave, investigate, survey, final or full)
subagent_type: atelier-tester
model: <the tester of the Roles: line, sonnet by default>
prompt:
Work root: <absolute path of the repository or worktree>. Work here and write nothing outside it.
Kind: <wave, investigate, survey, final or full>   Name: <result name, for example wave-1, investigate-1, survey or final>   Reports: <report,report,...>
Do not change source files or test files. Do not stash or reset to find a cause. Do not call review skills or subagents.
Test rules: docs/pbi/tests-at-end.json (approved by the user; the project's own rules are in it). Follow its `investigate`, `limits` and `notes`; they win over your habits.
wave: run `python <skill>/verify.py <report>,<report>,... --deferred --when wave`, or call the skill named in "skill" of docs/pbi/tests-at-end.json when it has one. When that skill needs child agents of its own, use verify.py instead (you are at depth 2, your child has no Agent tool). Run no E2E spec. Write each failure as a test name, file, PBI and a cause guess, write the result, then run `python <skill>/role_results.py classify docs/pbi/verify/tests/<name>.test-result.json`.
investigate: the failures to look into: <test, file and the result file they came from>. Run each failing test or spec alone (a spec with the `investigate` command, a unit test with the runner's file filter), read its output, find the files it imports or visits and the PBI whose diff touches them (`git diff --name-only` and the PBIs' Targets). For each, write cause regression (a behaviour the PBI should have kept), intended (the PBI changed it on purpose: its Notes, its goal or the diff renames or removes what the test reads) or unknown, with the evidence in why (the failing assertion and the line of the diff). Write recommendation rework when a regression is clear, otherwise needs-designer.
survey: read the tests in the range <the range from appendix/tests-at-end.md> with the principle at <path of the principle's SKILL.md, or none>. Write the table of the survey (| file:test | verdict | reason | action |; file:test is <file>:<test>, or <file> alone for an add) to docs/pbi/notes/test-survey.md. A delete's reason starts with `covered by <file:test>` or `none needed`. Under the table, for every test file with a fix, rewrite or add row, write a `### Cases: <test file>` heading, one Exemplar: line (the closest existing test in style) and the table | id | behaviour | input | expected | mock | with literal values (appendix/tests-at-end.md, ## Test cases), taking the cases from the ## Test cases of the build reports <the reports> where they cover the row. Write ? in a field you cannot decide from the requirement; never take an expected value from what the code returns now. Do not put the counts of the verdicts in the JSON.
final: run `python <skill>/verify.py PBI-<n> --name <report> --skip-deferred` once per report, then `python <skill>/verify.py <report>,<report>,... --deferred --when end`, then, when there are specs to run again, once `python <skill>/verify.py --related <the specs that failed in the e2e episode and the specs a test PBI changed> --each --name final-related` (with `Specs again: none`, skip it). Never run the full E2E suite.
Specs again: <for final: the specs, joined with commas, or none>
full: run `python <skill>/verify.py --final --name <name>` once. It refuses without the approval; do not run the suite another way.
- Every command you start yourself has a limit (`timeout` in bash, or verify.py's limits). Run a failing spec again at most `limits.retries` times. When a limit is hit, stop and write recommendation needs-designer with the limit in `why`.
- After a narrowed run, check the spec count in the output (for Cypress, `Running: x of N`). When it ran more specs than you asked for, stop: the filter did not apply. With pnpm, `pnpm run <script> -- --spec x` may pass `--` on to the tool and drop the filter; use the form in `investigate`.
- Failing list so far: <docs/pbi/verify/tests-at-end.failing.md, or none>
- Send long output to a log file under docs/pbi/verify/tests/ and read only the exit code and the last 30 lines.
- cause is your guess: intended (a PBI changed it on purpose), regression (a behaviour that should have stayed), unknown. The designer or the lead of the test stage decides.
- <the implementer block of prompts/kiln-chamber.md, when kiln is on>
- Write docs/pbi/verify/tests/<name>.test-result.json, then run `python <skill>/role_results.py check` on it until it prints ok:
  {"schema": "atelier.test-result/1", "kind": "wave",
   "commands": [{"command": "<command>", "code": 0}],
   "failing": [{"test": "<file::name>", "file": "<file>", "pbi": "PBI-<n>", "cause": "regression", "why": "<why>"}],
   "recommendation": "pass"}
  recommendation: pass, rework (a PBI must be redone), or needs-designer.
Write the result at docs/pbi/verify/tests/<name>.test-result.json; the result file is what counts. Your final message is one line, and may be empty.
```

Use the result as input to your own decision, never as the decision.
