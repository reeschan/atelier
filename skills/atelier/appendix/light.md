# Appendix: the light lane

Read this when `lane.py` prints `lane: light` (`SKILL.md`, `## Entry`). You build, check, review, and record the change directly in your own session instead of starting an Atelier run.

## Lane decision

- `lane.py` checks size and risk signals (`heavy_check.py`) from the goal and candidate paths without asking the user; pass `--light` or `--full` only when the user explicitly asks to override the lane.
- An empty path list, an open `docs/pbi/conductor/run.json`, or a non-main session always keeps `lane: atelier` regardless of `--light`.

## Steps

1. **Record start.** Measure or note your current context token count before editing, then run `python <skill>/lane.py record start --builder-model <your model> --goal-file <f>`, passing the same goal file you gave `lane.py` at Entry (without it `task.profiled` is not written). It writes `docs/pbi/light.json`, emits `run.started` (`lane: light`) and `task.profiled`, and prints `reviewer:` and `review-skill:`.
2. **Build in your session.** Write or update the related tests first, then implement the change within the scoped files. Ask before any destructive operation (`## Destructive operations`), and escalate (`## Escalation`) if the work expands beyond the light lane.
3. **Run checks and tests.** Run the formatter and linter from `docs/pbi/checks.json` (or, when absent, the repository's own format, lint, and type-check commands), plus the related unit tests and E2E specs chosen by the same scope rules as `e2e_run.py` (`appendix/tests-at-end.md`, `## E2E`: specs reading changed symbols or changed in this run). Ask the user before running the full E2E suite. Pass or fail is by exit code only. If any command exits nonzero, fix the cause and run it again; do not go on to step 4 until every check and test passes.
4. **Re-measure the diff.** Run `git add -N -- <new files>` if you added files, then run `python <skill>/lane.py check --diff [--base origin/main]`. If it prints `lane: atelier`, stop and follow `## Escalation` before starting a review.
5. **Review on another model family.** Launch `atelier-reviewer` with the model on the `reviewer:` line (another family than the builder, picked by `lane.py record start`; the line is `<model>[/<effort>][@<runner>]`, so take the model before `/` and the effort after it, and when it ends in `@<runner>` launch it through `role_run.py` as `final.review` on that runner and model instead, `runners.md`, `## Roles on a runner`) to run the skill on the `review-skill:` line (`prompts/final-review.md`). Pass `Base:`, `Target:`, `Spec:`, and `Rules:` filled as in `appendix/review.md` (`## Run a review skill`). When the `atelier-reviewer` definition is missing in your session, use the fallback in `appendix/episodes.md` (`## Fallback when a definition is missing`).
   - Confirm every `critical` and `major` finding against the code. If any hold, return to step 2 to fix them, re-run steps 3 and 4, and run a second review checking only the fixed findings (at most 2 review rounds so small work does not loop on fixes).
   - Keep confirmed `minor` findings for the final report.
6. **Report and record end.** Write a short report in chat covering what changed, the checks run, the review result, any remaining minors or open items, and how to revert. Do not commit. Run `python <skill>/lane.py record end --result finished [--findings <c>,<m>,<n>] [--context-before <n> --context-after <n>]`. If your session context grew noticeably, add one line suggesting `/compact`.

## Destructive operations

Before deleting stored user data, running a force push, or changing files outside the repository, stop and ask the user.

## Escalation

When `lane.py check --diff` returns `lane: atelier` or the work grows beyond the light lane mid-build:

1. Run `python <skill>/lane.py record end --result escalated`.
2. Append your notes so far and a summary of the current diff to the goal file.
3. Start an Atelier run with `conductor.py start` (`SKILL.md`, `## Start`), where the designer will assign every file already changed to a PBI (`appendix/plan.md`, `## Split into PBIs`).
