# Appendix: tests at the end

Read this at Start of every run. The option is on by default: the bundled templates set `build.tests_at_end`. It is off when the run copy `docs/pbi/tests-at-end.json` says `"enabled": false`, when the user asks for workers to run their own tests, or when no test command is found (below).

With the option on, workers change only the code during the waves. They write no tests, change no tests and run no slow checks. You run the slow checks once per wave, and after the last wave one test stage reviews, fixes, adds and deletes the tests of the whole change.

Use it when several workers share one working tree and the test run is slow or flaky under load: their test runs fail on other PBIs' half-done changes, and you run the tests again at the end of the wave anyway.

## Start

1. When `docs/pbi/tests-at-end.json` is missing, write it from the template's `build.tests_at_end` (shape: `<skill>/examples/tests-at-end.json`). The run copy wins; `verify.py` reads only the run copy. To turn the option off, write `{"enabled": false}`.
2. The bundled template leaves `deferred` and `test_paths` empty. Draft them from the repository (its package scripts, test runner config, and where its test files live) and show the draft in the PBI list approval (`SKILL.md`, `## Approve`); do not stop to ask before that. The fields:
   - `deferred`: substrings of verify commands that workers never run. Every command that contains one of them runs once per wave, by you. Put here the test runs and the checks that need tests (for example a check that every domain file is read by a test).
   - `test_paths`: globs of test files. Workers never create, change or delete them.
   - `principle`: the skill name of the test principle the test stage follows. The default is pstack's `principle-test-behavior-not-implementation`. Find its `SKILL.md` with Glob under `~/.claude/plugins/`; it cannot be invoked as a skill (`disable-model-invocation`), so pass its path. When it is missing, use `## Test principle (fallback)` below.
3. When no test command is found, `deferred` stays empty and the option does nothing: write `Tests at end: off (no test command)` in STATUS.md, and workers run their verify commands as usual.
4. Otherwise write the option in STATUS.md: `Tests at end: on (deferred: ..., test_paths: ...)`.

## Plan

- Keep test files out of the build PBIs. Their Targets name no file in `test_paths`, and their acceptance criteria ask for no new test. Write the behaviour a test should pin into the PBI's Notes under `Tests for the end:` (one line each, with the requirement it comes from). The test stage reads these lines.
- A worker-run verify command must pass without touching a test file. When a command also checks the test files (a type check that includes the test project, a lint that runs the tests), split it: the part on the source stays with the worker, the part on the tests goes to `deferred`. Example: the type check of the source project for the worker, the type check of the test project deferred.
- When a code change breaks existing tests by design (a renamed export, a changed signature), say so in the PBI's Notes. The test stage fixes them; the worker does not.

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

1. When every PBI of the wave has been checked, run the deferred commands once for all of them:

   ```bash
   python <skill>/verify.py <report>,<report>,... --deferred
   ```

   Each distinct command runs once, and its result is merged into each report's `<report>.json`.
2. Compare the failing tests with the previous wave's list (`docs/pbi/verify/tests-at-end.failing.md`; empty before wave 1). For each failure that is new:
   - Caused by a change a PBI made on purpose (its Notes say so, or the diff renames or removes what the test reads): add it to the list with the PBI. The test stage fixes it. It does not block the PBI.
   - A behaviour the PBI should have kept: rework that PBI, as for a failing acceptance criterion (`appendix/rework.md`).
   - When you cannot tell which PBI caused it, find the files the failing test imports, and the PBI of the wave whose diff touches them. Run that test file alone and decide from its output. Never stash or reset to find out.
3. Record each PBI with `record_run.py`. A deferred number that failed only for a reason in the list counts as pass; write `deferred: <numbers>, failing tests listed` in its STATUS.md note.
4. Write the updated list to `docs/pbi/verify/tests-at-end.failing.md`, one line per test: file, test name, the PBI, why it fails.

## Test stage

Run it once, after the last wave and before the review stage.

1. **Range.** List the source files changed since the base (`git diff --name-only <base>...` plus untracked files). The range is the tests in `test_paths` that read those files, the failing list, and the `Tests for the end:` lines of every PBI.
2. **Survey.** Start one `pbi-worker` subagent on the template's review model with this brief: the principle's `SKILL.md` path, the range, the failing list and the `Tests for the end:` lines. It reads each test in the range and writes `docs/pbi/notes/test-survey.md`, one row per test or missing test:

   | file:test | verdict | reason | action |
   |---|---|---|---|

   - `keep`: catches a broken behaviour and survives a change of internals.
   - `fix`: fails now, and the behaviour it asserts still holds. The action says what to change.
   - `rewrite`: asserts an implementation detail, or proves nothing. The action says what behaviour to assert instead.
   - `delete`: pins implementation only, or repeats another test, and no behaviour is lost. The reason names the test that still covers the behaviour, or says none is needed.
   - `add`: a `Tests for the end:` line, or a behaviour the change introduced, that no test covers. The action names the observable result to assert.
   The subagent edits nothing and returns counts per verdict and the path.
3. **Check the survey.** Read the rows for `delete` and `add` in full. Drop an `add` that asks for a test of an internal call, and a `delete` that would leave a behaviour of the change untested.
4. **Test PBIs.** Turn the rows into PBIs (`appendix/plan.md`), grouped by test file, never two PBIs on the same file. Their Targets are test files only; source files are "Do not touch". Their verify commands run their own test files (one file per command, so the runs stay short), and the type check of the test project. Workers may run these commands. Route and build them as usual, at most 3 at once, because the test runs share the machine.
5. **Close.** When every test PBI passes, run the deferred commands once more on the whole tree (all build and test reports). Everything must pass. A failure here is a test PBI's rework, or a build PBI's when the test shows a real bug.
6. Write a stage record `tests` in STATUS.md: counts per verdict, the test PBIs, the final run.

## Test principle (fallback)

A test claims what the code does for its users: a caller, an end user, another service. Keep a test that fails when that behaviour breaks and keeps passing when only the internals change. Drive the code through its users' interface with a concrete input, take the expected value from the requirement, and assert what the contract promises, no more and no less. Rewrite or delete a test that asserts private helpers, internal call order or data the test built itself.
