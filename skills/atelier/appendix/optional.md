# Appendix: optional stages

Read this when the template has an `optional` list and one of its rows is picked (`appendix/engine.md`). Optional stages add work that not every run needs. A template turns one on by listing it; the bundled templates list none.

## The stages

| Stage | Placed after (default) | What it adds | Artifact |
|---|---|---|---|
| `ui-design` | `design` | the look of the screens the work adds or changes: direction, theme, page and component mockups, a walkthrough video | `docs/pbi/ui/<slug>/` |
| `marketing` | `understand` | who the work is for and how they react: a simulated user panel, objections, positioning, a launch note | `docs/pbi/marketing/<slug>.md` |
| `security` | `review` | a security audit of the changed code: trust boundaries, confirmed vulnerabilities with source evidence, leads that need validation | `docs/pbi/security/<slug>.md` (a summary; the skill keeps its full output where it says) |

- The template says where each one runs (`{"stage", "after", "components"}`, `after` any stage but `finish`, so its files are ready for the completion report). Each optional stage appears at most once.
- Optional stages need no fallback. When no row is usable or no `when` holds, skip the stage and record why.
- Like a gate, an optional stage runs every row that is usable and whose `when` holds, in order. A UI design can take a design skill and then a video skill.

## when for optional stages

| Value | Holds when |
|---|---|
| `ui-change` | the request adds a screen, changes how one looks, or changes a user flow |
| `user-facing` | the request adds or changes something end users see or pay for |
| `ask-user` | the user said yes when asked |

You decide `ui-change` and `user-facing` from the request, like `clearly-complex`. When you cannot tell, ask once with AskUserQuestion before the first row of that stage runs, and save the answer in `docs/pbi/adapters.json` as `{"<stage>": {"decision": "run|skip", "asked": true}}`. When the design decision (`appendix/design.md`) also needs asking at that point, put both in the same AskUserQuestion. An optional stage placed before design (marketing, by default) is asked about on its own.

`ask-user` never decides on its own: ask every run, once, even when the answer seems obvious. For a stage placed after `plan` or later (security, by default), put the question in the PBI list approval (Approve in `SKILL.md`), so the run does not stop again later; for a stage placed earlier, ask before its first row. Save the answer in `docs/pbi/adapters.json` the same way, so a resume does not ask again. With no answer, the stage does not run.

## Run

1. Run each picked row as its `where` says (`appendix/engine.md`). Give it the request, the spec when one exists (`docs/specs/<slug>.md`), the notes so far, and the artifact folder above.
2. The skill decides how to work; you do not rewrite its output. Ask it to put the files it makes (HTML, images, videos, notes) in the artifact folder, or copy them there when it writes to its own place, and to list them in `docs/pbi/ui/<slug>/README.md` or at the top of the marketing note.
3. Keep only paths in your context. Do not read videos or large HTML files.

## Security

- Run the skill on the changes of this run: give it the repository, the merge-base and the head, the spec, and the PBI list, and ask for a review of the code those PBIs changed (with Cloudflare's `security-audit`, that is its full audit mode limited to those paths). Use its default output directory outside the repository unless the user chose one; never write audit files into the repository except the summary below.
- The skill builds and runs target code only inside the sandbox it requires; if that sandbox is missing it reports leads as needing validation. Do not loosen that.
- Write `docs/pbi/security/<slug>.md`: the skill's output path, then each confirmed finding (file, one-line impact, severity) and each lead that needs validation (one line each). Keep only paths and these lines in your context.

## How later stages use it

- **design** (when `marketing` runs before it): read the marketing note's conclusions and put the user-facing decisions in the spec.
- **plan**: a PBI that builds a screen links its mockup and theme files in its Notes, so its worker builds to them. The acceptance criteria name the mockup when the screen must match it.
- **review**: the reviewer compares the built screens with the mockups when the PBI links them.
- **fix** (after `security`): a confirmed finding in a file a PBI changed sends that PBI back like a review finding (it counts toward the rework limit). A confirmed finding outside the PBIs' files, and every lead that needs validation, goes to the completion report under findings; do not fix them in this run.
- **finish**: the completion report lists the artifacts under the changes, with paths.

## Records

Write a stage record for each row, as for any stage: `stage ui-design after design: ui-design.design-studio run (ui-change: new settings page)`. An optional stage that did not run gets one line: `stage marketing after understand: skip (user-facing: internal refactor)`.
