# Appendix: tests at the end

Read this at Start of every run. The option is on by default: the bundled templates set `build.tests_at_end`. It is off when the run copy `docs/pbi/tests-at-end.json` says `"enabled": false`, when the user asks for workers to run their own tests, or when no test command is found (below).

With the option on, workers change only the code during the waves. They write no tests, change no tests and run no slow checks. After each wave and each rework the tester runs the fast deferred checks and only the E2E specs related to the PBIs (`related_specs`); after the last wave one test stage reviews, fixes, adds and deletes the tests of the whole change, and runs the related specs of the whole run once. The full E2E suite never runs on its own: the run asks the user (`full-run`) and runs it once only on a yes. You reconcile each test-result and decide.

Use it when several workers share one working tree and the test run is slow or flaky under load: their test runs fail on other PBIs' half-done changes, and you run the tests again at the end of the wave anyway.

## Start

1. When `docs/pbi/tests-at-end.json` is missing, write it from the template's `build.tests_at_end` (shape: `<skill>/examples/tests-at-end.json`). The run copy wins; `verify.py` reads only the run copy. To turn the option off, write `{"enabled": false}`.
2. The bundled template leaves `deferred` and `test_paths` empty. Draft them from the repository (its package scripts, test runner config, and where its test files live) and show the draft in the PBI list approval (`designer.md`, `## Approve`); do not stop to ask before that. The fields:
   - `deferred`: substrings of verify commands that workers never run. Every command that contains one of them runs by the tester after each wave (kind `wave`), or only in the test stage with `when: end`. Put here the test runs and the checks that need tests (for example a check that every domain file is read by a test).
   - `test_paths`: globs of test files. Workers never create, change or delete them.
   - `principle`: the skill name of the test principle the test stage follows. The default is pstack's `principle-test-behavior-not-implementation`. Find its `SKILL.md` with Glob under `~/.claude/plugins/`; it cannot be invoked as a skill (`disable-model-invocation`), so pass its path. When it is missing, use `## Test principle (fallback)` below.
   - `skill` (optional): the name of a test skill the tester calls instead of `verify.py --deferred`. Without it, the tester runs `verify.py --deferred`.
   - An entry of `deferred` is a substring, or an object `{"match", "when": "wave"|"end", "timeout", "server": {"start", "url"}}`. `when: end` runs only in the test stage. A command that runs the whole E2E suite goes to `final` (below), never to `deferred`.
   Then add the project's test rules (`## Project test rules`).
3. When no test command is found, `deferred` stays empty and the option does nothing: write `Tests at end: off (no test command)` in STATUS.md, and workers run their verify commands as usual.
4. Otherwise write the option in STATUS.md: `Tests at end: on (deferred: ..., test_paths: ...)`.

## Project test rules

At Start, before you draft the keys above, read the project's own rules and the user's instructions about tests:

- the repository's `AGENTS.md`, `AGENTS.project.md`, `CLAUDE.md` and the folders' `CLAUDE.md`, and the README of each test folder (for example the README in an e2e folder);
- the user's memory: shared-memory (`search` with words like test, E2E, Cypress, spec; `get` only what applies) and Claude Code's auto-memory index (`~/.claude/projects/<the project's folder name>/memory/MEMORY.md`, and the files it points to that are about tests).

Write what you took into `docs/pbi/tests-at-end.json`:

- `env`: the Node version the project asks for (`.node-version`, `.nvmrc`, `engines`) and the shell (`bash: git-bash` on Windows). `verify.py` picks them itself; write them so the user sees them.
- `server`: `{"start", "url"}` when the E2E tests need a running server (for example `pnpm run start:e2e` on `http://localhost:10096`). `verify.py` starts it when the url does not answer, waits, and stops only what it started.
- `final`: the command that runs the whole suite the way the project says (for example `cd e2e && pnpm run cy:run:parallel`).
- `investigate`: the command that runs some specs, with `{specs}` for the comma-joined list (for example `cd e2e && pnpm run cy:run:local --spec {specs}`).
- `changed` (optional): the project's command that runs the specs a change touched, when the rules name one.
- `limits`: `{"per_spec": 900, "retries": 2, "idle": 600}` unless the project says otherwise: one spec runs at most 15 minutes and is run again at most twice; the related specs of the whole run get 15 minutes per spec; the full suite has no time limit but stops after 10 minutes with no output.
- `notes`: other rules for the tester, one line each.
- `sources`: where each rule came from (file or memory id).

The project's rules win over Atelier's defaults, and the user's memory over a default too. Show every rule in the approval draft (`appendix/approve.md`, `## Who builds`), one line per point where a project rule replaced a default. When nothing is found, draft from the package scripts as above and say so. Only what the user approved reaches the tester.

## Plan

- Keep test files out of the build PBIs. Their Targets name no file in `test_paths`, and their acceptance criteria ask for no new test. Write the behaviour a test should pin into the PBI's Notes under `Tests for the end:` (one line each, with the requirement it comes from). The test stage reads these lines.
- A worker-run verify command must pass without touching a test file. When a command also checks the test files (a type check that includes the test project, a lint that runs the tests), split it: the part on the source stays with the worker, the part on the tests goes to `deferred`. Example: the type check of the source project for the worker, the type check of the test project deferred.
- When a code change breaks existing tests by design (a renamed export, a changed signature), say so in the PBI's Notes. The test stage fixes them; the worker does not.
- Give every build PBI its `related_specs`: the E2E specs that exercise what it changes (from the spec files' names and what they import or visit), or none. They go in the approval draft.
- A build PBI's verify commands never run the whole E2E suite (the `final` command, or the E2E script with no spec filter). `verify.py` refuses such a command with exit 2.

## Build

Add to every worker prompt, after `Files that parallel PBIs touch` (`runners.md`):

```
Deferred verify commands: <numbers from `verify.py PBI-<n> --list-deferred`, or none>
Test files you must not touch: <the test_paths globs>
```

## Verify, per PBI

- Run `python <skill>/verify.py PBI-<n> --name <report> --skip-deferred`. The deferred numbers keep no result yet.
- Read the diff as usual. A change to a file in `test_paths` is a change in the "Do not touch" list: rework.
- Do not record the PBI yet; it is judged at the end of the wave.

## End of a wave

1. When every PBI of the wave has been checked, start the tester (`prompts/tester.md`, kind `wave`). It runs the deferred commands of `when: wave` once for all of them, then the related specs of the wave's PBIs:

   ```bash
   python <skill>/verify.py <report>,<report>,... --deferred --when wave
   python <skill>/verify.py --related <spec>,<spec>,... --name wave-<n>
   ```

   Each distinct command runs once, and its result is merged into each report's `<report>.json`. After a rework, the tester runs the same, for the reworked PBIs and their related specs only. When `docs/pbi/tests-at-end.json` has a `skill`, the tester calls that skill instead. It writes each failure as a test name, file, PBI and a `cause` guess (`intended`, `regression` or `unknown`).
2. Reconcile the tester's test-result (`appendix/episodes.md`, `## Reconcile`). The `cause` is a proposal; you decide each one. Compare the failing tests with the previous wave's list (`docs/pbi/verify/tests-at-end.failing.md`; empty before wave 1). For each failure that is new:
   - `intended` (a change a PBI made on purpose; its Notes say so, or the diff renames or removes what the test reads): add it to the list with the PBI. The test stage fixes it. It does not block the PBI.
   - `regression` (a behaviour the PBI should have kept): rework that PBI, as for a failing acceptance criterion (`appendix/rework.md`).
   - `unknown`: find the files the failing test imports, and the PBI of the wave whose diff touches them. Run that test file alone and decide from its output. Never stash or reset to find out.
3. Record each PBI with `record_run.py`. A deferred number that failed only for a reason in the list counts as pass; write `deferred: <numbers>, failing tests listed` in its STATUS.md note.
4. Write the updated list to `docs/pbi/verify/tests-at-end.failing.md`, one line per test: file, test name, the PBI, why it fails.

## Test stage

Run it once, after the last wave and before the review stage.

1. **Range.** List the source files changed since the base (`git diff --name-only <base>...` plus untracked files). The range is the tests in `test_paths` that read those files, the failing list, and the `Tests for the end:` lines of every PBI.
2. **Survey.** Start the tester (`prompts/tester.md`, kind `survey`) with this brief: the principle's `SKILL.md` path, the range, the failing list and the `Tests for the end:` lines. It reads each test in the range and writes `docs/pbi/notes/test-survey.md`, one row per test or missing test:

   | file:test | verdict | reason | action |
   |---|---|---|---|

   - `keep`: catches a broken behaviour and survives a change of internals.
   - `fix`: fails now, and the behaviour it asserts still holds. The action says what to change.
   - `rewrite`: asserts an implementation detail, or proves nothing. The action says what behaviour to assert instead.
   - `delete`: pins implementation only, or repeats another test, and no behaviour is lost. The reason names the test that still covers the behaviour, or says none is needed.
   - `add`: a `Tests for the end:` line, or a behaviour the change introduced, that no test covers. The action names the observable result to assert.
   The subagent edits nothing. Read the counts per verdict from the table it wrote, not from the JSON.
3. **Check the survey.** Read the rows for `delete` and `add` in full. Drop an `add` that asks for a test of an internal call, and a `delete` that would leave a behaviour of the change untested.
4. **Test PBIs.** Turn the rows into PBIs (`appendix/plan.md`), grouped by test file, never two PBIs on the same file. Their Targets are test files only; source files are "Do not touch". Their verify commands run their own test files (one file per command, so the runs stay short), and the type check of the test project. Workers may run these commands. Route and build them as usual, at most 3 at once, because the test runs share the machine.
5. **Close.** When every test PBI passes, start the tester (`prompts/tester.md`, kind `final`). It runs the verify commands of every report again with `--skip-deferred`, the deferred commands of `when: end` once, and the related specs of the whole run once (`verify.py --related`, plus `changed` when it was approved). Everything must pass. Reconcile its test-result: a failure here is a test PBI's rework, or a build PBI's when the test shows a real bug. After a fix, run only the specs that failed.
6. **Full suite.** When the close passed and `final` is set, end the episode with a question of kind `full-run` (with `state`): the related specs passed, and running the whole suite takes about <the time the project or the last full run says>. On `full-run: yes` (the `Answer:` file), start the tester with kind `full` once (`verify.py --final`; it refuses without the user's yes and uses the yes up). On no, go on. When the full run fails, fix, run the failed specs, and ask `full-run` again before another full run.
7. Write a stage record `tests` in STATUS.md: counts per verdict, the test PBIs, the final run, and whether the full suite ran.

## Test principle (fallback)

A test claims what the code does for its users: a caller, an end user, another service. Keep a test that fails when that behaviour breaks and keeps passing when only the internals change. Drive the code through its users' interface with a concrete input, take the expected value from the requirement, and assert what the contract promises, no more and no less. Rewrite or delete a test that asserts private helpers, internal call order or data the test built itself.
