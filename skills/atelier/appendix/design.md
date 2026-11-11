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
   - **Otherwise**: end the episode with a question (kind `decision`) before the split, asking whether to run the design step; the answer comes in the next launch's `Answer:` (inline: ask once with AskUserQuestion). Save the answer in `docs/pbi/adapters.json` as `{"design": {"decision": "run|skip", "asked": true}}`. On resume, read it and do not ask again. Do not store it in `docs/pbi/state.json`, which the web sync overwrites.

## Design: run

Read `docs/pbi/notes/map.md` and `docs/pbi/notes/survey.md` when they exist (`appendix/map.md`, `appendix/lint.md`): the spec names the layers each change belongs to, and the confirmed bug-hunt leads inside the range.

1. Run the picked component in your session: the skill named in the setting, or the template's design skill when it is in the skill list. The bundled templates have none; the user adds one as an external component (`appendix/engine.md`, `## External components`).
2. Then write `docs/specs/<slug>.md` yourself, where `<slug>` is a short lowercase name for the work. Headings, in this order: Problem Statement, Solution, User Stories, Implementation Decisions, Testing Decisions, Out of Scope, Further Notes. The design skills decide; they do not write the spec.
3. When `design.builtin` is the one picked (no design skill in the skill list), write the decisions you made in the same file. Record `run` when the template has no design skill, or `fallback (<skill> not in the skill list)`.

## Design: unknowns

An unknown is a point the request and the code do not settle (a behaviour, a boundary, a name the user expects). The user knows the answer, so you ask rather than assume.

1. Read only the lines the point depends on, and keep them in its slice (`design_record.py slice --read <path>:<start>-<end> --unknown <u>`, `appendix/adapters.md`, `## Confirm`).
2. When the lines settle it, write the decision in the spec and go on.
3. When it is still open and its answer changes the PBI split, an interface or the data model: collect every such point and end the episode with one question (kind `decision`, with `state`) before plan, with the choices you see and the one you would pick. Ask once for all of them.
4. When it is still open and its answer changes only details inside a PBI: put it in the approval draft as an `ask` block (the choices, yours first), under "Open questions". An unanswered `ask` is not an approval (`design_record.py check-reply`), so nothing goes on unanswered.
