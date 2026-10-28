# Appendix: optional stages

Read this when the template has an `optional` list and one of its rows is picked (`appendix/engine.md`). Optional stages add work that not every run needs. A template turns one on by listing it; the bundled templates list none.

## The stages

| Stage | Placed after (default) | What it adds | Artifact |
|---|---|---|---|
| `ui-design` | `design` | the look of the screens the work adds or changes: direction, theme, page and component mockups, a walkthrough video | `docs/pbi/ui/<slug>/` |
| `marketing` | `understand` | who the work is for and how they react: a simulated user panel, objections, positioning, a launch note | `docs/pbi/marketing/<slug>.md` |
| `security` | `review` | a security audit of the changed code: trust boundaries, confirmed vulnerabilities with source evidence, leads that need validation | `docs/pbi/security/<slug>.md` (a summary; the skill keeps its full output where it says) |
| `model-check` | `design` | a checked model of how the system moves between states: the rules the user agrees to, a statechart in the reader's words, the shortest path to each state that breaks a rule | `docs/pbi/model-check/<slug>/` |

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

`ask-user` never decides on its own: ask every run, once, even when the answer seems obvious. For a stage placed after `route` or later (security, by default), put the question in the PBI list approval (Approve in `SKILL.md`), so the run does not stop again later. For a stage placed after `plan` or earlier (model-check, by default), ask before its first row, because the approval comes after route. When the design decision is asked at that point too, put both in the same AskUserQuestion. Only optional stages take `ask-user`; `flow.py` rejects it elsewhere. Save the answer in `docs/pbi/adapters.json` the same way, so a resume does not ask again. With no answer, the stage does not run.

## Run

1. Run each picked row as its `where` says (`appendix/engine.md`). Give it the request, the spec when one exists (`docs/specs/<slug>.md`), the notes so far, and the artifact folder above.
2. The skill decides how to work; you do not rewrite its output. Ask it to put the files it makes (HTML, images, videos, notes) in the artifact folder, or copy them there when it writes to its own place, and to list them in `docs/pbi/ui/<slug>/README.md` or at the top of the marketing note.
3. Keep only paths in your context. Do not read videos or large HTML files.

## Security

- Run the skill on the changes of this run: give it the repository, the merge-base and the head, the spec, and the PBI list, and ask for a review of the code those PBIs changed (with Cloudflare's `security-audit`, that is its full audit mode limited to those paths). Use its default output directory outside the repository unless the user chose one; never write audit files into the repository except the summary below.
- The skill builds and runs target code only inside the sandbox it requires; if that sandbox is missing it reports leads as needing validation. Do not loosen that.
- Write `docs/pbi/security/<slug>.md`: the skill's output path, then each confirmed finding (file, one-line impact, severity) and each lead that needs validation (one line each). Keep only paths and these lines in your context.

## Model check

- Use it when the work changes a flow with states: a screen flow, a checkout, a protocol, a job's lifecycle, a retry or a lease. Say in the question why this request has one, or that it has none.
- Give the skill the spec and the request, and name in the prompt the artifact folder and a scratch folder outside the repository as its outputs (`appendix/engine.md`, `## read-skill components`). It writes the model, its config and its view in a scratch folder outside the repository, and runs its command there. Copy into the artifact folder: `chart.mmd`, the report, the spec and config, the view, and the SVG when one was rendered. Write `docs/pbi/model-check/<slug>/README.md`: each rule in the user's words, held or broken, and for each broken rule the path as the report numbers it. Keep only these lines in your context.
- The first run downloads TLC and Java with mise (about 300 MB, into mise's install folder) and needs the network; the command needs a TypeScript runtime (Node 22.18 or later). When mise has no TLC yet, the question that asks whether to run the stage also says what will be downloaded, from where, about how large, where it goes and how to remove it (`mise uninstall`); a yes to the stage is the permission. When something is missing, record `fallback (<what is missing>)`, say it in one line, draw the statechart by hand in the spec as a Mermaid `stateDiagram-v2` in the reader's words, and let the user check it at the approval.
- Each rule the user agrees to is a decision: put it in the spec. A broken rule is a design finding: fix the design, or put the rule's path in the spec as a known bug the work fixes, and check again once.

## How later stages use it

- **design** (when `marketing` runs before it): read the marketing note's conclusions and put the user-facing decisions in the spec.
- **plan**: a PBI that builds a screen links its mockup and theme files in its Notes, so its worker builds to them. The acceptance criteria name the mockup when the screen must match it.
- **plan** (after `model-check`): the PBI that owns a rule gets an acceptance criterion for it, and each broken rule's path becomes a test that replays it step by step against the real code. The PBI links the chart in its Notes.
- **review**: the reviewer compares the built screens with the mockups when the PBI links them.
- **fix** (after `security`): a confirmed finding in a file a PBI changed sends that PBI back like a review finding (it counts toward the rework limit). A confirmed finding outside the PBIs' files, and every lead that needs validation, goes to the completion report under findings; do not fix them in this run.
- **Approve**: show the model-check chart (`chart.mmd` as a `mermaid` block) and its rules with the PBI list, so a reader who does not read code can check the behavior.
- **finish**: the completion report lists the artifacts under the changes, with paths.

## Records

Write a stage record for each row, as for any stage: `stage ui-design after design: ui-design.design-studio run (ui-change: new settings page)`. An optional stage that did not run gets one line: `stage marketing after understand: skip (user-facing: internal refactor)`.
