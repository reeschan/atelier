# Appendix: setup and the editor

Read this when the argument is `setup`, `setup --recommended`, `models` or `templates`. Read it also when the argument is empty and home has no `agents.json`.

## First run

When home has no `agents.json`, ask one question with AskUserQuestion before the work starts: "Set up now" or "Start with agent only". The answer is recorded in `agents.json`, so later runs read the record and ask nothing.

- **Start with agent only**: run `python <skill>/setup_agents.py record --use ""`, then go back to the work. This records `agent` as the only enabled runner.
- **Set up now**: follow `## setup`, then go back to the original work.

## setup

1. Run `python <skill>/setup_agents.py detect`. It prints one line per runner: `found` or `missing`, the path, and the version.
2. Ask one multiple-choice question with AskUserQuestion. Put the external runners that `detect` found — opencode, codex, agy, dsh — in the list. `agent` is always used, so it stays out of the list. When no external runner was found, skip the question and continue with `agent` only.
3. Run `python <skill>/setup_agents.py record --use <chosen runners, comma-separated>`. `record` keeps the existing model choices and billing settings.
4. Follow `## models`.
5. Follow `## templates`.
6. Follow `## Optional integrations`. After that, say in one line that the recommended set can be installed with `setup --recommended`. Do not ask now; only name it.

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

## templates

The editor is one local page with three tabs. Every save goes to home.

Start it in the background from the repository root with Bash's `run_in_background`:

```bash
python <skill>/studio/serve.py
```

The editor listens on 127.0.0.1 only and opens the browser itself. Add `--tab agents|routing|priority` to start on a tab, and `--home DIR` to use a home other than the default.

1. Read the first output line, `OPEN <url>`, and give the URL to the user. Then wait; the background command notifies you when it ends.
2. Each save prints `SAVED <kind> <path>`. kind is one of agents, models, routing, template, application. Summarize the lines for the user as they arrive, so the user knows what was written.
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
        "hooks": [{ "type": "command", "command": "node \"<HOME>/.claude/skills/pbi-orchestration-jev/rtk/rtk-gate.mjs\"", "timeout": 10 }]
      }
    ]
  }
}
```

The 10-second hook timeout is room for the gate's own 5-second limit. Turn the mode on for the session with `node <skill>/rtk/rtk-mode.mjs on`; without the flag file, the hook passes commands through unchanged. Restart Claude Code after the settings change so the hook and `rtk` on the PATH are picked up. When the user does not approve the block, leave `~/.claude/settings.json` as it is.

## recommended

The argument `setup --recommended` installs nothing by itself: it shows the one recommended set and asks once. Follow `appendix/adapters.md` (`## Recommended set`).

## home

home is the directory for per-user settings, outside the skill. The resolution order is `--home DIR`, then `PBI_HOME`, then `XDG_CONFIG_HOME/pbi-orchestration-jev`, then `~/.config/pbi-orchestration-jev`. The old `PBI_TEMPLATES_HOME` is still read for compatibility.

home is optional; a missing file means "not set", and the skill creates a file only when a save needs it.

| File | What it holds |
|---|---|
| `agents.json` | The runners to use, the CLI path and version found for each, when it was checked, the selected models, and the billing kind |
| `models/<runner>.json` | The last model list fetched for that runner, with its fetch time, method, error, and effort levels |
| `routing.json` | The routing template that maps each tier to a step order |
| `templates/<name>.json` | One priority template |
| `workflows/<name>.json` | A workflow template; `flow.py` reads it before the bundled one of the same name |
| `overrides.json` | The source of truth for the current application: `synced_at`, `updated_at` and `application` |
| `history.jsonl` | One line per finished application, with `ended_at` and `ended_how` (replaced, stopped, declined, expired) |
| `web.json` | The URL of the optional priority-templates page; sync runs only while this file has a URL |
| `prices.json` | Price overrides that sit on top of the bundled price table |
