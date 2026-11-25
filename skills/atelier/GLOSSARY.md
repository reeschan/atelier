# Atelier v8

Takes a coding request that spans several files, turns it into an agreed spec and small work items, routes each item to a model, builds and checks them, and reviews the whole once.

## Roles

**Caller (呼び出し側)**:
The session the user talks to. It launches every model role at depth 1 when the conductor says so, and carries questions and answers.
_Avoid_: main agent, parent session

**Conductor (進行役)**:
The script that holds a run's state and gives the next action. It is not a model.
_Avoid_: orchestrator, scheduler, dispatcher

**Surveyor (調べる役)**:
A cheap model, with the committed OMM or the OMM cache, that writes the fact sheet.
_Avoid_: scanner, explorer

**Interviewer (詰める役)**:
The model that runs the grill and writes the spec. It never writes work items.
_Avoid_: designer, griller

**Planner (決める役)**:
The model that turns an approved spec and the fact sheet into work items, and triages the final review's findings. Launched at fixed points only.
_Avoid_: designer, orchestrator

**Worker (実装役)**:
A model that builds one work item, or one group of fixes, from its ticket alone.
_Avoid_: implementer, child agent, fixer

**Reviewer (見直し役)**:
The model that reviews the whole change once, after every work item has passed its checks.
_Avoid_: checker, QA

## Artifacts

**Spec (仕様)**:
The agreed statement of what to build, in the to-spec layout. Only an approved spec reaches the planner.

**Design tree (設計の木)**:
The grill's state: one line per settled decision, and the list of open questions.
_Avoid_: grill log, transcript

**Fact sheet (事実の束)**:
What the surveyor found about the repository, each item with its source.
_Avoid_: context dump, notes

**Committed OMM (コミット済みの OMM)**:
The `.omm/` architecture diagrams and descriptions that the target repository keeps in its own commits. A run reads it as of the commit that last changed it and never writes to it.
_Avoid_: repository cache, tracked scan

**OMM cache (OMM のキャッシュ)**:
Scans of the target repository kept outside it, keyed by the commit they describe. Used only when the repository has no committed OMM.
_Avoid_: committed OMM, local .omm

**Rule document (規約の文書)**:
A document the target repository says to read before implementing or reviewing. The surveyor cites it, and the planner, workers and reviewer read it whole.
_Avoid_: guideline, conventions file

**Review skill (レビューのスキル)**:
The target repository's own review skill. When the plan names one, the caller runs it as the final review instead of the reviewer.
_Avoid_: external reviewer

**Data need (外のデータ)**:
A file outside the repository that a PBI's checks need, with where it goes inside the repository. The user approves it with the plan, and the caller copies it before the worker starts.
_Avoid_: fixture request, asset

**PBI**:
One small unit of work a worker can build from its ticket alone.
_Avoid_: task, ticket

**Retrospective list (振り返りの一覧)**:
Everything the run set aside to decide once at the end: the final review's findings, objections, unresolved failures, plan returns, mutation survivors, E2E failures and unlock requests, each with its kind.
_Avoid_: review.yaml, backlog, issue list

**Fix list (直しの一覧)**:
The items of the retrospective list the user approved to fix.

**Leftover (残り)**:
An item still failing after the fix stage. The run does not fix it; it goes to the backlog, and the run is not complete.
_Avoid_: carry-over, remaining TODO

**Backlog (backlog)**:
The leftovers of past runs of one repository, kept outside the working tree, which the next run's planner reads as candidate PBIs.
_Avoid_: todo list, issue tracker

**Run record (run の記録)**:
One line per launch, check and fix: role, model, tokens, rules read and rules fired.
_Avoid_: events, metrics log

**Carry-over (持ち越し)**:
A failed check that the run records and goes on past instead of stopping. It is fixed by a fix PBI, or in the fix stage; while one is open, the run is not complete.
_Avoid_: TODO, deferred failure, hold

**Fix PBI (修正の PBI)**:
A PBI the conductor writes from a carry-over and puts before the PBIs that depend on the failed one. The planner does not write it.
_Avoid_: rework PBI, auto-fix

**Unresolved (未解決)**:
A carry-over whose fix PBI failed twice, or a failed check that no PBI owns. The run holds the PBI and those that depend on it, goes on without asking, and puts it on the retrospective list.
_Avoid_: blocked, rebuild question

**Objection (異議)**:
A worker's record that a locked case looks wrong, written instead of changing it. The worker goes on, and the objection goes on the retrospective list.
_Avoid_: carry_over, dispute, test edit

**Plan return (途中の戻り)**:
A pause in the middle of a run, asked of the user, to let the planner change a public interface or a scenario that later PBIs cannot be built on. Each one is counted and goes on the retrospective list.
_Avoid_: replan, escalation

## Acceptance

**Acceptance PBI (受け入れの PBI)**:
A PBI that turns approved scenarios into runnable tests and changes no source. All of them are built in the first wave, before any building PBI.
_Avoid_: test PBI, tester task

**Scenario (シナリオ)**:
One behaviour the spec requires, written in Gherkin as a Scenario Outline, which the user approves with the plan.
_Avoid_: acceptance criterion, test case

**Case (ケース)**:
One row of a scenario's examples, with an ID such as `REQ-12/C1`. Cases, not scenarios, are assigned to PBIs, counted and reported.
_Avoid_: example, test, scenario

**Red check (Red の確認)**:
The check of an acceptance PBI. It passes when every case fails for the missing behaviour (an assertion or a not-implemented error), never for a broken import or a case that already passes.
_Avoid_: failing test run

**Lock (鍵)**:
The acceptance test files, their settings and the public interfaces that no worker may change, recorded outside the working tree with the commit they were locked at.
_Avoid_: test_paths, do_not_touch, freeze

**Light mode (軽量モード)**:
A way to build a small requirement without scenarios or approval: its cases are plain tests with IDs, still checked red and locked.
_Avoid_: quick mode, no-test mode

**Hidden case (控え)**:
A case the worker never sees: the planner writes only the relation between its values, and the conductor picks new values at each wave's check.
_Avoid_: hold-out, hold, secret test

**Mutation survivor (生き残り)**:
A small change to the changed code that no test caught. It goes on the retrospective list as a gap in the scenarios or in the unit tests.
_Avoid_: mutant, weak test

## Routing

**Level (難度)**:
How hard a PBI is, L1 to L5, defined by an expert's time estimate and whether design judgment is needed. Never named after a model.
_Avoid_: tier, 段, model class

**Route (経路)**:
The runners, models and efforts to try for one level, in order.
_Avoid_: assignment, fallback chain

**Template (テンプレート)**:
The user's single settings file: routes per level, a model per role, limits, parts on or off, and which rule texts are read.
_Avoid_: routing template, priority template, workflow template

**Template switch (テンプレートの切り替え)**:
Using another template until an expiry date.
_Avoid_: priority template, override

**Runner (実行先)**:
What runs a model role: Claude Code itself, or an external CLI.
_Avoid_: provider, backend

**Sensitive candidate (sensitive の候補)**:
A PBI the planner flags as needing care. Only the user decides it is sensitive, and a sensitive part leaves the run.

## Flow

**Stage (工程)**:
One fixed step of a run: OMM scan, survey, grill, plan, approve, data preparation, build, check, review, fix, e2e, report.
_Avoid_: 段, phase, episode

**Round (巡)**:
One exchange of the grill: a batch of questions and the user's answers.

**Wave (波)**:
PBIs with no unfinished dependency, built together in one or more launch batches, then checked once.

**Next action (次の行動)**:
What the conductor hands to the caller or the drive next: one launch, one launch batch, a check, a question, and so on.
_Avoid_: pending action, step, task

**Launch (起動)**:
One run of a model role: one role does one piece of work (a PBI, a fix group, the plan, the review) and writes one result file. Each launch has its own ID, and usage and guard rules are recorded per launch.
_Avoid_: job, call, invocation

**Launch batch (一斉起動)**:
Launches started together and awaited together: in the same wave or the same fix stage, with files to change that do not overlap, on the same kind of runner (external CLIs, or the caller's subagents), up to the template's parallel limit.
_Avoid_: 束, bundle, parallel job

**Check (機械の検査)**:
A command whose exit code decides pass or fail. No model judges it.
_Avoid_: verification by review

**Repository check (全体の検査)**:
A check over the whole repository (the whole test suite, the type check and the like), listed in the plan's `checks`. The conductor runs it once after each wave and once after the fix stage; a worker runs only the tests related to its own work.
_Avoid_: PBI check, worker check

**Guard rule (守りの規則)**:
One of the rules that protect against harm (G1 to G14), each with how it is stopped.
_Avoid_: caution, note, warning
