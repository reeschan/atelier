# Appendix: calling Jev and its rules

Read this when you call Jev outside `prepare.py`: the gate `report` when a report's status and its verify disagree, and the gate `rework_quality` for a rework note. `prepare.py` calls gate 1 (brief and pbi_quality) itself.

## Calling Jev

Use the bundled client `<skill>/jev/jev.py` (usage in the repository's README). The client reads the API key from the `TYPESAFE_API_KEY` environment variable, then from `~/.config/typesafe/credentials.json`. Never read or show the key or that file, and never write a key into a script or a setting.

```bash
python <skill>/jev/jev.py docs/pbi/jev.config.json <gate> <file with the text to send> > docs/pbi/jev/<PBI>.<gate>.json
```

- The settings are `docs/pbi/jev.config.json`. When it is missing, copy `<skill>/jev.config.template.json` there, then use that file.
- The API: noul `criteria` is `{"1": "...", "0": "..."}`, score is a list of level descriptions, choice is `{"<option>": "<description>"}`. Answers come back under `answers.<key>` (noul: `noul`; score: `score`, `confidence`, `probabilities`; choice: `choice`, `confidence`). Never print the whole response; take only the keys you need.
- On failure, the client retries (429 and 529 up to 3 times; 5xx and connection errors once). When it still fails, skip that gate, decide yourself, and mention it in the completion report. Jev being down never stops the work.

## Rules

- **Log only changes.** `prepare.py` writes decision changes to jev-log. You write to it only when you override the decision table, when you hand off, and when you ask Jev the gate `report`. PBI outcomes, reworks, tokens and cost go to `docs/pbi/runs.jsonl` through `record_run.py`, never as free text in jev-log.
- **The thresholds and the decision table are provisional.** Once 10 to 20 PBIs have run, propose a review of the decision table and the warning thresholds to the user, with the per-model results of `model_report.py --archive`. The user decides. Review at the same time the rework rate of opencode on the sonnet tier, the handoff threshold (250,000), and the cache TTL (compare "a 1h TTL would have prevented it" in `agent_stats.py --all` with the 1h writes). A change goes into the constants of `select_model.py` and `prepare.py`, with its reason recorded.
- **Send only prose**: PBI text, summaries, worker reports, rework notes. Never source code, diffs, environment variables, credentials or customer data.
- **Do not make it count.** Counts, numbers of times and date comparisons come from the facts and STATUS.md, never from Jev.
