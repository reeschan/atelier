# Appendix: finish

Read this when the finish stage picks `finish.builtin`: every PBI has passed or stopped, and the review stage is done.

## Before the report

1. Promote refs (`appendix/memory.md`, "Finish"). Close the work (the done memory) only after the report is written: see the end of `## The completion report`.
2. Collect the session's stats per role (read only; nothing changes). After a handoff, also collect the earlier session with `--session <ID>`.

   ```bash
   python <skill>/agent_stats.py --out docs/pbi/stats --run docs/pbi/conductor/run.json | head -n 40
   ```

   - The command writes `docs/pbi/stats/roles.json` and prints a table per role (caller, designer, worker, reviewer, tester): requests, tokens and cost.
   - In an inline run, when `docs/pbi/conductor/run.json` is missing, use `python <skill>/agent_stats.py --out docs/pbi/stats | head -n 40` as before; its summary opens with reads and turns per agent: requests, cache reads, share, average per request and the largest context, for the orchestrator and each subagent.
   - A cache rebuild is a request that read less than half of what was cached just before and wrote anew. Past the TTL it counts as "TTL expired"; expired under a 5m TTL but within 1 hour, it counts as "a 1h TTL would have prevented it".
   - For earlier work, use `--all` (every session, PBI subagents included) or `--session <ID>`.
   - Per-PBI models, tokens and cost are in `runs.jsonl`. The jev-log line at the end holds only the count of cache rebuilds and the number of `runs.jsonl` rows.
3. Write the cases for an outer harness (`appendix/cases.md`). It only reads `docs/pbi/` and writes `cases.jsonl`.

   ```bash
   python <skill>/export_cases.py
   ```

4. Profile the run, re-measure the PBIs, assign the finished cases to a split and embed the failures. All four read `docs/pbi/` and leave their record (events or archive); run them once, in this order.

   ```bash
   python <skill>/events.py profile --run --task-type <type> --domain <domain> --difficulty <low|mid|high>
   python <skill>/events.py backfill --dir docs/pbi
   python <skill>/events.py split assign --dir docs/pbi
   python <skill>/events.py failure-embed --dir docs/pbi
   ```

   - Run all four even when one exits non-zero: name each failed command and its stderr line in the completion report.
   - `profile --run` records the whole run once, with the same values as the PBIs' `profile --pbi` (`appendix/events.md`). In an inline run, when `docs/pbi/conductor/goal.md` is missing, write the two or three lines that restate the goal in the spec (`designer.md`, `## Start`) to `docs/pbi/conductor/goal.md` first, then run `profile --run`. `backfill` measures every PBI of this work again from the churn of its Targets since the base commit, and emits `task.profiled` again when the size or the anchors change.
   - `split assign` puts each new case in train or held-out from the seed in `<home>/splits/<name>.json`, and an existing case keeps its split; a held-out case moves to regression only through `split retire`. Home is `~/.config/atelier`, or the `ATELIER_HOME` value when it is set. When the manifest is missing, `split init` makes it (`appendix/cases.md`, "Hillclimb").
   - `failure-embed` writes one failure vector per sent-back attempt to the archive's `vectors.jsonl`. When the embedding server does not answer, it prints the URL and `0 件` and the work goes on.
   - Then run `python <skill>/ingest.py stalled --dir docs/pbi`. When it prints `stalled`, the last rounds (`--k`, default 3) had no `accept`: say so in the completion report and prompt a triage (root-cause labelling), suspecting the grader side first — a reviewer finding that disagrees with verify, many `cause=unknown` from the tester, or the same failure on every model.

5. When rtk mode is on, run these from the repository root, then turn it off. The number covers every rtk run in this repository so far (other sessions, Codex and opencode included); say so.

   ```bash
   rtk gain -p | head -n 20
   node <skill>/rtk/rtk-mode.mjs off
   ```

6. When `docs/pbi/adapters.json` has `map.decision` `repo`, run `python <skill>/omm_cache.py export docs/pbi --base <base>`. It copies the stored `.omm/` into the working tree; exit 2 (no stored scan, or an untracked `.omm/` already there) leaves the working tree as it is. Say in the report where `.omm/` is or why it is not, that the user commits it, and that it shows the merge-base, so `omm-pr-scan` can bring it up to the change.

## The completion report

Write it to `docs/pbi/REPORT.md` first, in the user's language. The gates after finish run on that file. The bundled templates have none; a template that adds yomiyasu as an external component (`gate.yomiyasu`, `appendix/engine.md`) polishes a Japanese report's wording when the yomiyasu skill is installed: call it on the file, apply its wording fixes, and keep numbers, ids, paths, commands and pasted tables as they are. Then write its path (`docs/pbi/REPORT.md`) in the `report` of the episode result and end the finish episode with status `done`. The report is a fixed-path artifact, not a summary file: when a rule of your environment stops you from writing it, put its whole text in `report_body` of the result and the conductor writes the file; `done` refuses a finish with neither. The conductor deletes an earlier `docs/pbi/REPORT.md` when it starts the finish episode, so an old report never stands in for this one, and refuses to start it when that file cannot be removed. With `report_body`, do not write the done memory: the report does not exist yet. The conductor writes the file at `done` and prints `then: write the done memory ...`, and the caller writes it after that; inline, send the file's text to the user. Once `docs/pbi/REPORT.md` exists, close the work: write the done memory (`appendix/memory.md`, "Finish"). A run that stops before the report keeps its handoff, so `resume` still finds it.

Open with one line that restates the user's goal in your own words, as at the start, then three headings, in this order:

- **Waiting on you**: PBIs at the limit, held, or left with quality warnings, and the decision each one needs.
- **Changes**: the PBIs that passed and the files they changed.
- **Findings**:
  - problems found on the way
  - the stage records from STATUS.md, as they are (`appendix/engine.md`)
  - the two tables of `python <skill>/model_report.py`, pasted as they are: runs, outcome breakdown, unavailable rate, tokens, cost and API equivalent, time, and the provisional grades. The grades are a guide for a person to adjust later; add no reading of them here. Whether a rotate group split evenly shows in the run counts
  - runs whose archive copy failed (`archive.ok` false in `runs.jsonl`)
  - every unusable runner: the PBI, the cause, and what the user picked instead
  - PBIs where you overrode the decision table, and how they ended
  - where Jev was skipped, and the number of handoffs
  - the stats per role: the table of `docs/pbi/stats/roles.json` (caller, designer, worker, reviewer, tester)
  - rtk savings (tokens and rate, top commands), or "rtk: off (context below the threshold)"
  - the lint recheck: the path of `docs/pbi/jevlint/recheck.md`, its `report` rows, the reworks it caused, and each send to an outside model: the stage, the file count and the user's answer from `adapters.json` `sends`, the files from `docs/pbi/jevlint/<name>.send.txt`, and the tokens, cost and provider from `docs/pbi/jevlint/<name>.dry-run.json`
  - the lint survey's leads under "Found, not in this run", and the map's source (cache folder, scan, or fallback prompt)
  - the recommended tools that were missing and the fallback each stage used
  - kiln: the number of compact-event lines per action (`docs/pbi/.kiln/compact-events.jsonl`), and the continues after a kiln stop
  - working memory: the work key, the number of refs written, the promoted memory ids and titles, and every `runs.jsonl` row whose `memory.violation` is true (runner, model, what was used), and the rows with `missed_refs`
  - the causes of rework, tier up and hold, counted from `runs.jsonl`, and the line `export_cases.py` printed
