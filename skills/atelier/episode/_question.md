# Questions: the draft

Every question but `approve` is a draft at the `question.path` of your result, written for someone who knows the goal but not Atelier's insides, in the language of the user's goal. The full form is `appendix/questions.md`; read it, with its row in `## What goes in each kind`, when you write a question. An approval keeps its own form (`episode/approve.md`).

## The six sections

These headings, in this order, and no other `##` heading (English drafts use the second name). Leave out an optional one you do not need.

1. `## 結論` / `## Summary` (needed): what is to be decided and what changes with the answer.
2. `## 今の流れ` / `## Before` (needed): today's behavior, or the flow so far and where it stopped.
3. `## 答えたあとの流れ` / `## After` (needed): the flow with the recommended answer, with change marks, or one branch per option, with edge labels.
4. `## 決めること` / `## Decisions` (needed): one or more `ask` blocks; every option says what changes when it is picked.
5. `## 判断の材料` / `## Details` (optional): tables, numbers, findings.
6. `## 影響範囲` / `## Impact` (optional): what the answer changes and what relies on it.

## Check the draft

As the last change before you end the episode, run `python <skill>/design_record.py lint --draft <path> --kind <kind>` and fix every `✗` line; `done` runs the same check and refuses a draft with errors (`appendix/questions.md`, `## Check the draft`).
