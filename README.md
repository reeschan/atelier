# pbi-orchestration-jev

English | [日本語](README.ja.md)

A [Claude Code](https://claude.com/claude-code) skill that splits multi-file implementation work into **PBIs (Product Backlog Items) and hands them to sub-agents**. The main session acts as the orchestrator: it writes the PBIs, picks a model for each, reviews the results and sends work back. The sub-agents (the opencode CLI with DeepSeek, Codex CLI, Claude via the Antigravity CLI, DeepSeek Harness (`dsh`), or Claude Code's Agent tool) do the implementation.

Some judgment calls along the way go to **Jev** ([TypeSafe](https://api.typesafe.ai)), a judgment API, as short text. Pass/fail is still decided by actually running verification commands, and counts are kept in a status file.

> The skill text itself (`SKILL.md`) is written in Japanese. Claude reads it fine either way.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/architecture-dark.svg">
  <img src="assets/architecture-light.svg" alt="pbi-orchestration-jev architecture. The developer gives the Claude Code orchestrator a task, approves it and resumes it after a hand-off. The orchestrator passes facts to prepare.py, which asks Jev for gate 1 (brief and PBI quality), picks a tier and a route with select_model.py, reads the routing, overrides and agents.json from your home (~/.config/pbi-orchestration-jev) and docs/pbi/routing.json, and drops steps whose runner is unusable. PBIs go to pbi-worker (the Agent tool), the default runner for every tier, or to external runners (opencode, Codex, Antigravity, DeepSeek Harness) enabled in routing.json; they change the source code of the target repository. The orchestrator verifies each PBI with verify.py and unchanged.py, asks Jev in plain text only when a report and the verification disagree and to check rework notes, and records status and HANDOFF in docs/pbi/. An optional shared-memory MCP holds per-task refs and the hand-off memory; pbi-worker can only get refs. Template Studio, with models from OpenRouter and Artificial Analysis, writes the try order to docs/pbi/routing.json, and the priority templates web page (a claude.ai artifact) syncs with templates.py, which keeps templates and overrides in your home.">
</picture>

The diagram source is `assets/architecture.d2` (rendered with [D2](https://d2lang.com)).

## Highlights

- **Models are chosen by a decision table, not by gut feeling.** `select_model.py` combines facts the orchestrator counts (files changed, modules touched, sensitivity) with Jev's reading of a short brief (novelty, coupling, ambiguity) and picks a tier: haiku, sonnet or opus.
- **Runners are tried in order.** Each tier comes with a `route`, an ordered list of runners. If one is out of quota or fails, the next one takes over.
- **Pass/fail comes from verification commands.** Neither the sub-agent's report nor Jev's answer is taken at face value. The worker starts its report with a status line (`DONE` / `DONE_WITH_CONCERNS` / `NEEDS_CONTEXT` / `BLOCKED`); the orchestrator sorts the report by that status and the verification results, and asks Jev to triage only when the two disagree. The orchestrator runs the commands itself and checks each acceptance criterion.
- **At most two rounds of rework, and an optional tier up.** A failed PBI goes back with the review notes, twice at most. The orchestrator can raise a PBI's tier (for example haiku to sonnet) with `prepare.py --only PBI-<n> --tier <tier>`, which rewrites that PBI's route from the routing in effect at that moment. If the design needs rethinking, or a PBI still fails after two rounds, the orchestrator stops and asks the user.
- **Only prose goes to Jev.** PBI text, briefs, excerpts from reports and rework notes. Never source code, diffs, environment variables or credentials.
- **The orchestrator's context stays small.** Every token in the orchestrator's context is re-read on every later turn. Workers end with a 5-line report that starts with a status line and put the details in a file, long command output goes to log files, and PBI text is written once and passed by path (`worker.md`, section 0.7 of `SKILL.md`).
- **Hand-off when the context grows.** Between waves of PBIs the orchestrator measures its context with `agent_stats.py --ctx`. Above 250,000 tokens it writes `docs/pbi/HANDOFF.md` (plus a hand-off memory when shared memory is available) and asks you to `/clear` and resume with `/pbi-orchestration-jev resume`. From a session started without a project folder, run `/pbi-orchestration-jev resume <repository path>`; without the path, the skill finds the repository from the hand-off memory.
- **Opus designs and reviews; cheaper models implement.** Only PBIs that need design decisions are implemented by Opus. Sensitive or tightly coupled PBIs are implemented by Sonnet and get a deep review from the Opus orchestrator.
- **Optional output compression with [rtk](https://github.com/rtk-ai/rtk).** A bundled hook rewrites Bash / PowerShell commands to `rtk <cmd>` only in sessions where the skill switched it on.
- **Mechanical steps run as scripts.** `prepare.py` runs Jev gate 1 (brief and PBI quality) plus the decision table in one call, and `verify.py` runs a PBI's verification commands; both return only a table to the orchestrator. Files a PBI must not touch are checked by `unchanged.py`, which hashes them before the worker starts. The skill body is split into a core and `appendix/` files that are read only when needed. In the author's measurements (2026-10, 6 tasks) these two changes cut cost by 15%.
- **Workers get just what they need (optional).** With a shared-memory MCP (`mcp__shared-memory__*`), only the orchestrator writes per-task "refs" (one fact each) and lists their ids in the PBI. The `pbi-worker` sub-agent can only `get` memories, never search or write.
- **CLAUDE.md first.** Stable repository knowledge (conventions, commands, pitfalls) is expected to live in layered CLAUDE.md files; the orchestrator searches memory only as a supplement.
- **Pick the try order in a browser.** Template Studio turns the models you have into performance, cost and balanced proposals; reorder and press Apply to write `docs/pbi/routing.json` (see Template Studio below).
- **Override routing for a while.** Turn on a priority template and it replaces per-tier routes until its deadline, then asks whether to continue. You can also set it from a web page (an artifact); see Priority templates below.
- **Usage stats.** `agent_stats.py` reads Claude Code transcripts and reports tokens per model and per agent, how often the prompt cache had to be rebuilt, and the current context size.
- **A record per run.** Each time the orchestrator decides a run (pass, rework, promotion, unavailable, on hold), `record_run.py` appends one line to `docs/pbi/runs.jsonl`: runner, model, effort, tokens, cost, steps, time and verification results are read from the logs, and only the outcome and the review finding counts come from the orchestrator. Values it cannot read stay null. It also copies that run's raw logs to an archive outside the repository: `$PBI_RUNS_ARCHIVE` if set (point it at a synced folder to keep logs off the machine), otherwise `~/.claude/pbi-runs`. `model_report.py` sums the runs per model, across repositories with `--archive`, and adds a provisional A–D grade for people to adjust later; it never changes routing.
- **Costs are estimates.** The bundled `prices.json` is a sample price list; a `prices.json` in your home overrides and extends it. `agent_stats.py --cost` reports the designer's cost in API-equivalent USD, and `model_report.py` reports how many runs have no known cost (it leaves them out of the sums).

## Flow

```mermaid
flowchart TD
  A[Design and split into PBIs] --> B[Count facts<br/>facts.json]
  B --> C[prepare.py<br/>Jev: judge the brief and PBI quality<br/>select_model.py: tier and route]
  C --> F{User approval}
  F --> G[Run a wave along the route]
  G --> V[verify.py runs the verification commands]
  V --> H{Worker status and verify.py results agree?}
  H -->|no| H2[Jev: triage the report]
  H -->|yes| I
  H2 --> I[Review<br/>acceptance criteria and the diff]
  I -->|fail| K{Reworks left?}
  K -->|yes| G
  K -->|no| L[Stop and ask the user]
  I -->|pass| M{Context ≥ 250k?}
  M -->|no, PBIs left| G
  M -->|yes| N[HANDOFF.md, hand-off memory<br/>/clear, resume]
  N --> G
  M -->|all done| J[Final report and stats]
```

## Routing

| Condition (evaluated top to bottom) | Tier | Review |
|---|---|---|
| `novelty` = 2 and `ambiguity` = 2 (design decisions needed) | opus | normal |
| `sensitive` is true | sonnet (Agent tool only) | deep |
| `coupling` = 2 | sonnet | deep |
| any score ≥ 1, or files changed ≥ 4, or modules ≥ 2 | sonnet | normal |
| none of the above | haiku | normal |

The bundled default routes every tier to the Agent tool: `haiku` at effort `max`, `sonnet` at effort `high`, and `opus` at its default effort. The default lives in `routing.default.json`; `select_model.py` keeps no table of its own. To use external runners, write your own routing template in `routing.json` in `docs/pbi/` of the target repository or in your home (see [Priority templates](#priority-templates)). The bundled example is `skills/pbi-orchestration-jev/examples/routing/multi-runner.json`.

After the route is built (from `routing.json` or a priority template), the skill checks each step against `agents.json` in your home and against the CLIs it can find. It drops a step whose runner is disabled there, whose CLI is missing, or whose model is not in that runner's model list, and the approval list shows each dropped step with its reason. The last Agent step is never dropped.

A minimal `agents.json` in your home:

```json
{"runners": {"opencode": {"enabled": true, "models": ["opencode-go/deepseek-v4.1-flash"]}}}
```

- A Jev score with confidence below 0.7 is lowered by one step. Without Jev, the tier is decided from the facts alone: files changed, modules and sensitivity.
- Antigravity offers no Haiku, so a route that sends the haiku tier to `agy` has to use Sonnet at low effort.
- `--skip opencode,agy` drops runners that hit a quota or got denied in this session. `--all docs/pbi` routes every PBI at once and prints one line per PBI.
- "deep" review means the Opus orchestrator reads the whole diff itself. A normal review over 300 lines is delegated to an Opus reviewer agent.

## Template Studio

The "which runner and model, in what order" part of routing (the bundled all-agent default, or your own `routing.json` such as `examples/routing/multi-runner.json`) can be picked in a browser. The decision table that sets the tier does not change.

```
/pbi-orchestration-jev templates
```

Run this in Claude Code and the skill starts a small server on 127.0.0.1 and opens the page. To open it without the skill:

```sh
python ~/.claude/skills/pbi-orchestration-jev/studio/serve.py --target <target repo root>
```

1. **Pick models**: keep only the runner chips you have on each provider card.
2. **Pick a proposal**: the page builds performance, cost and balanced proposals from those models. You can set a quality floor per workload (light / standard / design-heavy).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/studio-proposals-en-dark.png">
  <img src="assets/studio-proposals-en-light.png" alt="Template Studio: pick models on provider cards, then choose one of three proposals." width="800">
</picture>

3. **Adjust the order**: for each work class, drag or use the arrow buttons to reorder, and untick what you do not want. Pick the Claude Code model that catches the rest.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/studio-order-en-dark.png">
  <img src="assets/studio-order-en-light.png" alt="Adjusting the try order per work class by reordering and unticking models." width="800">
</picture>

4. **Apply**: the Apply button writes `docs/pbi/routing.json` and the server stops. The previous file is kept as `routing.prev.json`. From then on `prepare.py` tries runners in that order.

- The page is in English or Japanese. It follows your browser language, and the 日本語 / EN button in the header switches it (the choice is remembered). `?lang=en` or `?lang=ja` in the URL also works.
- Refresh the model list (`studio/catalog.json`) with `python studio/update_catalog.py`. Prices and new models come from OpenRouter; quality is the Artificial Analysis coding index (when `AA_API_KEY` is set). Until then the values are placeholders, and the page says so.
- The server only writes `docs/pbi/routing.json`, and only when the per-run token, the Host header and the template shape all check out.
- Design notes (Japanese): [docs/template-studio.md](docs/template-studio.md).

## Priority templates

A **priority template** overrides the routing for a while. Template Studio sets the usual routing (`routing.json`); a priority template sits on top of it. Save per-tier routes under a name (the same shape as `routing.json`), turn the template on with a deadline, and until that deadline the overridden tiers take their route from the template instead of the usual one; tiers without an override are unaffected, and the last fallback is still that tier's Claude Code agent from `routing.json`. When the deadline passes, your choice of expiry behaviour decides the rest: with `ask`, the skill asks whether to continue (extend, stop, or make it indefinite); with `drop`, it quietly stops applying the template. The bundled web page (an artifact with its own database) shows the current application, the templates and the history, and starts, extends and stops applications from anywhere, phone included.

Two things a template cannot change:

- `sensitive` PBIs never go to an external runner, whatever the template says.
- Design reassessment and review duties stay with the decision table.

Templates are personal settings and live in your home. The default home is `~/.config/pbi-orchestration-jev` (`$XDG_CONFIG_HOME/pbi-orchestration-jev` when `XDG_CONFIG_HOME` is set), and `PBI_HOME` points it somewhere else. The home holds `routing.json` (try order), `templates/<name>.json` (priority templates), the synced copy `overrides.json`, the web page URL `web.json`, `agents.json` (runner and model choices) and `prices.json` (price overrides). None of them is required, and the skill does not create the directory. To try the bundled example, copy it into `templates/` there:

```sh
mkdir -p ~/.config/pbi-orchestration-jev/templates
cp skills/pbi-orchestration-jev/examples/templates/cheap-all-tiers.json ~/.config/pbi-orchestration-jev/templates/
```

Replace `~/.config/pbi-orchestration-jev` with your home if you set `XDG_CONFIG_HOME` or `PBI_HOME`.

The example routes every tier to `opencode-go/deepseek-v4.1-flash` at effort `max`. A step in a priority template can also be a rotate group, `{"rotate": [step, step, ...]}`: the PBI number picks which member goes first, and the other members follow as that PBI's fallbacks, so PBIs are spread evenly across the members (for example, alternating between two free-tier models before two paid ones). For the file format, the home rules and the full walkthrough, see `skills/pbi-orchestration-jev/appendix/priority-templates.md`.

## Requirements

| Tool | Required | Used for |
|---|---|---|
| [Claude Code](https://claude.com/claude-code) | yes | the orchestrator and the Agent tool |
| Python 3.10+ | yes | scripts such as `prepare.py`, `select_model.py` and `verify.py`, and the Jev client |
| Jev API key | recommended | without it, Jev gates are skipped and the tier is decided from the facts alone |
| shared-memory MCP (`mcp__shared-memory__*`) | no | per-task refs and the hand-off memory; without it, facts go into the PBI notes |
| [opencode](https://opencode.ai) + OpenCode Go | no | external runner you enable in `routing.json` (see `examples/routing/multi-runner.json`) |
| [Codex CLI](https://github.com/openai/codex) | no | external runner you enable in `routing.json` |
| Antigravity CLI (`agy`) | no | Claude via a Google AI subscription |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) | no | external runner for `routing.json` or a priority template, e.g. the official DeepSeek API through dsh headless |
| Node.js | no | the rtk hook, and the JS Jev client (`jev.mjs`) |
| [rtk](https://github.com/rtk-ai/rtk) | no | compressing command output |

Only Claude Code is required. Jev, shared-memory, the external CLIs, the web pages and rtk are optional. A missing runner just means the route moves on; Claude Code alone is enough to run the skill.

## Install

```sh
git clone https://github.com/reeschan/pbi-orchestration-jev.git
cp -r pbi-orchestration-jev/skills/pbi-orchestration-jev ~/.claude/skills/
mkdir -p ~/.claude/agents && cp pbi-orchestration-jev/skills/pbi-orchestration-jev/agents/pbi-worker.md ~/.claude/agents/
```

`pbi-worker` defines the worker and reviewer sub-agent. Restart Claude Code to load it. Without it the skill falls back to general-purpose (you only lose the guard that stops workers from searching memory).

Put your Jev API key in one of these places (checked in this order):

1. the `TYPESAFE_API_KEY` environment variable
2. a credentials file at `~/.config/typesafe/credentials.json` containing `{"api_key": "..."}`. Override the path with `TYPESAFE_CREDENTIALS_FILE`.

Never commit the key. Consider adding the credentials file to `permissions.deny` in Claude Code's `settings.json` so agents cannot read it.

### Optional: rtk

[rtk](https://github.com/rtk-ai/rtk) is a CLI proxy that compresses the output of common commands (git, tests, builds). The skill does not use `rtk init -g`, which would turn it on for every session. Instead, a bundled hook rewrites commands only in sessions where the skill ran `rtk/rtk-mode.mjs on`.

1. Install rtk (for example `winget install rtk-ai.rtk`, or `cargo install --git https://github.com/rtk-ai/rtk`; plain `cargo install rtk` is a different package) and check `rtk gain` works.
2. Add the hook to `~/.claude/settings.json`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash|PowerShell",
           "hooks": [{ "type": "command", "command": "node \"<HOME>/.claude/skills/pbi-orchestration-jev/rtk/rtk-gate.mjs\"", "timeout": 10 }]
         }
       ]
     }
   }
   ```

3. Restart Claude Code so the hook and `rtk` on your `PATH` are picked up.

Without the flag file (`~/.claude/state/rtk/<session_id>.on`), or without `rtk` on the `PATH`, the hook passes commands through unchanged. For opencode and Codex, rtk's own `rtk init -g --opencode` / `rtk init -g --codex` apply globally.

### Notes on opencode

- The DeepSeek models on OpenCode Go return HTTP 400 unless the workspace's privacy setting is Global.
- Starting several `opencode run` at the same moment can fail with `database is locked`. The skill staggers starts by 30 seconds.
- If opencode lives in a Node installed by fnm or nvm, it may not be on Claude Code's `PATH`. `runners.md` shows how to find it.

### Notes on DeepSeek Harness (`dsh`)

- `dsh` is not in the default routing; add it to your `routing.json` or a priority template. A step looks like `{"runner": "dsh", "model": "deepseek-official/deepseek-flash", "effort": "high"}`: the model is `<dsh provider>/<model>`, and the effort is passed to dsh as `reasoningEffort`. Supported values depend on the provider and the installed dsh version. Omit `effort` to omit `reasoningEffort`; dsh may then apply its provider default.
- `dsh_run.py` runs one PBI: it writes a per-run `--patch` overlay with the provider, model and effort, runs `dsh --profile headless --json`, and keeps the events, the final answer and the start/end time under `docs/pbi/dsh/`. `record_run.py` sums the token usage from the `status` events (no cost is reported).
- API keys come from the environment (`DEEPSEEK_API_KEY`, `OPENCODE_GO_API_KEY`) or from key-only files (`~/.config/deepseek/deepseek.key`, `~/.config/opencode/opencode-go.key`). Values are never printed.
- It uses `dsh` on the `PATH` (`npm i -g @deepseek-ai/dsh`), or `PBI_DSH_BIN` / `PBI_DSH_NODE`. Custom providers go in the `cordis.patch.yml` of `DSH_HOME`.
- Headless runs use `DSH_PERMISSION_MODE=workspace-write`: writes stay inside the repository, and escalation requests are denied because nobody is there to approve them. Builds that need named pipes may fail inside the sandbox; the orchestrator runs the verification itself.

### Notes on Antigravity

- As of 2026-10, headless runs (`agy -p`) had command execution auto-denied (`RunCommand`), so work did not progress. The skill drops `agy` for the rest of the session after the first such denial.
- `agy` **silently denies commands it has no permission for and still exits 0**. Denials only show up in `denied_actions` in the JSON result and on stderr. The skill checks `denied_actions` every time.
- Permissions live in `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`. The sandbox is off by default, so `command(node)` alone was not enough; `unsandboxed(node)` was also needed (checked with agy 1.3.3).

## Usage

In Claude Code, invoke it for work that spans several files:

```
/pbi-orchestration-jev <what you want built>
```

To pick the routing template in a browser, run `/pbi-orchestration-jev templates` (see Template Studio above).

Everything is recorded under `docs/pbi/` in the target repository.

| Path | Contents |
|---|---|
| `docs/pbi/STATUS.md` | status, runner and rework count per PBI |
| `docs/pbi/HANDOFF.md` | what the next session needs after a hand-off |
| `docs/pbi/PBI-<n>.md` | the PBI itself |
| `docs/pbi/PBI-<n>.facts.json` / `.brief.md` / `.route.json` | routing inputs and result |
| `docs/pbi/worker.md` | how workers proceed and report (copied from the skill) |
| `docs/pbi/tools/unchanged.py` | checks files a PBI must not touch (copied from the skill) |
| `docs/pbi/routing.json` | try order (written by Template Studio; without it, the `routing.json` in your home or the bundled default applies) |
| `docs/pbi/reports/`, `docs/pbi/notes/` | workers' detailed reports and research notes |
| `docs/pbi/verify/`, `docs/pbi/reviews/` | verification logs and delegated reviews |
| `docs/pbi/jev-log.jsonl` | routing decisions when they change, decision-table overrides, hand-offs, and report triage by Jev |
| `docs/pbi/jev.config.json` | Jev gates (created from the template) |
| `docs/pbi/opencode/`, `docs/pbi/codex/`, `docs/pbi/agy/`, `docs/pbi/dsh/` | prompts and outputs for each runner |
| `docs/pbi/stats/` | token and cache stats |

The orchestrator only stops to wait for the user in four cases: approving the PBI list, hitting the rework limit (or needing a redesign), before destructive operations, and when it hands off a large context. After a hand-off, run `/clear` and then `/pbi-orchestration-jev resume`.

A session started without a project folder (the Claude desktop app runs it in an empty folder it creates) has no `docs/pbi/STATUS.md` in its working directory. The skill then picks the repository in this order:

1. the path in `/pbi-orchestration-jev resume <repository path>`
2. without a path, the path in the hand-off memory (tag `handoff`) in shared memory; if several tasks are mid hand-off, it asks which one to resume
3. otherwise, it asks you

It then checks that `STATUS.md` and the branch match, and either moves the session to that folder or keeps working with absolute paths. That is why `HANDOFF.md` and the hand-off memory always carry the repository's absolute path and branch.

## Files

```
skills/pbi-orchestration-jev/
├── SKILL.md                    # the skill (instructions for the orchestrator)
├── runners.md                  # how to hand PBIs to opencode / Codex / Antigravity / dsh
├── dsh_run.py                  # runs one PBI on DeepSeek Harness headless
├── verify-commands.md          # pitfalls when writing verification commands
├── worker.md                   # how workers proceed and report
├── appendix/                   # read only when needed (memory, rework, handoff, finish, jev, templates, priority-templates)
├── agents/pbi-worker.md        # worker / reviewer sub-agent (copy to ~/.claude/agents)
├── prepare.py                  # Jev gate 1 + decision table in one call; prints a table
├── verify.py                   # runs a PBI's verification commands; prints exit codes and tails
├── record_run.py               # one line per run in runs.jsonl, and a copy of its raw logs in the archive
├── model_report.py             # results, tokens, cost and a provisional grade per model, from runs.jsonl
├── prices.json                 # sample API prices for the cost_api_equiv estimate (a prices.json in your home overrides it; not what you are billed)
├── select_model.py             # decision table: tier, route and review depth; drops steps per agents.json
├── agents.py                   # reads agents.json and finds runner CLIs (PATH, fnm, nvm)
├── common.py                   # shared helpers: home resolution, prices, paths, labels
├── routing.default.json        # default routing: every tier on the Agent tool (override with routing.json in docs/pbi/ or your home)
├── studio/                     # build templates in a browser (index.html, serve.py, update_catalog.py, catalog.json)
├── templates.py                # priority templates: validate, sync from the web page, apply / answer / push writes
├── web/priority-templates.html # web page (artifact) to view and set priority templates
├── examples/routing/           # example routing template with external runners (multi-runner.json)
├── examples/templates/         # example priority templates (copy to templates/ in your home)
├── unchanged.py                # hash snapshot to check files a PBI must not touch
├── agent_stats.py              # tokens, cache rebuilds and context size from transcripts
├── jev.config.template.json    # template for Jev gates
├── opencode.config.json        # opencode permissions (no push, no reset, no outside dirs)
├── rtk/
│   ├── rtk-gate.mjs            # optional PreToolUse hook for rtk
│   └── rtk-mode.mjs            # turn rtk on / off for the current session
├── jev/
│   ├── jev.py / jev.mjs        # Jev client (Python / JS)
│   └── test_jev.py / jev.test.mjs
├── test_select_model.py / test_agents.py / test_common.py
├── test_templates.py / test_prepare.py / test_unchanged.py
├── test_record_run.py / test_model_report.py / test_dsh_run.py
├── test_references.py          # every file the skill docs name exists
└── test_agent_stats.py
```

### Jev client

```sh
python jev/jev.py <config.json> <gate> <text-file>   # use a gate from the config
python jev/jev.py --request <request.json>           # send a raw request
```

On success it prints the response JSON and exits 0. A Jev failure prints `{"error": ...}` and exits 1; bad arguments exit 2. 429/529 are retried up to 3 times; 5xx, connection errors and timeouts are retried once.

## Tuning

- The decision table and thresholds are starting points. After 10–20 PBIs, look at the results per model (`model_report.py --archive`: first-try pass, reworks, promotions, tokens and cost) and adjust the constants in `select_model.py` and `prepare.py`.
- Runner models and effort levels come from a routing template (`routing.json`, same shape as `routing.default.json`). Put it at `docs/pbi/routing.json` in the target repo, or `routing.json` in your home, and `select_model.py` uses it; without one, the bundled `routing.default.json` applies.
- You can also build the template in a browser (see Template Studio above).
- Review the hand-off threshold (250,000 tokens) and the results of the external runners at the same time.

## Tests

```sh
cd skills/pbi-orchestration-jev
python -m unittest discover -p 'test_*.py'
python -m unittest discover -s studio -p 'test_*.py'
cd jev
python -m unittest test_jev
node --test --test-reporter=tap jev.test.mjs
```

The Jev client tests only talk to a mock server on 127.0.0.1; they never call the real Jev.

## License

[MIT](LICENSE)
