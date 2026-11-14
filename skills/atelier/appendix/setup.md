# Appendix: setup and the editor

Read this when the argument is `setup`, `setup --recommended`, `models` or `templates`. Read it also when the argument is empty and home has no `agents.json`.

## First run

When home has no `agents.json`, the caller does this section before `conductor.py start`, not the designer. The caller asks, once, with AskUserQuestion, before the work starts: "Set up now" or "Start with agent only". The answer is recorded in `agents.json`, so later runs read the record and ask nothing. When the designer meets this state (no `agents.json`), it ends the episode with a question (kind `decision`) instead of asking.

- **Start with agent only**: run `python <skill>/setup_agents.py record --use ""`, then go back to the work. This records `agent` as the only enabled runner.
- **Set up now**: follow `## setup`, then go back to the original work.

## setup

1. Run `python <skill>/setup_agents.py detect`. It prints one line per runner: `found` or `missing`, the path, and the version.
2. Ask one multiple-choice question with AskUserQuestion. Put the external runners that `detect` found — opencode, codex, agy, dsh — in the list. `agent` is always used, so it stays out of the list. When no external runner was found, skip the question and continue with `agent` only.
3. Run `python <skill>/setup_agents.py record --use <chosen runners, comma-separated>`. `record` keeps the existing model choices and billing settings.
4. Follow `## models`.
5. Offer the agent definitions. `agents/` has `agents/pbi-worker.md`, `agents/atelier-designer.md`, `agents/atelier-reviewer.md` and `agents/atelier-tester.md`. Ask once, and copy them to `~/.claude/agents/` only when the user allows it (the work root's `.claude/agents/` works too; the conductor checks it and `CLAUDE_CONFIG_DIR` in place of `~/.claude`):

   ```bash
   mkdir -p ~/.claude/agents && cp <skill>/agents/*.md ~/.claude/agents/
   ```

   When the user does not allow it, leave the folder as it is. The skill runs without the definitions: `next` prints `warn agents: atelier-designer not installed; using fallback`, an agent launch uses `subagent_type: general-purpose` and puts `Read <skill>/agents/<role>.md and follow it as your instructions.` first in the prompt, and headless builds `--agents` from the same file (`appendix/episodes.md`).

   The skill needs depth 3: caller 0, designer 1, worker, reviewer or tester 2, the children of a skill they call 3. Set `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=3` before Claude Code starts. An agent at depth 3 has no Agent tool, so a skill child that would need its own child uses the built-in fallback (`appendix/review.md`, `appendix/tests-at-end.md`).
6. Follow `## templates`.
7. Follow `## Optional integrations`. After that, say in one line that the recommended set can be installed with `setup --recommended`. Do not ask now; only name it.

Use `--home DIR` on these commands when the user keeps the settings outside the default home (`## home`).

## models

Run `python <skill>/setup_agents.py models`. It refreshes every enabled runner and prints one line per runner.

- `<runner>  ok  <n> models (<method>)`: the list was refreshed. The models selected in `agents.json` stay selected.
- `<runner>  failed  <error>  (kept <n>; ...)`: the previous list is kept. Tell the user that the model can be added by hand with `python <skill>/setup_agents.py add-model --runner <runner> --id <model> [--effort <level> ...]`, or in the editor's agents tab (`## templates`).
- `warn <runner>: selected model <id> is no longer listed`: pass the line as it is. The selection stays in `agents.json`, and the user can keep the model by adding it by hand when the runner still accepts it.

The way a runner lists its models changes with its version. `### Known issues` keeps the version and date of the last check; read the command in the table there when a refresh fails in a new way.

### Known issues

Checked on 2026-10-21 with opencode 2.0.29, codex-cli 0.153.4, agy 1.4.4 and dsh 0.2.0-rc.2. A newer runner may rename a command or change its output; `--help` and the runner's release notes are the next place to look.

| Runner | How the list is obtained | Where the effort levels come from |
|---|---|---|
| opencode | `opencode models --standalone`, one model id per line | the model cache that opencode keeps |
| codex | `codex debug models`, JSON | the reasoning levels in the same JSON |
| agy | `agy models`, tab-separated | the level at the end of the model id |
| dsh | the bundled `dsh.models.json` catalog and `dsh headless --dump-config` | the thinking level in the catalog |
| agent | the Agent tool's built-in definitions (haiku, sonnet, opus, fable) | low through max |

## roles

The role models come from three layers, strongest first, and each key (runner, model, effort, permission_mode) is taken from the first layer that sets it:

1. **run**: `conductor.py start --role <role>[.<key>]=<value>` (repeatable; the key defaults to `model`, and `none` sets effort to `null`), for this run only. The roles are `designer`, `episodes.test`, `reviewer.deep`, `reviewer.other` and `tester`.
2. **repo**: `docs/pbi/roles.json`, the same `{"roles": {...}}` shape as below, for every run in this repository.
3. **home**: `roles` in home's `agents.json`, beside the runners.

A key no layer sets takes its default. For `episodes.test` and `reviewer.other`, when no layer sets their runner or model, the default is the first step of the routing template's middle tier (`tiers.sonnet` of `docs/pbi/routing.json`, home's `routing.json` or the bundled `routing.default.json`) whose runner the role takes and can use (enabled in agents.json and found, and before its `until`); its runner, model and effort show with the source `template`. The middle tier always ends with an agent step, so there is always one. An invalid value (not a non-empty string; `null` is allowed for effort only) is skipped and the next layer's value is used; `next` prints one `warn roles.<role>.<key> (<layer>)` line naming the value it used and where it came from. Effort `null` leaves the runner's own level.

`runner` is where the role runs: `agent` (Claude, the default) or a runner of `agents.json`. The designer and `episodes.test` take `agent`, `codex` or `claude`; the reviewer stages and the tester take `agent`, `opencode`, `codex`, `agy`, `dsh` or `claude`. `model` is then that runner's model id (`gpt-6.1-terra` for codex, `deepseek-official/deepseek-flash` for dsh). `start` stops with exit 2 when a runner other than agent is not enabled in agents.json or its CLI is not found, and prints a `warn roles.<role>.model` line when the model is not in that runner's model list (`models/<runner>.json`).

Every `next` that launches an episode prints `roles: designer opus (repo), reviewer.deep opus (default), ...`, and keeps each value with its layer in `running.roles` and the episode's `history` entry of `docs/pbi/conductor/run.json`. A headless `run` launches the designer with the values `next` kept.

- `designer`: `{"runner", "model", "effort", "permission_mode"}`. Defaults: runner `agent`, model `opus`, effort `null`, `permission_mode` `auto` (passed to `claude -p` for a headless designer). On codex, `permission_mode` names the sandbox (`read-only`, `workspace-write`, `danger-full-access`); any other value, `auto` included, gives `danger-full-access`. Effort goes to codex as `model_reasoning_effort`, with no cap.
- `episodes`: `{"test": {"runner", "model", "effort", "permission_mode"}}`: the lead of the test episode (`appendix/tests-at-end.md`, `## Test stage`). Its default is the routing template's middle tier (above), then `sonnet` at `high` on agent. When the lead hands a point over (`needs_designer`), the conductor runs that test episode on `designer`.
- `reviewer`: `{"deep": {"runner", "model", "effort"}, "other": {"runner", "model", "effort"}}`. `deep` defaults to model `opus` with effort `null`; `other` defaults to the routing template's middle tier (above), then `sonnet` at `high`. A review stage that is not deep uses `other`, even when `other` is the model that built the PBI (the reviewer starts with a fresh context). A deep review whose `deep` model built the PBI uses `other`; when both do, it uses whichever of opus and sonnet at high effort on agent is not the builder's model.
- `tester`: `{"runner", "model", "effort"}`. Defaults: runner `agent`, model `sonnet`, effort `null`.

A value shows as `model[/effort]`, followed by `@<runner>` when the runner is not agent: `gpt-6.1-terra/high@codex`. For example, the designer and both reviewer stages on GPT Terra 6.1 for one run:

```bash
python <skill>/conductor.py start --goal-file <file> --role designer.runner=codex --role designer=gpt-6.1-terra --role reviewer.deep.runner=codex --role reviewer.deep=gpt-6.1-terra --role reviewer.other.runner=codex --role reviewer.other=gpt-6.1-terra
```

```json
{"roles": {
  "designer": {"runner": "agent", "model": "opus", "effort": null, "permission_mode": "auto"},
  "episodes": {"test": {"runner": "agent", "model": "sonnet", "effort": "high", "permission_mode": "auto"}},
  "reviewer": {"deep": {"runner": "agent", "model": "opus", "effort": null},
               "other": {"runner": "agent", "model": "sonnet", "effort": "high"}},
  "tester": {"runner": "agent", "model": "sonnet", "effort": null}
}}
```

## templates

The editor is one local page with four tabs (agents, routing, priority, flow). Every save goes to home.

Start it in the background from the repository root with Bash's `run_in_background`:

```bash
python <skill>/studio/serve.py
```

The editor listens on 127.0.0.1 only and opens the browser itself. Add `--tab agents|routing|priority|flow` to start on a tab, and `--home DIR` to use a home other than the default.

1. Read the first output line, `OPEN <url>`, and give the URL to the user. Then wait; the background command notifies you when it ends.
2. Each save prints `SAVED <kind> <path>`. kind is one of agents, models, routing, template, application, workflow. Summarize the lines for the user as they arrive, so the user knows what was written.
3. The run ends with `CLOSED` (the user pressed Close) or `TIMEOUT` (nothing was saved for 1800 seconds; the 30-minute default closes an editor that was left open, and every save restarts the count. `--timeout 0` waits without a limit).

The tabs:

- **Agents and models**: choose the runners and the models for each runner, add a model by hand, and read the quality and price references beside the list.
- **Routing template**: build the step order for each tier. The editor writes `routing.json` to home after `select_model.routing_error` passes.
- **Priority templates and application**: create and edit priority templates, including rotate groups, then apply one with an expiry and an expiry rule. The editor writes `overrides.json`.

When `docs/pbi/PBI-*.facts.json` exists, run `python <skill>/prepare.py docs/pbi` after the editor closes and tell the user in one line per PBI how the assignment changed.

## Optional integrations

These steps come last in setup. Each one is optional; the skill runs with none of them.

**shared-memory**. Look for the memory tools (`mcp__shared-memory__search`, `mcp__shared-memory__get`, `mcp__shared-memory__write`). When they are missing, show the user https://github.com/reeschan/shared-memory and read that README's setup together with the user. Hand the registration to the `mcp-setup` skill when that skill is available. The setup steps live in the README; this appendix names only the repository and the skill.

**Jev**. The client reads the API key from the `TYPESAFE_API_KEY` environment variable, then from `~/.config/typesafe/credentials.json` (a file like `{"api_key": "..."}`; `TYPESAFE_CREDENTIALS_FILE` points at another path). `prepare.py` copies `jev.config.template.json` to `docs/pbi/jev.config.json` on its first run; copy the template there yourself when the gates need a change. Without a key, the Jev gates are skipped and the tier comes from the facts alone.

**rtk**. The hook rewrites Bash and PowerShell commands to `rtk <cmd>` only in sessions where rtk mode is on. Show the user the block to add to `~/.claude/settings.json` before writing it, and write it only after approval:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell",
        "hooks": [{ "type": "command", "command": "node \"<HOME>/.claude/skills/atelier/rtk/rtk-gate.mjs\"", "timeout": 10 }]
      }
    ]
  }
}
```

The 10-second hook timeout is room for the gate's own 5-second limit. Turn the mode on for the session with `node <skill>/rtk/rtk-mode.mjs on`; without the flag file, the hook passes commands through unchanged. Restart Claude Code after the settings change so the hook and `rtk` on the PATH are picked up. When the user does not approve the block, leave `~/.claude/settings.json` as it is.

**Recommended tools**. Run `python <skill>/tools_check.py` and show its table, one line per tool: status, how to install, and the fallback that runs without it. The tools are oh-my-mermaid (`omm` and the `omm-scan` skill, for the map stage), jevlint (for lint-survey and lint-recheck; `no-key` means it runs AST only until `jevlint auth set typesafe`), sac-strategic-compact (kiln), and pstack's `principle-test-behavior-not-implementation` (the test stage). Every row that is not `ok`:

1. Ask once with AskUserQuestion, naming each tool with its link: install it now, or go on with the fallback.
2. On yes, show the commands and run only the ones the user approves. Downloads and global installs need that yes; never install on your own. A plugin install needs `/reload-plugins` or a restart before it is listed.
3. On no, nothing changes: each run uses the fallback (`appendix/map.md`, `appendix/lint.md`, `appendix/kiln.md`, `appendix/tests-at-end.md`) and its stage record says so.

When jevlint lives outside PATH and the fnm or nvm Node versions, set its path in `agents.json`: `{"tools": {"jevlint": {"path": "<path to jevlint.cmd>"}}}`. The OMM cache folder is `omm.cache_dir` in the same file (`appendix/map.md`).

**Kiln** is on by default (`appendix/kiln.md`). To turn it off for a repository, write `{"enabled": false}` to `docs/pbi/kiln.json`. When `docs/pbi/kiln.json` already exists, keep it and say so; replace it only after the user explicitly approves.

## recommended

The argument `setup --recommended` installs nothing without the user's yes: it checks the recommended example template, shows the missing skills and asks once. Follow `appendix/adapters.md` (`## Recommended set`).

## home

home is the directory for per-user settings, outside the skill. The resolution order is `--home DIR`, then `ATELIER_HOME`, then the old names `PBI_HOME` and `PBI_TEMPLATES_HOME`, then `XDG_CONFIG_HOME/atelier`, then `~/.config/atelier`. Under `XDG_CONFIG_HOME` or `~/.config`, the old folder `pbi-orchestration-jev` is used while `atelier` does not exist, so a home from before the rename keeps working.

home is optional; a missing file means "not set", and the skill creates a file only when a save needs it.

| File | What it holds |
|---|---|
| `agents.json` | The runners to use, the CLI path and version found for each, when it was checked, the selected models, the billing kind, and the models for the roles (`roles`) |
| `models/<runner>.json` | The last model list fetched for that runner, with its fetch time, method, error, and effort levels |
| `routing.json` | The routing template that maps each tier to a step order |
| `templates/<name>.json` | One priority template |
| `workflows/<name>.json` | A workflow template; `flow.py` reads it before the bundled one of the same name |
| `components/<id>.json` | An external component written by `component.py add`; it cannot reuse a bundled id |
| `overrides.json` | The source of truth for the current application: `synced_at`, `updated_at` and `application` |
| `history.jsonl` | One line per finished application, with `ended_at` and `ended_how` (replaced, stopped, declined, expired) |
| `web.json` | The URL of the optional priority-templates page; sync runs only while this file has a URL |
| `prices.json` | Price overrides that sit on top of the bundled price table |
