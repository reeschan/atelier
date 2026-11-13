> **注記:** cloud container 2026-11-10 の記録。API キーと外部通信が無い環境での結果で、ユーザー PC の状態ではない。Jev やツールが使えるかは、その回の確認結果で判断すること。

# State tablet (designer, before Approve)

## Objective
atelier v6: the caller only talks with the user; conductor.py runs fresh designer episodes; reviewers and a Sonnet tester return JSON; no manual /clear + resume.

## Established facts
- Spec: docs/specs/atelier-v6.md. PBIs: docs/pbi/PBI-001..013.md (+ facts, brief, route.json). Design record atelier-v6 revision 1.
- Claude Code subagents nest up to 3 layers by default (CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH; https://code.claude.com/docs/en/sub-agents.md). claude -p loads ~/.claude/agents; --agent picks the main thread's definition; --permission-prompts none denies prompts.
- Runners: agent only (claude CLI 2.1.322 present but off). Jev unavailable: tiers from facts; overrides in jev-log.
- Acceptance tools written by the designer: docs/pbi/tools/conductor_smoke.py, conductor_run_smoke.py, fake_designer.py. Drafts: docs/pbi/notes/drafts/.
- Lint survey: 8 leads, all outside the range (record_run.py, claude_run.py, verify.py) -> "Found, not in this run".

## Applied changes
- Repository: docs/specs/atelier-v6.md only (draft files moved out of skills/atelier into docs/pbi/notes/drafts/).

## Verification
- python -m unittest discover in skills/atelier: 640 OK before the run.

## Dead ends
- Writing the code directly (stopped: the user wants the work done through Atelier).

## Remaining plan
1. Approve (stop 1). 2. Waves: W1 001,004,011 / W2 002,005 / W3 003,006 / W4 007,008,009,010 / W5 012,013, deferred unittest per wave. 3. Test stage: test_role_results.py, test_conductor.py, agent_stats tests, test_references. 4. lint-recheck (fallback), review, finish, commit and push to claude/intelligent-ritchie-xmydhe.
