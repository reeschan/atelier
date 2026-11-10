# Prompt: one designer episode

`conductor.py next` fills the block below and writes it to `docs/pbi/conductor/episodes/<seq>-<episode>.prompt.md`. The caller gives that file to an `atelier-designer` subagent (`agents/atelier-designer.md`), or `conductor.py run` gives it to `claude -p`. A line that starts with `{?name}` is kept only when `name` has a value. Edit the wording here, not in the script; keep every placeholder.

```text
Work root: {work_root}. Work here and write nothing outside it.
You are the designer of Atelier (called the orchestrator in its appendices). Read {skill}/designer.md first and follow it. Skill directory: {skill}
Episode: {episode} (seq {seq})
{?wave}Wave: {wave}
Goal: {goal}
Workflow: {workflow}
Roles: reviewer {reviewer}; tester {tester}
State: docs/pbi/STATUS.md and docs/pbi/conductor/run.json (read only the parts you need)
{?continue_from}Continue from: {continue_from} (a state tablet: go on from its next action, never redo finished steps)
{?answer}Answer: {answer} (the user's answer to your last question: read it first)
Result: as your last action write {result} (schema atelier.episode/1, appendix/episodes.md), then reply with its summary lines and nothing else.
```
