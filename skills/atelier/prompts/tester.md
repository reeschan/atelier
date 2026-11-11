# Prompt: test run

Give this to an `atelier-tester` subagent to run the deferred checks (`appendix/tests-at-end.md`) and write `docs/pbi/verify/tests/<name>.test-result.json`. Fill in the brackets. Keep only the line of the kind you start and delete the other two. It runs on the tester of the `Roles:` line (sonnet by default). A value ending in `@<runner>` (or any value when your prompt says `Agent tool: no`) runs through `role_run.py --role tester` with the `prompt:` part below, named `test-<name>` (`runners.md`, `## Roles on a runner`).

```text
description: TEST.<kind> <short text>   (kind: wave, survey or final)
subagent_type: atelier-tester
model: <the tester of the Roles: line, sonnet by default>
prompt:
Work root: <absolute path of the repository or worktree>. Work here and write nothing outside it.
Kind: <wave, survey or final>   Name: <result name, for example wave-1, survey or final>   Reports: <report,report,...>
Do not change source files or test files. Do not stash or reset to find a cause. Do not call review skills or subagents.
wave: run `python <skill>/verify.py <report>,<report>,... --deferred`, or call the skill named in "skill" of docs/pbi/tests-at-end.json when it has one. When that skill needs child agents of its own, run `verify.py --deferred` instead (you are at depth 2, your child has no Agent tool). Write each failure as a test name, file, PBI and a cause guess.
survey: read the tests in the range <the range from appendix/tests-at-end.md> with the principle at <path of the principle's SKILL.md, or none>. Write the table of the survey (file:test, verdict, reason, action) to docs/pbi/notes/test-survey.md. Do not put the counts of the verdicts in the JSON.
final: run `python <skill>/verify.py PBI-<n> --name <report>` once per report, then run the deferred commands with `python <skill>/verify.py <report>,<report>,... --deferred`. When the skill in docs/pbi/tests-at-end.json needs child agents of its own, run `verify.py --deferred` instead.
- Failing list so far: <docs/pbi/verify/tests-at-end.failing.md, or none>
- Send long output to a log file under docs/pbi/verify/tests/ and read only the exit code and the last 30 lines.
- cause is your guess: intended (a PBI changed it on purpose), regression (a behaviour that should have stayed), unknown. The designer decides.
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
