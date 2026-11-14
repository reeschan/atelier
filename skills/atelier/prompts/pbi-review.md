# Prompt: review of one report

Give this to an `atelier-reviewer` subagent for every report (a first try or a rework) to check it and write `docs/pbi/reviews/<report>.review.json`. Start it only after every worker of the wave has returned. Fill in the brackets. The reviewer's model comes from the `Roles: reviewer deep=… other=…` line the conductor passes: deep for a deep PBI, other otherwise. A normal review uses other even when other built the attempt: the reviewer starts with a fresh context. A deep review whose deep model built the attempt uses other. Inline, with no Roles line, use opus for a deep PBI and sonnet at high effort otherwise. When a deep review finds both stages equal to the builder's model, use whichever of opus and sonnet at high effort on agent is not the builder's model; never start a reviewer on the builder's model for a deep review. A stage value ending in `@<runner>` (or any stage when your prompt says `Agent tool: no`) runs through `role_run.py --role reviewer` with the `prompt:` part below (`runners.md`, `## Roles on a runner`); name it `<report>.review`.

```text
description: PBI-<n>.review <short text>   (<report>.review for a rework, for example PBI-<n>.rework-1.review)
subagent_type: atelier-reviewer
model: <from Roles: reviewer deep=… other=…, deep for a deep PBI and other otherwise; a deep review whose deep model built the attempt uses other; with no Roles line, opus for a deep PBI and sonnet at high effort otherwise>
effort: <omit for opus; high for sonnet>
prompt:
Work root: <absolute path of the repository or worktree>. Work here and write nothing outside it.
PBI: docs/pbi/PBI-<n>.md   Report: <report name, for example PBI-<n> or PBI-<n>.rework-1>   Stage: <deep or normal>
Built on: {runner} {model}
Files that parallel PBIs touch: <the same line the worker got, or none>
Deferred verify commands: <numbers from `verify.py PBI-<n> --list-deferred`, or none>
Read docs/pbi/PBI-<n>.md, docs/pbi/reports/<report>.md and the refs in its Refs section (get them by id). Then, in this order:
1. Run `python <skill>/verify.py PBI-<n> --name <report> --skip-deferred`. Pass or fail is its exit code, never your judgement.
- When `verify.py` exits 2 because it cannot read the commands, run them by hand, each to its own log. When it prints `refused:`, do not run them by hand: write the refusal as a critical finding and recommendation hold (the designer fixes the PBI's verify commands).
2. Run `python <skill>/unchanged.py check PBI-<n>`. A changed "Do not touch" file is a finding.
3. Judge the acceptance criteria one by one: pass, fail, or deferred (its verify command is in the deferred list). Each gets a reason in one line.
4. Read `git diff -- <targets>`. A deep PBI: read the whole diff, narrowed to auth, permissions, input checks, data loss and module contracts. Any other PBI: read it against the acceptance criteria and the spec.
- Check the diff against the CLAUDE.md files that apply to the changed files; a broken rule there is a finding with cause `convention`.
- A bug the acceptance criteria cannot catch gets a small script in `docs/pbi/tools/` that shows it, used as the finding's repro.
5. Write docs/pbi/reviews/<report>.review.json (below). Copy the model and runner of the Built on: line into "built". "verify.failed" holds the numbers of the failed verify commands, copied from docs/pbi/verify/<report>.json.
6. Run `python <skill>/role_results.py check docs/pbi/reviews/<report>.review.json` and fix the file until it prints ok.
- On a deep review, if the Built on: model is the model you run on, write nothing and stop. A normal review goes on.
- Formatting and lint are done: verify commands 1 and 2 ran the project's formatter and linter. A finding about formatting, whitespace or a lint rule alone is not a finding; leave it out.
- A rework note with `Minor` lines (a minor of the final review the user chose to fix): judge each one as fixed or not, and write a finding for each that is not.
- Do not edit any other file. Do not call review skills or subagents. Do not run jevlint.
- <the implementer block of prompts/kiln-chamber.md, when kiln is on>
- Findings: critical = an acceptance criterion fails or something breaks; major = drift from the spec or an open behaviour decided; minor = a small mismatch that can mislead (a wrong name, a stale comment, a missing check). Taste and wording are not findings. One entry per problem that should block the merge, with file, line, why and a repro.
- recommendation: pass (no fail, no critical or major finding, and no minor of the rework note left unfixed), rework (fixable by the same model, including a minor of the rework note left unfixed), tier-up (needs a stronger model), hold (the designer must decide).
JSON, with these keys:
{"schema": "atelier.review/1", "report": "<report>", "pbi": "PBI-<n>", "reviewer": {"model": "sonnet", "effort": "high"},
 "built": {"runner": "agent", "model": "opus"}, "verify": {"failed": []},
 "criteria": [{"n": 1, "verdict": "pass", "why": "verify 1 exited 0"}],
 "findings": [{"severity": "major", "file": "<file>", "line": 1, "why": "<why>", "repro": "<repro>"}],
 "recommendation": "pass"}
Write the file at docs/pbi/reviews/<report>.review.json; the result file is what counts. Your final message is one line, and may be empty.
```

Use the result as input to your own decision, never as the decision. Run `python <skill>/role_results.py bundle` yourself once every review of the wave is written.
