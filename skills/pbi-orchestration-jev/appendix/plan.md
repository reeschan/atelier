# Appendix: plan and route

Read this when the plan stage picks `plan.builtin`, or the route stage or its gate picks `gate.jev`, `route.jev` or `route.opus`.

## Split into PBIs

Keep each PBI small: about 3 changed files at most. Write one file per PBI, `docs/pbi/PBI-<n>.md`. The worker has none of your design context, so the PBI and its refs must be enough to do the work.

```markdown
# PBI-<n>: <title>

- Model: <from the prepare.py table>
- Depends on: <PBIs that must be done first, or none>

## Goal
<why this is needed and where it fits, in 2 or 3 sentences>

## Targets
<files and functions to change; for a new file, its path and role>

## Do not touch
<files, APIs, schemas and existing tests that must not change>

## Acceptance criteria
1. <verify command number and the expected output>

## Verify commands
1. `<one line, run as is from the repository root>`
2. `<...>`

## Refs
<workspace ref ids with a one-line gist each, or none>

## Notes
<patterns to follow, files to read, pitfalls, facts only this PBI needs, notes from docs/pbi/notes/>
```

- One verify command per line, as `<n>. \`<command>\``. `verify.py` reads exactly this form; put explanations and expected values in the acceptance criteria.
- List only the refs this PBI needs. A worker never looks for refs that are not listed.

## Writing verify commands

A broken verify command is your mistake, and it looks like a failed PBI. Avoid these:

- **Files not to touch**: check them with `unchanged.py`, not `git status`. `git status --porcelain -- <files>` also reports new files of earlier waves and changes of parallel PBIs. Hash the files right before the launch (`unchanged.py save`), not when writing the PBI, and use `python docs/pbi/tools/unchanged.py check PBI-<n>` as the command. It prints `changed <path>` and exits 1 for each changed file. Leave out files that a parallel PBI touches.
- **Comparing with HEAD**: earlier PBIs are not committed yet, so `git diff --exit-code -- <file>` always fails. Use `unchanged.py`.
- **Short output**: the result should show in the exit code or a few lines (`vitest run <file>`, `tsc --noEmit -p .`, `grep -c`). Never expect a whole test or build log.
- **`grep -c` counts lines**, imports included: write "1 or more", not "1". With no match it exits 1; say so in Notes and judge by the printed value.
- **Paths with `$` or `[`**: put them in single quotes (`'src/routes/api/recipes/$id.ts'`).
- **Git Bash path conversion** (Windows): an argument starting with `/` becomes `C:/Program Files/Git/...`. Prefix commands that pass URL paths with `MSYS_NO_PATHCONV=1`.
- **The Bash tool on Windows** fails with `eval: unexpected EOF` when one command has both a `<` redirect inside `$( )` and `$'...'`. Split them into two commands; count CRs with `grep -c $'\r$' file` on its own.
- **`/tmp` on Windows**: Windows node and python read Git Bash's `/tmp` as `C:\tmp`. Keep temporary files in the repository or under a Windows path.
- **Format checks see work files**: `prettier --check .` reads untracked files, so add `docs/pbi/` to `.prettierignore` first.
- **Dev servers on Windows** do not stop with `&` and `kill`. Write one script that starts, requests and stops the whole process tree (for example `docs/pbi/tools/smoke.mjs`), use one port per PBI, and point every connection variable at a dummy so production data is never touched. Never copy `.env*`.
- **`node --test`**: add `--test-reporter=tap`; the default reporter of Node 26 prints no `# pass` / `# fail` lines.
- **rtk and `grep -h`**: in `rtk grep`, `-h` means help. The bundled hook leaves grep and rg commands with `-h` alone, but do not write `rtk grep -h` yourself.

## Facts and brief

For each PBI, write these two files, all PBIs in one parallel turn.

`docs/pbi/PBI-<n>.facts.json`:

```json
{"files_changed": 3, "files_new": 1, "files_deleted": 0, "modules": 1, "criteria": 5, "sensitive": false}
```

- `files_changed`: changed, new and deleted files together, tests included. `modules`: how many feature units the changed files belong to.
- `sensitive`: the PBI touches authentication, permissions, a database schema, deletion of user data, or secrets. Deleting files inside the repository does not count.

`docs/pbi/PBI-<n>.brief.md`, at most 8 lines. Jev gets the brief, never the whole PBI of a sensitive one.

```
Goal: <one sentence>
Kind of change: <replace an existing pattern / add to an existing pattern / a new mechanism>
New abstraction: <none / a type or function / a new contract between modules>
External interaction: <none / n HTTP calls / storage / ...>
Invariants to keep: <none / specifics>
Judgment needed: <none / specifics>
```

## Route with Jev (`gate.jev`, `route.jev`)

Run once, after all facts and briefs exist:

```bash
python <skill>/prepare.py docs/pbi [--skip opencode,codex,agy,dsh] [--only PBI-<n> --tier <tier>]
```

`prepare.py` runs Jev gate 1 (brief and PBI quality) and then `select_model.py`, so one run covers both `gate.jev` and `route.jev`. When the template has `route.jev` without `gate.jev`, run `python <skill>/select_model.py --all docs/pbi` instead.

- Copy each row (tier, route head, review, scores, reason, priority template, dropped steps, quality warnings) to the PBI's Model line and to STATUS.md.
- After rewriting a PBI, judge it again with `--only PBI-<n>`. To raise a tier, run `--only PBI-<n> --tier <tier>`, which rewrites its `route.json`.
- The decision table is in the docstring of `select_model.py`; the reason column quotes it. Without Jev, the tier comes from the facts alone.
- An active priority template applies after the decision table and before the routing template. It never applies to a sensitive PBI, and the last step is always the routing template's agent for that tier. Copy the first line and the priority-template column to the approval list.
- Quality warnings never decide pass or fail. Show them in the approval list; rewriting once after a warning is optional.
- When you override the decision table, write the reason in the STATUS.md note and in jev-log.

## Route everything to opus (`route.opus`)

Run `python <skill>/select_model.py --all docs/pbi --only <all PBI ids, comma-separated> --tier opus`. It writes each `route.json` with the opus tier of the routing template, and Jev is not called. Copy the rows as above.
