# Appendix: tests at the end

Read this at Start of every run. The option is on by default: the bundled templates set `build.tests_at_end`. It is off when the run copy `docs/pbi/tests-at-end.json` says `"enabled": false`, when the user asks for workers to run their own tests, or when no test command is found (below).

With the option on, workers change only the code during the waves. They write no tests, change no tests and run no slow checks. After each wave and each rework the tester runs the fast deferred checks. E2E runs once, after the last wave, in the `e2e` episode: a script with no model (`e2e_run.py`), so no designer waits on it. Then the test stage runs on the lead of the test episode (the routing template's middle tier), which hands a point to the designer only when it cannot decide. The scope of the E2E and whether the full suite runs are decided at the approval. You reconcile each test-result and decide.

Use it when several workers share one working tree and the test run is slow or flaky under load: their test runs fail on other PBIs' half-done changes, and you run the tests again at the end of the wave anyway.

## Start

1. When `docs/pbi/tests-at-end.json` is missing, write it from the template's `build.tests_at_end` (shape: `<skill>/examples/tests-at-end.json`). The run copy wins; `verify.py` and `e2e_run.py` read only the run copy. To turn the option off, write `{"enabled": false}`.
2. The bundled template leaves `deferred` and `test_paths` empty. Draft them from the repository (its package scripts, test runner config, and where its test files live) and show the draft in the PBI list approval (`designer.md`, `## Approve`); do not stop to ask before that. The fields:
   - `deferred`: substrings of verify commands that workers never run. Every command that contains one of them runs by the tester after each wave (kind `wave`), or only in the test stage with `when: end`. Put here the test runs and the checks that need tests (for example a check that every domain file is read by a test).
   - `test_paths`: globs of test files. Workers never create, change or delete them.
   - `test_command`: the command that runs one test file, with `{file}` for its path (for example `pnpm vitest run {file}`). `tests_plan.py` writes it into every test PBI.
   - `test_typecheck` (optional): the type check of the test project. `tests_plan.py` adds it to every test PBI.
   - `principle`: the skill name of the test principle the test stage follows. The default is pstack's `principle-test-behavior-not-implementation`. Find its `SKILL.md` with Glob under `~/.claude/plugins/`; it cannot be invoked as a skill (`disable-model-invocation`), so pass its path. When it is missing, use `## Test principle (fallback)` below.
   - `skill` (optional): the name of a test skill the tester calls instead of `verify.py --deferred`. Without it, the tester runs `verify.py --deferred`.
   - `implement_tier` (optional): the tier (`haiku`, `sonnet` or `opus`) that builds the test PBIs of the test stage (`## Test stage`, step 4). The bundled templates set `haiku`, the bottom tier of the routing template: the runners and models of that tier come from the routing template and the priority template in force (`select_model.py`), so a routing that sends the bottom tier to an external runner writes the tests there. Without it (or `null`), the test PBIs are routed by the decision table like any PBI.
   - `e2e`: how the `e2e` episode finds its scope (`## E2E`): `base` (the branch the work merges into, default `origin/main`), `specs` (a glob or a list of globs of the E2E spec files, for the specs that read a changed symbol), `symbols` (`false` turns that search off) and `full` (whether the full suite runs once; written only from the approval's answer, `appendix/approve.md`).
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
- `limits`: `{"per_spec": 900, "retries": 2, "idle": 600}` unless the project says otherwise: one spec runs at most 15 minutes and is run again at most twice; the full suite has no time limit but stops after 10 minutes with no output.
- `notes`: other rules for the tester, one line each.
- `sources`: where each rule came from (file or memory id).

The project's rules win over Atelier's defaults, and the user's memory over a default too. Show every rule in the approval draft (`appendix/approve.md`, `## Who builds`), one line per point where a project rule replaced a default. When nothing is found, draft from the package scripts as above and say so. Only what the user approved reaches the tester.

## Plan

- Keep test files out of the build PBIs. Their Targets name no file in `test_paths`, and their acceptance criteria ask for no new test. Write the behaviour a test should pin into the PBI's Notes under `Tests for the end:` (one line each, with the requirement it comes from). The test stage reads these lines.
- A worker-run verify command must pass without touching a test file. When a command also checks the test files (a type check that includes the test project, a lint that runs the tests), split it: the part on the source stays with the worker, the part on the tests goes to `deferred`. Example: the type check of the source project for the worker, the type check of the test project deferred.
- When a code change breaks an existing test by design (a renamed export, a changed signature, a moved screen element), write one line per test in the PBI's Notes: `Breaks by design: <test file or spec>[::<test name>] — <why>`. `role_results.py classify` reads exactly this form and turns those failures into `intended` without a model; a breakage described any other way stays `unknown` and costs an investigation. The test stage fixes those tests; the worker does not.
- The worker of a build PBI writes the concrete test cases of what it changed under `## Test cases` in its report (`worker.md`). It has just read the code, so the cases cost it little; the test stage turns them into test PBIs that a cheaper model writes (`## Test cases`, below).
- Give every build PBI the line `- Related specs: <spec>, <spec>` (or `none`) at its head: the E2E specs that exercise what it changes (from the spec files' names and what they import or visit). `e2e_run.py` reads this line; the approval draft shows it.
- A build PBI's verify commands never run E2E specs or the whole E2E suite (the `final` command, or the E2E script with no spec filter). `verify.py` refuses a `final` command with exit 2; E2E runs only in the `e2e` episode.

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

1. When every PBI of the wave has been checked, start the tester (`prompts/tester.md`, kind `wave`). It runs the deferred commands of `when: wave` once for all of them, and no E2E spec:

   ```bash
   python <skill>/verify.py <report>,<report>,... --deferred --when wave
   python <skill>/role_results.py classify docs/pbi/verify/tests/wave-<n>.test-result.json
   ```

   Each distinct command runs once, and its result is merged into each report's `<report>.json`. After a rework, the tester runs the same, for the reworked PBIs only. When `docs/pbi/tests-at-end.json` has a `skill`, the tester calls that skill instead. It writes each failure as a test name, file, PBI and a `cause` guess (`intended`, `regression` or `unknown`); `classify` then marks the failures a `Breaks by design:` line names as `intended`.
2. Reconcile the tester's test-result (`appendix/episodes.md`, `## Reconcile`). The `cause` is a proposal; you decide each one. Compare the failing tests with the previous wave's list (`docs/pbi/verify/tests-at-end.failing.md`; empty before wave 1). For each failure that is new:
   - `intended`: add it to the list with the PBI. The test stage fixes it. It does not block the PBI.
   - `regression` (a behaviour the PBI should have kept): rework that PBI, as for a failing acceptance criterion (`appendix/rework.md`).
   - `unknown`: start the tester with kind `investigate` on those failures. Decide from its proposal. Never stash or reset to find out, and do not run the tests yourself.
3. Record each PBI with `record_run.py`. A deferred number that failed only for a reason in the list counts as pass; write `deferred: <numbers>, failing tests listed` in its STATUS.md note.
4. Write the updated list to `docs/pbi/verify/tests-at-end.failing.md`, one line per test: file, test name, the PBI, why it fails.
5. After the last wave, end the build episode with next `e2e` when `tests-at-end.json` has `investigate` or `final`; otherwise with next `test`.

## E2E

The `e2e` episode has no designer. The conductor runs `e2e_run.py` (`conductor.py run`; the caller waits with `wait`), which:

1. takes the scope: every build PBI's `Related specs:`, then the specs matching `e2e.specs` that read a symbol the change defines or touches (the names of the definitions on the changed lines, and of the function around each changed hunk, since the merge-base), then the full suite once when `e2e.full` is `true`;
2. runs the specs one at a time (`verify.py --related <specs> --each --name e2e`; the server starts once, a failing spec runs again up to `limits.retries` times), then the full suite (`verify.py --final`);
3. writes `docs/pbi/verify/tests/e2e.test-result.json` (kind `e2e`, one failing row per spec, with the PBI that names it or changed the symbol it reads; a failed full suite gives one row per spec its log marks as failed, or one `full suite` row that says to read the log when no spec can be read from it) and `docs/pbi/verify/e2e.scope.md` (each spec and why), marks the `Breaks by design:` failures `intended`, and ends the episode with next `test`.

The script never asks. When it stops on a limit or a server, it writes `blocked` in the test-result and in its summary, and the test stage decides.

## Test stage

Run it once, after the `e2e` episode (or after the last wave when the run has no E2E) and before the review stage. The test episode runs on its lead: the `episodes.test` role, by default the first usable step of the routing template's middle tier (`appendix/setup.md`, `## roles`). Its prompt says `Lead: test`.

- The lead does every step below. When a step needs a judgement the lead cannot make from the requirement, the notes and the test-results (whether a failure is a regression, whether a test may be deleted, a case whose expected value the requirement leaves open but the user should not be asked), it writes the point in the state tablet and ends the episode with next `{"episode": "test", "needs_designer": "<the point>"}`. The designer gets the same episode with `Escalated: <the point>`, decides it, writes the decision in the state, and ends with next `test` to hand the stage back.
- A question for the user (a `decision`) is asked as in any episode; the lead asks it itself.

1. **Range.** List the source files changed since the base (`git diff --name-only <base>...` plus untracked files). The range is the tests in `test_paths` that read those files, the failing list, the failing rows of the e2e test-result, and the `Tests for the end:` lines of every PBI.
2. **Survey.** Start the tester (`prompts/tester.md`, kind `survey`) with this brief: the principle's `SKILL.md` path, the range, the failing list and the `Tests for the end:` lines. It reads each test in the range and writes `docs/pbi/notes/test-survey.md`, one row per test or missing test:

   | file:test | verdict | reason | action |
   |---|---|---|---|

   - `keep`: catches a broken behaviour and survives a change of internals.
   - `fix`: fails now, and the behaviour it asserts still holds. The action says what to change.
   - `rewrite`: asserts an implementation detail, or proves nothing. The action says what behaviour to assert instead.
   - `delete`: pins implementation only, or repeats another test, and no behaviour is lost. The reason starts with `covered by <file:test>` (the test that still covers the behaviour) or `none needed`.
   - `add`: a `Tests for the end:` line, or a behaviour the change introduced, that no test covers. The action names the observable result to assert.
   Under the table, for every test file with a `fix`, `rewrite` or `add` row, the survey writes a `### Cases: <test file>` heading, one `Exemplar:` line and the cases in the shape of `## Test cases` (below), taken from the `## Test cases` of the build reports where they cover the row.
   The subagent edits nothing.
3. **Plan the test PBIs.** Run `python <skill>/tests_plan.py docs/pbi`. It writes one test PBI per test file (Targets that file only, Notes with `Cases:`, `Exemplar:` and `Delete:` lines, one acceptance criterion per case id and per delete, the format and lint of `checks.json`, `test_command` and `test_typecheck` as verify commands), adds them to `docs/pbi/waves.json` (at most 3 per wave, because the test runs share the machine), and prints only the exceptions (`exception: <file>: <why>`). Read the exceptions, not the survey:
   - a case with `?` or an empty field: fill it from the requirement and rerun. When the requirement does not decide an expected value that a `Tests for the end:` line or a behaviour of the change depends on, ask the user (a question of kind `decision`) before the test stage closes. Drop the case only when nothing depends on it. A test PBI never carries a case with `?`.
   - a `delete` with no covering test, an `add` that asks for a test of an internal call, a file outside `test_paths`, keep and delete on one test: fix the row in the survey (drop it, or correct it) and rerun. A file that already has a test PBI is kept as it is.
4. **Build the test PBIs.** Write their work tickets (`design_record.py work PBI-<n>`). When `tests_plan.py` printed a `then:` line (`implement_tier` is set), route them with it: `python <skill>/prepare.py docs/pbi --only <the test PBIs> --tier <implement_tier>`. Otherwise route them as usual. Build them wave by wave with `worker.md` as usual.
   A test PBI that fails goes to rework at the same tier. The rework note (`appendix/rework.md`) names only the failing case ids, with the failing assertion and its output; the worker fixes those cases and leaves the passing ones as they are. A failure before any case runs (the file does not compile or import, a shared setup fails, the type check of the test project fails) has no case id: the note names the failure, its output and the setup or import it comes from, and the worker fixes only that. When the failure shows that the expected value of a case is wrong, fix the case in the PBI's `Cases:` first (a decision; the lead hands it to the designer when the requirement does not settle it). When it shows a real bug in the source, it is a build PBI's rework, as in step 5. Raise the tier only when the user picks that (`appendix/rework.md`, `## Stop`).
5. **Close.** When every test PBI passes, start the tester (`prompts/tester.md`, kind `final`). It runs the verify commands of every report again with `--skip-deferred`, the deferred commands of `when: end` once, and the specs that failed in the `e2e` episode (for a `full suite` row, the failing specs you read from its log) plus the specs a test PBI changed, one at a time (`verify.py --related <specs> --each --name final-related`). Everything must pass. Reconcile its test-result: a failure here is a test PBI's rework, or a build PBI's when the test shows a real bug (`regression`; an `unknown` goes to the tester's kind `investigate` first). After a fix, run only the specs that failed. The full suite never runs here: it ran in the `e2e` episode when the approval said yes.
6. Write a stage record `tests` in STATUS.md: counts per verdict, the test PBIs, the exceptions `tests_plan.py` printed, the final run, whether the full suite ran, and each point handed to the designer.
7. End the episode with next `review`, or `build` with the wave and `pbis` of the build PBIs a regression sends back.

## Test cases

A case is one row. Its fields are what a cheaper model needs to write the test without reading the source beyond the imports:

| id | behaviour | input | expected | mock |
|---|---|---|---|---|
| C1 | <what the user of the code sees, one line> | <the concrete call or action and its arguments> | <the concrete result, error or output, from the requirement> | <what is replaced and what it returns, or none> |

- `id` is unique in the test file (`C1`, `C2`, ...). `behaviour` names the requirement or the `Tests for the end:` line it comes from.
- `input` and `expected` are literal values, not "a valid user" or "the right error". Name the function or the screen action, the arguments, and the exact value, message or state.
- `mock` names each boundary that is replaced (a network call, the clock, a file system) and what it returns. Mock only at the boundaries of the code; never mock the unit under test.
- `Exemplar: <path of an existing test file>`: one per test file, the closest existing test in style (same runner, same kind of target). The writer copies its imports, setup and assertion style. When the project has no test yet, write `Exemplar: none` and name the runner and its assertion style on the same line.
- A case covers one behaviour. Boundaries (empty, one, limit, over the limit) are separate cases.

## Test principle (fallback)

A test claims what the code does for its users: a caller, an end user, another service. Keep a test that fails when that behaviour breaks and keeps passing when only the internals change. Drive the code through its users' interface with a concrete input, take the expected value from the requirement, and assert what the contract promises, no more and no less. Rewrite or delete a test that asserts private helpers, internal call order or data the test built itself.
