# Approve: writing the approval draft

The approval draft (`docs/pbi/notes/approve.md`) is the page the user reads to approve the run. Write it for someone who knows the goal but not Atelier's insides: what changes, what they must decide, and what each PBI does. The example is `examples/approve.md`; write yours in the same shape, in the language of the user's goal.

## The seven sections

Use these headings, in this order, and no other `##` heading. English drafts use the second name.

| # | Heading | English | What goes in it |
|---|---|---|---|
| 1 | `## 結論` | `## Summary` | What this run changes, in two or three sentences (a `callout`) |
| 2 | `## 今の流れ` | `## Before` | The flow as the user sees it today, as a diagram |
| 3 | `## この run のあとの流れ` | `## After` | The same flow after the run, with change marks and PBI numbers |
| 4 | `## 決めること` | `## Decisions` | Every `ask` of the run. With nothing to decide, the one line `なし` (`None`) |
| 5 | `## PBI` | `## PBIs` | One table: what each PBI does and what works after it |
| 6 | `## 影響範囲` | `## Impact` | What the blast-radius check found, as a diagram, and how each risk is guarded |
| 7 | `## 作る人と見直し` | `## Who builds` | Which model builds each PBI and how deep its review is; the tests at the end |

Your stage records (the component picked per stage, skips, fallbacks) stay in the design record and `docs/pbi/notes/`. They never go in the draft.

## Diagrams

- Draw `## 今の流れ` and `## この run のあとの流れ` with an am component: `flow` for a flow, `tree` when the run changes only files (a docs fix), `er` or `sequence` when that is the shape.
- Draw what the user sees happen (the user asks, a page opens, a reply is checked), not modules or functions. Name nodes and edges in the user's words.
- The After diagram starts from the Before lines. Mark each line at its start: `- ` for a line that goes away, `+ ` for a new one, `~ ` for a changed node. The mark belongs to the line only; refer to the node by its plain name everywhere else. Put the PBI number in the node it changes: `+ ページで見せる -> どの返事も確かめる (PBI-003)`. am then shows the diagram with a before / diff / after switch.
- When one PBI changes a flow the overall diagram cannot hold, add that PBI's own small before and after under `## PBI`.

## Decisions

- Ask only what changes what the user gets, what it costs, how long it takes or what can go wrong. Decide the insides yourself (how the caller passes an answer, the shape of a command) and write the decision in the spec.
- Every option carries `| what changes when you pick it`. Put the one you would pick first, with `*`.
- When a question needs an internal term, explain it in the same sentence.
- These go here, each as one `ask`: the open points from the design stage (`appendix/design.md`, `## Design: unknowns`), whether to run an optional stage placed after route (`appendix/optional.md`), each `Survey fix:` lead from the lint survey (`appendix/lint.md`), installing a missing tool with its fallback (`tools_check.py`), and a priority template or quality warning only when the user has something to decide about it.

## PBIs

The table header is fixed: `| PBI | 何をするか | 終わると何ができるか | 先に要る PBI |` (`| PBI | What it does | What works after | Needs |`). Write what the PBI does and what the user can do once it is done. Tier, route, review level and scores stay out of this table.

## Impact

- Draw each finding of the blast-radius check as `the part that changes -> the part that relies on it`, with what can break as the edge label; emphasize (`*`) the risky ones. Name nodes in the user's words; list file names under the diagram only when the user needs them.
- Under the diagram, one line per finding: how it is guarded (a test, a name that is kept).
- Then "見つけたが、この run ではしないこと" (found, not in this run): the lint survey's leads outside the range.
- When the check did not run, one line with the reason is enough.

## Who builds

One table `| PBI | 作るモデル | 見直しの深さ |` (`| PBI | Model | Review |`), the depth in words (ふつう / 深く). Then the tests: one table `| PBI | 関連する E2E |` with the `related_specs` of each PBI (`なし` when none), the test rules you took from the project (`appendix/tests-at-end.md`, `## Project test rules`: the full-run command, the one-spec command, the limits, and one line per point where the project's rule wins over Atelier's default), and that the full suite runs only when the user says yes at the end. The model-check chart when that stage ran. Scores and template names stay out.

## Check the draft

Run `python <skill>/design_record.py markdown --draft docs/pbi/notes/approve.md` as the last change before you end the episode (with `confirm` `off`, `design_record.py lint --draft` instead). `markdown` runs the lint first and adds the `reply-meta-` lines; a draft without them stops `answer --approve`. Exit 6 lists each error as `✗ L<line>` with an `例:` line: fix every one and run it again. A `! L<line> <term> → <rewording>` line is an internal term with no explanation on that line: reword it, add the explanation in parentheses after it, or keep it when the user knows the word. `design_record.py markdown` runs the same check first and adds no reply check info to a draft with errors. For a Japanese draft (frontmatter `lang: ja`, or Japanese text in its body), the same check also runs yomiyasu, and each candidate prints as `! L<line> yomiyasu <rule> → <candidate>`. These warnings never stop the run or change the exit code: decide from the context whether to reword the line, since the check does not rewrite it. When yomiyasu is not found, times out or fails, the check prints one line, `! yomiyasu skipped: <reason> (tools_check.py)`, and the other checks go on.
