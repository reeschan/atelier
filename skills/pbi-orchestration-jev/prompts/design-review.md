# Prompt: design review of one PBI

Give this to an Opus subagent before an opus-tier PBI goes to build, when you are not running on Opus. Fill in the brackets.

```text
description: PBI-<n>.design <short text>
subagent_type: pbi-worker
model: opus
prompt:
Work root: <absolute path of the repository or worktree>. Work here and write nothing outside it.
Read docs/pbi/PBI-<n>.md and the refs in its Refs section (get them by id).
Write only design problems, gaps and ambiguities to docs/pbi/reviews/PBI-<n>.design.md, one per line.
Do not edit any other file. Do not call review skills or subagents.
Your final message: the count and one line per problem, nothing else.
```

Fix the PBI from the result before you launch its worker.
