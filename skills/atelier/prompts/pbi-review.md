# Prompt: review of one PBI's diff

Give this to an Opus subagent when a PBI's diff is over 300 lines, or the PBI is `deep` and you are not running on Opus. Fill in the brackets.

```text
description: PBI-<n>.review <short text>
subagent_type: pbi-worker
model: opus
prompt:
Work root: <absolute path of the repository or worktree>. Work here and write nothing outside it.
Read docs/pbi/PBI-<n>.md and the refs in its Refs section (get them by id), then read `git diff -- <targets>`.
Write only the problems that should block the merge to docs/pbi/reviews/PBI-<n>.md, one per line:
- [critical|major|minor] <file>:<line> — <why> — <repro>
Do not edit any other file. Do not call review skills or subagents.
Your final message: the counts and one line per problem, nothing else.
```

Use the result as input to your own decision, never as the decision.
