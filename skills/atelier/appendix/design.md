# Appendix: understand and design

Read this when the understand or design stage picks `understand.builtin`, `design.builtin`, or an external understand or design component (`appendix/engine.md`).

## Understand

1. Read the code the request touches, and search shared memory within the limits of `appendix/memory.md`.
2. When a question needs a wide search, give it to a subagent: it writes the conclusion to `docs/pbi/notes/<name>.md` and returns at most 5 lines and the path.
3. Keep what later stages need in notes, not in your context. The PBIs link the notes they depend on.

## Design: the decision

Take this decision right after understand, before the optional stages placed after it (`map`, `lint-survey`, whose `when` is `ask-or-decide`). The design stage then uses the saved decision and does not ask again.

1. **Spec given**: the run starts with a spec path (a `docs/specs/*.md` file in the request, or a handoff from a design skill that passes one). The design is done: record `stage design: built-in skip (spec given)`.
2. **Setting**: run `python <skill>/adapter_gate.py docs/pbi` (no PBIs needed yet) and take the `design` row's setting.
   - `off`: skip without asking and record `off (turned off in agents.json)`.
   - a skill name: in `## Design: run`, use that skill instead of the template's design components when it is in the skill list. When it is not, keep the template's order and record the fallback with its reason.
   - `auto`: keep the template's order.
3. Decide from the request and a short look at the repository:
   - **Clearly complex** (run without asking): the request adds a new concept, an external interface or a data-model change, and the work likely spans 2 or more modules.
   - **Clearly simple** (skip without asking): the direction is already set (a spec exists, or the work continues earlier work), or the work stays in one module with 3 or fewer PBIs and adds no new concept.
   - **Otherwise**: end the episode with a question (kind `decision`) before the split, asking whether to run the design step, laid out as the `decision (design choice, ask-user, map, optional)` row of `appendix/questions.md` (`## What goes in each kind`); the answer comes in the next launch's `Answer:` (inline: ask once with AskUserQuestion). Save the answer in `docs/pbi/adapters.json` as `{"design": {"decision": "run|skip", "asked": true}}`. On resume, read it and do not ask again. Do not store it in `docs/pbi/state.json`, which the web sync overwrites.

## Design: run

Read `docs/pbi/notes/map.md` and `docs/pbi/notes/survey.md` when they exist (`appendix/map.md`, `appendix/lint.md`): the spec names the layers each change belongs to, and the confirmed bug-hunt leads inside the range.

1. Run the picked component in your session: the skill named in the setting, or the template's design skill when it is in the skill list. The bundled templates have none; the user adds one as an external component (`appendix/engine.md`, `## External components`).
2. Then write `docs/specs/<slug>.md` yourself, where `<slug>` is a short lowercase name for the work. Headings, in this order: Problem Statement, Solution, User Stories, Implementation Decisions, Testing Decisions, Out of Scope, Further Notes. The design skills decide; they do not write the spec.
3. When `design.builtin` is the one picked (no design skill in the skill list), write the decisions you made in the same file. Record `run` when the template has no design skill, or `fallback (<skill> not in the skill list)`.

## Design: grill

A run that designs (the design step `run`) always grills, with or without a design skill: the user answers every question of the design tree, and you answer none of them. When `mattpocock-skills:grilling` is in the skill list, follow its method for the questions; otherwise follow the steps here.

1. Keep the tree in `docs/pbi/notes/grill.json` (schema `atelier.grill/1`): `rounds` (each `{"n", "questions": [{"id", "question", "recommended", "answer", "answered_in"}]}`), `frontier` (the open questions that wait on earlier answers) and `aligned` / `aligned_in`.
2. A round is every question whose prerequisites are settled. Write it to `docs/pbi/notes/grill-<n>.md`: number each question (`Q1`, `Q2`, ...), and word it so that "yes" takes the recommendation. Facts you can look up are not questions: look them up.
3. End the episode with a question (kind `decision`, with `state`, its path the round file), laid out as the `decision (one grill round)` row of `appendix/questions.md` (`## What goes in each kind`): one page for the round, one pair of diagrams, one `ask` per Q with the recommended answer first. The next launch has the `Answer:` line: copy each answer into `answer`, and the `Answer:` path into `answered_in`. Never write an answer the user did not give, and never close a question as "decided in the spec".
4. Recompute the frontier from the answers and ask the next round, until the frontier is empty. Then ask whether the understanding is shared (kind `decision`), laid out as the `decision (end of grill)` row of `appendix/questions.md` (`## What goes in each kind`). Only when the user says yes, its `Answer:` path goes into `aligned_in`, with `aligned: true`. When the user says no or names what is still open, leave `aligned` false, ask the next round about those points, and ask again afterwards.
5. Only then write the spec and go on. `done` refuses an approve question while a question has no answer from an `Answer:` file, the frontier is not empty, or `aligned_in` is missing.

The unknowns below are the points the grill did not reach; in a run that grills, ask them in a round too.

## Design: unknowns

An unknown is a point the request and the code do not settle (a behaviour, a boundary, a name the user expects). The user knows the answer, so you ask rather than assume.

1. Read only the lines the point depends on, and keep them in its slice (`design_record.py slice <id> --question <q> --read <path>:<start>-<end> --unknown <u>`, `appendix/adapters.md`, `## Confirm`).
2. When the lines settle it, write the decision in the spec and go on.
3. When it is still open and its answer changes the PBI split, an interface or the data model: collect every such point and end the episode with one question (kind `decision`, with `state`) before plan, laid out as the `decision (design choice, ask-user, map, optional)` row of `appendix/questions.md` (`## What goes in each kind`). Ask once for all of them.
4. When it is still open and its answer changes only details inside a PBI: put it in the approval draft as an `ask` block (the choices, yours first), under `## 決めること` (`## Decisions`, `appendix/approve.md`). Nothing goes on with an open question unanswered:
   - With the `confirm` adapter on, `design_record.py check-reply` refuses a reply that leaves an `ask` unanswered or out.
   - With `confirm` `off` there is no check, and any reply comes back as the `Answer:`. Read it against the questions under `## 決めること` first: when it leaves one unanswered, end with kind `approve` again, asking only those, before anything goes to build.
