# Appendix: optional stages

Read this when the template has an `optional` list and one of its rows is picked (`appendix/engine.md`). Optional stages add work that not every run needs. A template turns one on by listing it. The bundled templates list `map`, `lint-survey` and `lint-recheck`; the others only when you add them.

## The stages

| Stage | Placed after (default) | What it adds | Artifact |
|---|---|---|---|
| `ui-design` | `design` | for `ui-change`: the look of the screens the work adds, from a new direction: theme, page and component mockups, a walkthrough video. For `ui-retouch`: an audit of the AI-looking signs on screens that already exist, the direction taken from the product, and the scope to fix (`## UI retouch`) | `docs/pbi/ui/<slug>/` (a retouch writes `docs/pbi/ui/<slug>/audit.md`) |
| `marketing` | `understand` | who the work is for and how they react: a simulated user panel, objections, positioning, a launch note | `docs/pbi/marketing/<slug>.md` |
| `security` | `review` | a security audit of the changed code: trust boundaries, confirmed vulnerabilities with source evidence, leads that need validation | `docs/pbi/security/<slug>.md` (a summary; the skill keeps its full output where it says) |
| `model-check` | `design` | a checked model of how the system moves between states: the rules the user agrees to, a statechart in the reader's words, the shortest path to each state that breaks a rule | `docs/pbi/model-check/<slug>/` |
| `map` | `understand` | the layers and modules the request touches, from the OMM cache, a scan, or the fallback prompt (`appendix/map.md`) | `docs/pbi/notes/map.md` |
| `lint-survey` | `understand` (after `map`) | jevlint scores, long functions, weak questions and bug-hunt leads in the range (`appendix/lint.md`) | `docs/pbi/notes/survey.md` |
| `lint-recheck` | `verify` | what the change made worse, compared with the merge-base (`appendix/lint.md`) | `docs/pbi/jevlint/recheck.md` |

- The template says where each one runs (`{"stage", "after", "components"}`, `after` any stage but `finish`, so its files are ready for the completion report). Each optional stage appears at most once.
- `ui-design`, `marketing`, `security` and `model-check` need no fallback. When no row is usable or no `when` holds, skip the stage and record why. Like a gate, they run every row that is usable and whose `when` holds, in order. A UI design can take a design skill and then a video skill.
- `map`, `lint-survey` and `lint-recheck` pick like a stage: the first usable row whose `when` holds, and nothing after it. Their last row is a fallback (`flow.py` checks it), so they always run when their `when` holds. Rows placed after the same stage run in the template's order (`map` before `lint-survey`).
- `after verify` means once, when every PBI has passed and the test stage of `appendix/tests-at-end.md` (when on) has ended, before review. It does not run per PBI or per wave.

## when for optional stages

| Value | Holds when |
|---|---|
| `ui-change` | the request adds a screen or a user flow, or gives screens a new look from a new direction. Not when `ui-retouch` holds |
| `ui-retouch` | the request only fixes how screens that already exist look: AI-looking signs, polish, spacing, color, type, copy tone. It adds no screen and no user flow, and keeps the product's features and content |
| `user-facing` | the request adds or changes something end users see or pay for |
| `ask-user` | the user said yes when asked |

`ui-change` and `ui-retouch` are one decision with three answers: `ui-change`, `ui-retouch`, or neither. Both never hold in the same run: a component that makes a new look (design-studio) and one that audits and fixes an existing look (unslop) would set two directions for the same screens. A request that adds screens and also touches up existing ones is `ui-change`; mention the retouch to the design skill as part of the request. Rows with `ui-change` and rows with `ui-retouch` can sit in the same `ui-design` stage, and only the rows of the answer run.

You decide `ui-change`, `ui-retouch` and `user-facing` from the request, like `clearly-complex`. When you cannot tell, end the episode with a question of kind `decision` (inline: AskUserQuestion) before the first row of that stage runs, and save the answer in `docs/pbi/adapters.json` as `{"<stage>": {"decision": "run|skip", "asked": true}}`. For `ui-design`, also save which one held: `{"ui-design": {"decision": "run", "when": "ui-change|ui-retouch", "asked": true}}`; ask it as one question with the three answers. When the design decision (`appendix/design.md`) also needs asking at that point, put both in the same question (kind `decision`). An optional stage placed before design (marketing, by default) is asked about on its own.

`ask-user` never decides on its own: ask every run, once, even when the answer seems obvious. For a stage placed after `route` or later (security, by default), put the question in the PBI list approval (Approve in `designer.md`), so the run does not stop again later. For a stage placed after `plan` or earlier (model-check, by default), ask before its first row with a question of kind `decision` (inline: AskUserQuestion), because the approval comes after route. When the design decision is asked at that point too, put both in the same question (kind `decision`). Only optional stages take `ask-user`; `flow.py` rejects it elsewhere. Save the answer in `docs/pbi/adapters.json` the same way, so a resume does not ask again. With no answer, the stage does not run.

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

## UI retouch

The `ui-retouch` rows (`ui-design.unslop`, heydamir/unslop) audit screens that already exist and decide what to fix. The edits themselves are built as PBIs, like any other change.

- Give the skill the screens the request names: the files that render them, and the route or URL to open in the running app (the dev server, or a page you can screenshot). Ask it for its steps up to the direction only: look at the screen, audit it against its list of signs, decide how much to change (heavy, light, or none), and write the direction. Its passes, its story video and paid image generation are not run here (the component's `forbid` says so); the passes become PBIs.
- The skill stops to show the audit and the direction and waits for an answer. That stop is the approval of the direction: ask it with AskUserQuestion once, together with the design decision when that is asked at the same point. A changed direction is written back before going on.
- Write `docs/pbi/ui/<slug>/audit.md`:
  - **Screens**: each screen, the files that render it, and the screenshot paths (saved in the artifact folder).
  - **Audit**: one line per sign, `<sign> — <where it lives (file:line or the element)>`, numbered `A1`, `A2`, …
  - **Verdict**: heavy, light (polish mode: keep the palette, type, layout and visual) or none, and why in one line.
  - **Direction**: the skill's five lines, as approved.
  - **Scope**: which audit lines are fixed in this run and in which pass (spacing, color, icons, copy, type, layout, details, visuals, motion), with the target values the skill gives (its tokens). Out of scope: the lines left as they are and the bigger ideas the skill suggested, one line each.
- With the verdict none, the stage ends there: put the refinements it suggests in the spec only when the user agrees, and record `run (ui-retouch: no slop)`.
- Copy the skill's credit (CC BY 4.0, Damir Sokolovsky, the repository link) into the top of `docs/pbi/ui/<slug>/audit.md`.
- Keep the audit lines and the scope in your context; leave the screenshots as paths.

## How later stages use it

- **design** (when `marketing` runs before it): read the marketing note's conclusions and put the user-facing decisions in the spec.
- **plan**: a PBI that builds a screen links its mockup and theme files in its Notes, so its worker builds to them. The acceptance criteria name the mockup when the screen must match it.
- **plan** (after a `ui-retouch` row): split the scope by pass and by screen, keeping the skill's order (spacing before color, copy before type and layout, visuals and motion last), and make a later pass depend on the earlier one when they touch the same files. Each PBI lists the audit lines it fixes (`A3`, `A7`) in its acceptance criteria and links `docs/pbi/ui/<slug>/audit.md` in its Notes, with the direction and the target values. Out-of-scope lines stay out of every PBI.
- **plan** (after `model-check`): the PBI that owns a rule gets an acceptance criterion for it, and each broken rule's path becomes a test that replays it step by step against the real code. The PBI links the chart in its Notes.
- **review**: the reviewer compares the built screens with the mockups when the PBI links them.
- **review** (after a `ui-retouch` row): render the changed screens again and check each audit line in scope is gone and no new sign from the skill's list appeared. A line still there sends its PBI back like a review finding.
- **fix** (after `security`): a confirmed finding in a file a PBI changed sends that PBI back like a review finding (it counts toward the rework limit). A confirmed finding outside the PBIs' files, and every lead that needs validation, goes to the completion report under findings; do not fix them in this run.
- **Approve**: show the model-check chart (`chart.mmd` as a `mermaid` block) and its rules with the PBI list, so a reader who does not read code can check the behavior.
- **finish**: the completion report lists the artifacts under the changes, with paths.

## Records

Write a stage record for each row, as for any stage: `stage ui-design after design: ui-design.design-studio run (ui-change: new settings page)`, `stage ui-design after design: ui-design.unslop run (ui-retouch: restyle the top page)`. A row whose `when` is the other half of the pair gets `skip (ui-retouch holds)` or `skip (ui-change holds)`. An optional stage that did not run gets one line: `stage marketing after understand: skip (user-facing: internal refactor)`.
