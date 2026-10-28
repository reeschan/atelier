# Appendix: finish

Read this when the finish stage picks `finish.builtin`: every PBI has passed or stopped, and the review stage is done.

## Before the report

1. Promote refs (`appendix/memory.md`, "Finish").
2. Collect the session's stats (read only; nothing changes). After a handoff, also collect the earlier session with `--session <ID>`.

   ```bash
   python <skill>/agent_stats.py --out docs/pbi/stats | head -n 40
   ```

   - The summary opens with reads and turns per agent: requests, cache reads, share, average per request and the largest context, for the orchestrator and each subagent.
   - A cache rebuild is a request that read less than half of what was cached just before and wrote anew. Past the TTL it counts as "TTL expired"; expired under a 5m TTL but within 1 hour, it counts as "a 1h TTL would have prevented it".
   - For earlier work, use `--all` (every session, PBI subagents included) or `--session <ID>`.
   - Per-PBI models, tokens and cost are in `runs.jsonl`. The jev-log line at the end holds only the count of cache rebuilds and the number of `runs.jsonl` rows.
3. When rtk mode is on, run these from the repository root, then turn it off. The number covers every rtk run in this repository so far (other sessions, Codex and opencode included); say so.

   ```bash
   rtk gain -p | head -n 20
   node <skill>/rtk/rtk-mode.mjs off
   ```

## The completion report

Write it to `docs/pbi/REPORT.md` first, in the user's language. The gates after finish run on that file. The bundled templates have none; a template that adds yomiyasu as an external component (`gate.yomiyasu`, `appendix/engine.md`) polishes a Japanese report's wording when the yomiyasu skill is installed: call it on the file, apply its wording fixes, and keep numbers, ids, paths, commands and pasted tables as they are. Then send the file's text to the user.

Open with one line that restates the user's goal in your own words, as at the start, then three headings, in this order:

- **Waiting on you**: PBIs at the limit, held, or left with quality warnings, and the decision each one needs.
- **Changes**: the PBIs that passed and the files they changed.
- **Findings**:
  - problems found on the way
  - the stage records from STATUS.md, as they are (`appendix/engine.md`)
  - the two tables of `python <skill>/model_report.py`, pasted as they are: runs, outcome breakdown, unavailable rate, tokens, cost and API equivalent, time, and the provisional grades. The grades are a guide for a person to adjust later; add no reading of them here. Whether a rotate group split evenly shows in the run counts
  - runs whose archive copy failed (`archive.ok` false in `runs.jsonl`)
  - how many times a route moved to its next step, and why
  - PBIs where you overrode the decision table, and how they ended
  - where Jev was skipped, and the number of handoffs
  - the stats summary: the orchestrator's share and the top 3 subagents by reads
  - rtk savings (tokens and rate, top commands), or "rtk: off (context below the threshold)"
  - working memory: the work key, the number of refs written, the promoted memory ids and titles, and every `runs.jsonl` row whose `memory.violation` is true (runner, model, what was used)
