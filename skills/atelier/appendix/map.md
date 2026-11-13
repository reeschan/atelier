# Appendix: map (layers and structure)

Read this when the optional stage `map` runs (`map.builtin`, placed after understand). It runs only when the design decision is run (`appendix/design.md`, `## Design: the decision`), which v4 takes right after understand.

The map tells the design and the lint survey which layers and modules the request touches. It is read once per run. You never read the diagrams yourself; a subagent reads them and writes a short note.

## Settings

- `docs/pbi/map.json` (optional, example `examples/map.json`): `scan_dirs` (the directories the fingerprint covers; default `src` when it exists at the merge-base, else the whole tree) and `perspectives` (default `overall-architecture`, `dependency-map`).
- The cache folder: `omm.cache_dir` in home `agents.json`, else `.omm-cache` beside the main clone. Under it: `<main clone name>/<first 12 of the fingerprint>/` with `.omm/` and `<folder>/fingerprint.json`. `.omm/` never goes into the repository.

## Run

1. Look up the cache:

   ```bash
   python <skill>/omm_cache.py lookup docs/pbi --base <base>
   ```

   It prints `hit <folder>` or `miss <folder>`.
2. **Hit**: start one subagent (`subagent`, memory none, with `prompts/kiln-chamber.md` in its prompt). It reads `<folder>/.omm/` (each perspective's `<perspective>/description.md` and `diagram.mmd`, then the elements the request names) and writes `docs/pbi/notes/map.md`: the layers, the modules the request touches, the direction of the dependencies between them (about 5 lines), and the cache folder. It returns at most 5 lines and the path.
3. **Miss**, when `tools_check.py` shows `omm` and `omm-scan` as `ok` and the user did not choose to go without them at Approve: ask first (`## Asking before a scan`). With `repo` or `cache`, start one subagent with this brief:
   - invoke the `oh-my-mermaid:omm-scan` skill with the perspectives from `map.json`, on the repository at the merge-base state the work starts from;
   - never run `npm install`, `omm view`, a commit, or anything that leaves `.omm/` in the working tree;
   - when the scan ends, run `python <skill>/omm_cache.py store docs/pbi --base <base> --perspectives <list> --note "<the request in a few words>"`;
   - write `docs/pbi/notes/map.md` as in step 2 from the stored folder, and return at most 5 lines and the path.
4. **Miss without the tools**, or the answer `skip`: start one subagent with `prompts/map-fallback.md`, filled in. It writes the same note from the code and its dependencies. Nothing is cached.
5. After the subagent returns, run `python <skill>/omm_cache.py check docs/pbi`. `left <path>` means `.omm/` is still in the working tree: run `store` yourself; never go on to the lint survey with `.omm/` left.
6. Write the stage record: `stage map after understand: map.builtin run (cache hit <folder>)`, `(scanned, stored <folder>, repo|cache)`, `(fallback prompt: <tool> missing)` or `(fallback prompt: the user chose not to create it)`.

## Asking before a scan

A scan takes minutes and tokens, and a diagram in the repository is a file the user keeps, so the user decides both.

1. Read `docs/pbi/adapters.json`. A `map` entry with `"asked": true` holds the answer: use it and do not ask again (resume).
2. Otherwise end the episode with a question (kind `decision`, with `state`; inline: AskUserQuestion once) with three choices, laid out as the `decision (design choice, ask-user, map, optional)` row of `appendix/questions.md` (`## What goes in each kind`):
   - `repo`: scan, and leave `.omm/` in the working tree at the end of the run, for the user to commit with the change;
   - `cache`: scan, and keep it only in the cache outside the repository;
   - `skip`: do not create it; the fallback prompt writes the note from the code and its dependencies.
3. Save the answer as `{"map": {"decision": "repo|cache|skip", "asked": true}}` in `docs/pbi/adapters.json`, beside the other entries.

With `repo` the scan still goes to the cache during the run, so `.omm/` never enters the lint survey, the recheck or the review diff. Finish copies it into the working tree (`appendix/finish.md`). Atelier never commits it.

## How later stages use it

- **lint-survey**: the note's modules choose the survey's range (`appendix/lint.md`).
- **design**: the spec's Implementation Decisions name the layers each change belongs to, and say when a change crosses the dependency direction the map shows.
- **plan**: a PBI whose targets cross two layers says so in its Notes.
