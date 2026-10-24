# Appendix: understand and design

Read this when the understand or design stage picks `understand.builtin`, `design.grilling`, `design.domain-modeling` or `design.builtin`.

## Understand

1. Read the code the request touches, and search shared memory within the limits of `appendix/memory.md`.
2. When a question needs a wide search, give it to a subagent: it writes the conclusion to `docs/pbi/notes/<name>.md` and returns at most 5 lines and the path.
3. Keep what later stages need in notes, not in your context. The PBIs link the notes they depend on.

## Design: the decision

1. **Spec given**: the run starts with a spec path (a `docs/specs/*.md` file in the request, or a handoff from a design skill that passes one). The design is done: record `stage design: built-in skip (spec given)`.
2. **Setting**: run `python <skill>/adapter_gate.py docs/pbi` (no PBIs needed yet) and take the `design` row's setting.
   - `off`: skip without asking and record `off (turned off in agents.json)`.
   - a skill name: in `## Design: run`, use that skill instead of the template's design components when it is in the skill list. When it is not, keep the template's order and record the fallback with its reason.
   - `auto`: keep the template's order.
3. Decide from the request and a short look at the repository:
   - **Clearly complex** (run without asking): the request adds a new concept, an external interface or a data-model change, and the work likely spans 2 or more modules.
   - **Clearly simple** (skip without asking): the direction is already set (a spec exists, or the work continues earlier work), or the work stays in one module with 3 or fewer PBIs and adds no new concept.
   - **Otherwise**: ask once with AskUserQuestion, before the split, whether to run the design step. Save the answer in `docs/pbi/adapters.json` as `{"design": {"decision": "run|skip", "asked": true}}`. On resume, read it and do not ask again. Do not store it in `docs/pbi/state.json`, which the web sync overwrites.

## Design: run

1. Run the picked component in your session: the skill named in the setting, or `design.grilling` when it is in the skill list. Its `then` runs `design.domain-modeling` after it, only when GLOSSARY.md or CONTEXT.md exists in the target repository.
2. Then write `docs/specs/<slug>.md` yourself, where `<slug>` is a short lowercase name for the work. Headings, in this order: Problem Statement, Solution, User Stories, Implementation Decisions, Testing Decisions, Out of Scope, Further Notes. The design skills decide; they do not write the spec.
3. When `design.builtin` is the one picked (no design skill in the skill list), write the decisions you made and the assumptions you put in, in the same file. Record `fallback (grilling not in the skill list)`. List the assumptions in the approval list under "Open assumptions", a few lines each, so the user can correct them there.
4. When step 3 applies and the work is clearly complex, add one line to the approval list, in the user's language: "To deepen the design, run `setup --recommended` to install mattpocock-skills." Then write `<home>/notices.json` as `{"recommended_hint_shown": "<ISO datetime>"}`. When that key is already there, do not show the line again.
