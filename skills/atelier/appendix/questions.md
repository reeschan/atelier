# Questions: writing a question draft

A question draft is the page the user reads to answer any question except an approval: a decision (including one grill round), `send-approval`, `rework-limit` (including the stale case), `destructive`, `full-run`, and the conductor's own questions. It lives at the `question.path` the conductor gave you. Write it for someone who knows the goal but not Atelier's insides: what to decide, and what changes with each answer. The two examples are `examples/question-send-approval.md` (a question that moves the run on, with edge labels) and `examples/question-grill.md` (a design question, with change marks). Write yours in the same shape, in the language of the user's goal. An approval keeps its own shape (`appendix/approve.md`); an inline question has no file and follows `designer.md`.

## The six sections

Use these headings, in this order, and no other `##` heading. English drafts use the second name. Leave out an optional section you do not need; the order of the rest stays.

| # | Heading | English | Needed | What goes in it |
|---|---|---|---|---|
| 1 | `## 結論` | `## Summary` | yes | What is to be decided and what changes with the answer, in two or three sentences (a `callout`) |
| 2 | `## 今の流れ` | `## Before` | yes | A design question: today's behavior. A question that moves the run on: the flow so far and where it stopped |
| 3 | `## 答えたあとの流れ` | `## After` | yes | A design question: the flow with the recommended answer, with change marks. A question that moves the run on: one branch per option, with edge labels |
| 4 | `## 決めること` | `## Decisions` | yes | One or more `ask` blocks |
| 5 | `## 判断の材料` | `## Details` | no | Tables, numbers, findings. The table headings are free |
| 6 | `## 影響範囲` | `## Impact` | no | What the answer changes and what relies on it |

`## PBI` and `## 作る人と見直し` (and their English names) belong to the approval draft; on a question they are errors.

## The two diagrams

Draw both with an am component: `flow` for a flow, `tree`, `er` or `sequence` when that is the shape. Name nodes and edges in the user's words, never function names.

- Design question: Before is today's behavior. After starts from the Before lines and marks each line at its start: `- ` for a line that goes away, `+ ` for a new one, `~ ` for a changed node. The mark belongs to the line only. The After diagram shows the flow with the answer you recommend.
- Question that moves the run on: Before is the flow so far, ending at the place it stopped (emphasize the stop with `*`). After is the fork from that place: one edge per option, each with the option as its edge label (`{送るか} -> 手元の点検だけを走らせる: AST だけ`).
- The check accepts either mark lines or edge labels in After; it needs at least one of them.

## What goes in each kind

| Kind | Summary says | Before shows | After shows | Details (optional) |
|---|---|---|---|---|
| decision (one grill round) | How many points this round decides, and what happens if all the recommended answers stand | Today's behavior | The flow when every recommended answer passes (change marks) | A note per Q |
| decision (end of grill) | Asks whether the understanding is shared; the number of points decided | Today's behavior | A summary of everything decided so far (change marks) | A table of the decisions |
| decision (design choice, ask-user, map, optional) | What choosing changes | Today's behavior | The flow with the recommended answer (change marks) | A comparison table |
| decision (install a tool?) | Install it, or go on with the fallback | The flow so far and where it stopped | Install / go on with the fallback (edge labels) | How to install, and the fallback |
| decision (rework candidates from the lint recheck, `appendix/lint.md`) | How many candidates the recheck confirmed, and that each one is reworked or only reported | The flow so far and where it stopped (the recheck, before the final review) | Rework (back to build, counted toward the limit) / report only (on to the final review) (edge labels) | Needed: each candidate with its PBI, file and claim |
| `send-approval` | Which stage sends how many files to which provider, the expected cost, and what happens with AST only (the local measures still run) | The flow so far and where it stopped | Send / AST only (edge labels) | Needed: the dry-run numbers (files, tokens, cost, provider) and the list of files sent (`<name>.send.txt`) |
| `rework-limit` | Which PBI reached the rework limit, on which model, and what to do | The flow so far and where it stopped | Raise the tier and run again / rewrite or split the PBI / back to design / the user takes it over (edge labels; the four choices of `appendix/rework.md`, `## Stop`) | Needed: what failed on each attempt, and the likely cause (the PBI said too little, the model fell short, or the design is wrong) |
| `rework-limit` (stale: a target file changed after the design) | A target file changed after the design; which PBIs to redesign | Design, approval, work, and the changed files | The PBIs to redo and the others (edge labels) | The changed files and the version at design time |
| `destructive` | What is deleted or overwritten, and whether it can be undone | The flow so far and where it stopped | Do it / do not (edge labels) | The list of targets |
| decision (minors of the final review, `appendix/review.md`) | How many confirmed minors are left, how many are recommended to fix, and that the fixes run in this run without another final review | The flow so far (the final review done, no critical or major left) | Fix (back to build for the owning PBIs) / skip (into the report) (edge labels) | Needed: one row per minor with file, line, what is wrong, owning PBI and the recommendation; one `ask` per minor, the recommended choice first with `*` |
| `full-run` (a run started before the full suite moved to the approval) | Whether to run the whole E2E suite once, and how long it takes | The flow so far and where it stopped | Run / do not run (edge labels) | The number of specs and the expected time |
| conductor questions (`no-agent-tool`, `handoff-loop`, `launch-failure`) | What happened and why it stopped | The flow so far and where it stopped | One branch per option (edge labels) | None |

- Details is optional, except where its cell says Needed: a send approval costs money and cannot be taken back, and a rework limit cannot be answered without knowing what happened. The check looks at the shape of the sections only, so this table and the examples are how you learn it.
- One grill round is one page. Draw one pair of diagrams for the whole round, and put one `ask` per Q, with the recommended answer first (`*`).
- The conductor writes its own questions itself, in fixed English sentences with the same sections (the conductor does not know the user's language); you do not write them. They pass the same check.

## Decisions

- Every `ask` sits inside `## 決めること`, never above or below it.
- Every option carries `| what changes when you pick it`. Put the one you would pick first, with `*`.
- `なし` (`None`) cannot be written: a question with no `ask` is not a question, and the check refuses it.
- When a question needs an internal term (`grill`, `stale`, a kind name), explain it on the same line, in parentheses right after it. Without the explanation the line gets a `!` warning.
- Ask only what changes what the user gets, what it costs, how long it takes or what can go wrong; decide the insides yourself.

## Check the draft

Run `python <skill>/design_record.py lint --draft <file> --kind <kind>` as the last change before you end the episode. `<kind>` is `decision`, `send-approval`, `rework-limit`, `destructive`, `full-run`, `handoff-loop` or `launch-failure`; without `--kind` the draft is checked as an approval. The check looks at the shape of the sections only, so the table above and the examples are how you learn what each kind carries. Read the output like this:

- `✗ L<line> <what is wrong>` followed by `  例: <the fixed form>` is an error. All errors are printed together and the exit code is 6. Fix every one and run it again.
- `! L<line> <term> → <rewording>` is a warning for an internal term with no explanation on that line. The exit code stays 0. Reword it, add the explanation in parentheses, or keep it when the user knows the word.
- `! L<line> yomiyasu <rule id> → <candidate>` is a candidate from the Japanese-writing check (phrasing that reads oddly). It never stops you and nothing is rewritten for you: judge it in context and fix the sentence or keep it. Lines inside a diagram fence are not checked.
- `! yomiyasu skipped: <reason>` (one line) means the tool is not installed or did not answer. The other checks still ran; `tools_check.py` shows how to install it.

`done` runs the same check on the draft at `question.path` for every kind except an approval. If the draft has errors, `done` refuses the result: it prints `state: retry` and an `error:` line that names the draft and its first errors (`L<n> …`), not the `✗` lines. The caller then runs `next`, which starts the same episode again without `Continue from:`; after two refusals in a row the conductor asks a `launch-failure` question instead. So run `lint --kind` yourself and fix every `✗` before you end the episode with the question. Warnings never stop it.
