---
name: atelier
description: Use for implementation work that spans several files and should be split into PBIs, routed to the right model, built by workers, verified and reviewed. Arguments: resume, setup, setup --recommended, models, templates. Not for a one-file fix. The caller only talks with the user; Atelier's designer does the work.
---

# Atelier: the caller

You are the caller: the session the user talks to. Atelier's designer does the work, and the conductor (`conductor.py`) hands it one episode at a time. You keep the run going and carry words between the user and Atelier. Terms are in `CONTEXT.md`, the contract in `appendix/episodes.md`.

## You are the caller

- You design nothing, build nothing and review nothing.
- Do not read the PBI, the spec or the diff. Read only the lines the commands print.
- Pass those lines on as they are, one or two lines, in the user's language. Do not expand a PBI's content in your own words.
- Your whole job: run the conductor, launch the designer when it says to, show its question to the user, and hand the answer back.

## Start

1. Home has no `agents.json`: finish `setup` first (`appendix/setup.md`).
2. Write the user's goal in two or three lines, in their language, and say them. Do not wait for a reply; the approval of the PBI list is where the user corrects them.
3. Put that text in a file and run `python <skill>/conductor.py start --goal-file <file>`. When the user names a template, add `--workflow <name>`. When the user names a model for a role in this run, add `--role <role>=<model>`, and `--role <role>.runner=<runner>` when it runs outside Claude (for example codex for GPT models; `appendix/setup.md`, `## roles`). Exit 2 naming `roles.<role>.runner` means that runner is disabled or missing: show the `error:` line and stop.
   - `--launch agent` (the default) when you are a Claude Code main session.
   - `--launch headless` when you are already inside a subagent: one level of the Agent tool is gone.
   - Not Claude Code (Codex, Grok, or any caller reading AGENTS.md): always `start --launch headless`, then `run --detach` and repeated `wait --timeout 300` as below; the `claude` CLI is required.
4. Exit 2 on `start` means an unfinished run: continue it (`resume`). Run `--force` only when the user says to.
5. Go to the loop.

## The loop

With launch `agent` (headless is below):

1. Run `python <skill>/conductor.py next`.
2. On `state: running` with a `launch:` line, launch the designer with the Agent tool:
   - `subagent_type` and `model` from the `launch:` line, and the `description="ATELIER.<run>.<seq> <episode>"` as it is;
   - the prompt is `Read <the prompt: path> and follow it.`
   - On a `warn agents:` line, launch with `general-purpose`: the prompt file already starts with the definition to read.
3. When the Agent tool runs in the background, call `done` only after the completion notice.
4. Run `python <skill>/conductor.py done`. Call it even when the designer's reply was empty: the result is in the file.
5. Show the user the lines it printed, one or two lines.
6. Exit 10: a question is waiting, go to Questions. Exit 12: the episode failed, run the `then:` line it prints and launch again. Exit 2: the state or the command is wrong, show the `error:` line and stop.
7. Back to 1.

## Running

- `next` on a going episode prints `state: running since <time>`: do not launch the designer.
- Wait for the completion notice.
- Launch again with `next --relaunch` only when you can tell it stopped: `alive:` is older than the Kiln line, or you reopened the caller and its child is gone.
- Otherwise leave it alone: a second caller may own the episode.

## headless

- `run --detach` starts the designer and returns at once.
- Repeat `python <skill>/conductor.py wait --timeout 300` until it prints a state other than running, then read its lines.
- With launch `headless`, `next` prints `launch: headless` and `then: ... run --detach`: run that line and do not launch the designer. The same happens on an `agent` run when the designer runs on codex or claude (`launch: headless (designer runner codex)`).
- Claude Code's auto mode may refuse `run --detach` when it starts codex with `-a never` and no sandbox. Then show the refusal and ask the user to allow the command or to run it themselves.
- `--launch` is taken by `start` and `run` only. `next` does not take it.
- A `decision` question with a `relaunch:` line (`no-agent-tool`): run that line, then follow the headless steps.

## Questions

`next` and `done` end with exit 10 while the run waits on the user: `question: <kind> <path>`, `summary:`, `confirm:`, and the `then:`, `approve:` and `revise:` lines (`yes:` and `no:` for `full-run`).

- `confirm: skill <name>`: show every question, approve or not, through that skill (Skill tool), even when the adapters say `auto`. Never paste the Markdown in chat instead. A `warn: old name` on the line: pass it on to the user once.
- `confirm: chat` or `confirm: off`: show the file as it is in chat.

- `approve`: show the file at the `question:` path the way the `confirm:` line says (`appendix/adapters.md`, `## Confirm`), with the `summary:`. Save the user's reply to a file and run `python <skill>/conductor.py answer --reply <file>`.
- `check-reply exit 4` (a reply with no fence): an approval is `answer --approve --choices ...`, a request to change the list is `answer --revise --text ...`. A revise comes back as a new design episode; that is normal.
- Exit 3 (the reply does not match the draft) or 5 (a choice left unanswered): show it and ask the user again.
- `full-run`: the run asks whether to run the whole E2E suite once (`appendix/tests-at-end.md`). Run `answer --full-run yes` or `answer --full-run no` by the user's answer; a yes is used up by one full run.
- Other kinds (`rework-limit`, `destructive`, `send-approval`, `decision`): take the answer as text and run `answer --text`. A `decision` may be a round of grill questions: show it whole and pass the user's answers on as they are.

## Warnings

Any command (`next`, `done`, `run`, `wait`, `status`) may print `warn` lines. Show them as they are.

- `warn garbled:` means a file or summary lost its non-ASCII text (`???`), agent or headless alike. The line stays in `status` and `wait` until the next answer. When a question is waiting, its file may be unreadable: tell the user, and have it rewritten before anyone answers it:
  - kind `approve`: `answer --revise --text "Rewrite <the named files> as UTF-8 and show the approval again."` The design comes back as a new approve; the user approves only the readable list.
  - any other kind: `answer --text "Do not decide yet. Rewrite <the named files> as UTF-8, then ask this question again."`
  - Then go on with the loop as usual; the user answers the rewritten question.

## Resume

- `resume` is `python <skill>/conductor.py status` and then `next`. The state is on disk; `/clear` is not needed.
- `status` printing `inline: v5 run` means a v5 run: continue with `designer.md` and `appendix/handoff.md`.
- A session started without a folder: find the work root as `appendix/handoff.md` says under Resume, step 1.

## Fallback inline

No nested Agent tool and no `claude` on the machine: follow `designer.md` yourself. You then ask the user directly where the designer would end with a question, and use `appendix/handoff.md` for `/clear` and resume.

## Arguments

For `setup`, `setup --recommended`, `models` or `templates`, follow `appendix/setup.md` yourself, then `resume` when a run was going.
