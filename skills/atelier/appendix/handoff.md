# Appendix: rtk mode, handoff and resume

Read this at every wave boundary, and when the argument is `resume` (or `resume <repository path>`).

## Measure at a wave boundary

When every PBI of the wave is reviewed and STATUS.md is up to date, measure the context before starting the next wave:

```bash
python <skill>/agent_stats.py --ctx
```

- `context` 150,000 or more and rtk mode is off: turn it on (`## rtk mode`).
- `context` 250,000 or more and PBIs remain: hand off (`## Hand off`). A new session starts at about 70,000 to 90,000; everything above that is re-read on every later turn.

## rtk mode

Only when [rtk](https://github.com/rtk-ai/rtk) and the bundled hook `rtk/rtk-gate.mjs` are installed (see the README). It starts off: writing long output to files already keeps the savings small (300 to 2,000 tokens per run in the author's measurements).

```bash
node <skill>/rtk/rtk-mode.mjs on
```

- While it is on, this session's Bash and PowerShell commands, subagents included, are rewritten to `rtk <command>` and their output is compressed. Read, Grep and Glob are not affected.
- When the output says rtk is not on the PATH, nothing is compressed. Keep going, and mention it in the completion report.
- Treat compressed output as the full result. Run `rtk proxy <command>` for the uncompressed output only when the output is empty but should not be, disagrees with the exit code, or is broken.
- Codex and opencode are always compressed when rtk's global setup is installed (`rtk init -g --codex`, `rtk init -g --opencode`). Antigravity (`agy`) is never compressed.
- Once on, it stays on until the completion report (`appendix/finish.md`), including while you are stopped.

## Hand off

1. Write `docs/pbi/HANDOFF.md` in at most 40 lines. The first two lines are always `Repository: <absolute path of the repository, or the worktree's work root>` and `Branch: <branch>`. Then: the next wave (PBIs and route heads), skipped runners and why, decisions you overrode, design decisions not yet in a PBI (add them to a PBI or the spec first), warnings for later PBIs found in review, the stage records from STATUS.md, and whether `docs/pbi/adapters.json` exists.
2. Write the handoff memory in the workspace (`appendix/memory.md`, "Hand off"): the gist of HANDOFF.md, the work key, the workspace scope, the absolute repository path and the branch.
3. Append `{"event": "handoff", "context": <value>, "done": <done>, "remaining": <remaining>}` to jev-log.
4. Tell the user only these two lines, in their language, and stop (rtk mode stays on; put the real absolute path in place of `<path>`):
   - "The context reached <value> tokens, so I am handing off."
   - "After `/clear`, `/atelier resume` continues from here. In a session started without a folder, type `/atelier resume <path>`."

## Resume

1. **Find the repository.** When the working directory has `docs/pbi/STATUS.md`, that is it. Otherwise (a session started without a folder runs in an empty folder the app made), try in order:
   1. the path in the argument (`resume <repository path>`)
   2. one search of shared memory with tags `handoff` and kind `episodic`, without a scope (the one exception to the scope rule, since the workspace is not known yet). Group the hits by work key and take the newest (`created_at`) of each.
      - One candidate: `get` it and take the absolute path and the branch.
      - Two or more (other work may be mid-handoff too): list the work keys, titles and `created_at`, and let the user pick. Never pick the newest yourself.
   3. When that still finds nothing (no MCP, no hits, no path in the body): ask the user for the path.

   Confirm that the path has `docs/pbi/STATUS.md` and that its current branch (`git -C <path> branch --show-current`) matches STATUS.md and the handoff memory. When it does not, stop and ask. Then, when the app's change_directory (`mcp__ccd_directory__change_directory`, loaded with ToolSearch) is available, move the session there. The move takes effect at the end of the turn, so until then, and for good when it is not available, work with absolute paths (Read and Write with absolute paths, Bash and PowerShell with `git -C <path>` or explicit paths). Say in one line of your first reply which path you chose and how (working directory, argument, memory, or the user).
2. Read `docs/pbi/STATUS.md` and `docs/pbi/HANDOFF.md`, and other files only in the parts you need. Do not redo the stages before build. Continue with the next wave.
