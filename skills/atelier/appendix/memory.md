# Appendix: working memory (shared-memory)

Read this when you choose the work key, write refs, hand off, or finish. It applies only when the shared-memory MCP (`mcp__shared-memory__*`) is connected. Without it, skip it all: Refs stay empty and nothing is promoted. When `write` is not allowed (in a check run, for example), skip refs and promotion and put the facts in the PBI's Notes. Either way, say so in the completion report.

You search widely to design; a worker reads only its PBI and the refs it lists. In the author's runs, 15 of 32 memory calls were worker searches that found nothing beyond the PBI.

| Term | Meaning |
|---|---|
| work | one request from the user; it stays the same work across handoffs. Numbered per repository: `W-001` |
| work key | `<repo>:<branch>:<work number>` |
| workspace | the scope `project:work.<repo>.<branch>.<work number>`. In the branch, `/` and every character other than letters, digits and `. _ -` become `-` |
| ref | one fact you write in the workspace |

## Start

1. Choose the work key. repo is the repository's directory name and branch the current branch. For the number, look for existing `W-` numbers in `repo:<repo>` and the workspace names; with none, `W-001`. When resuming, keep the work key in STATUS.md.
2. Put the work key and the workspace scope at the top of STATUS.md (`appendix/engine.md`).

## What CLAUDE.md already holds

- Treat the stable knowledge of the repository (layout, conventions, commands, pitfalls) as being in its layered CLAUDE.md files. The root one loads at start; a folder's one loads when a file in that folder is read, for workers too.
- Never copy a CLAUDE.md fact into a ref or a PBI's Notes.
- When the repository has no CLAUDE.md, you may search more widely.

## Search

- Search only for what CLAUDE.md does not hold: reasons behind past decisions, pitfalls found in other repositories. At most once in `repo:<repo>` and once in `global`, always with a scope; an unscoped search mixes in other work and other branches.
- Search the workspace scope only when resuming.
- The one unscoped search is the resume lookup with tags `handoff` (`appendix/handoff.md`).

## Write refs

- A ref is a fact used by two or more PBIs, a handoff, or a review warning. A fact for one PBI goes in that PBI's Notes.
- Write them in one parallel turn: once before the PBIs are written, and once per wave for warnings found in review. Do not `get` right after `write`; put the returned id straight into Refs.
- One fact per ref. Scope: the workspace. Tags: the kind (`ref:decision` a design decision, `ref:convention` an excerpt of a convention or pitfall, `ref:summary` a summary of an earlier PBI's result, `ref:review` a warning from review) and `work:<branch>/<work number>`. kind `semantic`; provenance `agent_verified` for a checked fact, `agent_inferred` for an inference.
- For an excerpt, name its source (the original memory id or file) in the body.
- Decisions made on the way and review warnings that matter to later PBIs are added per wave and listed in those PBIs' Refs.
- Put ref ids in Refs (`appendix/plan.md`) and never copy their bodies into the PBI. External runners are the exception: `runners.md` copies the bodies into the prompt.

## Workers and reviewers

- Agent tool workers and reviewers run as `subagent_type: "pbi-worker"`. That definition has only `get` among the memory tools (`agents/pbi-worker.md`, placed in `~/.claude/agents/pbi-worker.md`).
- When `pbi-worker` is missing, start `general-purpose` instead and write "no pbi-worker (reason)" at the top of STATUS.md. The rules of `worker.md` are then all that holds it back.

## Hand off

- Besides HANDOFF.md, write a handoff memory in the workspace: kind `episodic`, tags `handoff` and `work:<branch>/<work number>`. From the second handoff on, set the previous one in `supersedes`, so only the newest is live.
- Its title names the repository and the work number. Its body starts with the work key, the absolute repository path (the worktree's work root for a worktree) and the branch. A `resume` in a session started without a folder finds the repository through this path.

## Finish

- Promote at most 3 refs that meet all three: useful for the next work, not visible from the code, and checked.
- Promote to `repo:<repo>` when the fact is specific to the repository, to `global` otherwise. Write it as a new memory with the ref in `supersedes`, the tag `work:<branch>/<work number>`, and provenance `agent_verified`.
- Do not stop for approval. List the promoted ids and titles in the completion report.
- Leave the workspace open. Other work cannot see it, it drops out of search after 30 days without access, and `get` by id still reaches it.
