# Runners: launching a worker

Read this when the build stage picks `build.runners`: `## Common rules` first, then only the section of each runner that appears in a route, before its first use.

## Common rules

**Order.** Try each PBI's `route` from its head. A runner that is unusable (its "Unusable when" below) is not a failed PBI: record it with `record_run.py <report> --attempt <position> --outcome unavailable` before moving on (the next run overwrites the raw logs), take the next step, and update the PBI's model and note in STATUS.md. `prepare.py` has already matched external runners against agents.json and the CLIs it found; steps for a missing runner, an unused runner or an unselected model are listed as dropped and never enter a route.

**One worker per attempt.** Every attempt starts a new worker, reworks included. Never continue an earlier conversation (`opencode run -c`, `codex exec resume`, `dsh --session-id`); the report file and the rework note carry what is needed. The only message to a running worker is a continue after `NEEDS_CONTEXT` (`appendix/verify.md`).

**The prompt.** Every runner gets the same prompt. Never copy the PBI into it; pass paths.

```
Work root: <absolute path of the repository or worktree>. Work here and write nothing outside it.
PBI: docs/pbi/PBI-<n>.md
Rework: docs/pbi/PBI-<n>.rework-<k>.md (previous report: docs/pbi/reports/<previous report>.md)   <- reworks only
Report name: <PBI-<n> or PBI-<n>.rework-<k>>
Files that parallel PBIs touch: <none / paths>
How to work and report: read docs/pbi/worker.md first and follow it.
```

**External runners** (all but `agent`):

- Write the prompt to `docs/pbi/<runner>/<report>.prompt.md` and end it with `Summary file: docs/pbi/<runner>/<report>.last.md`.
- Launch one per background Bash call (`run_in_background`) from the repository root, never several joined with `&`. cd with a literal absolute path; a cd through a variable stops at Claude Code's permission check.
- Treat them as unable to use shared memory. When the PBI lists refs, `get` each one yourself and append them to the prompt file under `## Refs (workspace)`, by id, adding "The refs are copied below. Do not use memory." The template's `build.external_memory` stays `none` unless the user chose `refs-only` for a runner that has a memory `get` tool.
- They read AGENTS.md or nothing, never CLAUDE.md. Put the conventions a PBI needs in the PBI.
- Read only `.last.md` (5 lines). Never read `.jsonl`, `.log` or `.json` whole; take the items named below with `grep -m` or `tail -n`.
- `record_run.py` reads tokens, cost, steps and time from each runner's logs. Never add them by hand.
- After one unusable run, retry once; then take the next step. When a usage limit is the cause, write "<runner> <model>: limit reached (<time>)" at the top of STATUS.md and, for the rest of the session, route again with `--skip <model>` (several models joined by commas). Only that model's steps drop out. When the runner itself is broken (authentication, connection), use `--skip <runner>`.
- Two or more at once: start each one 30 seconds after the previous (`sleep 30;`, `sleep 60;` ... in front), because opencode and dsh share local state.

## agent (Agent tool)

- Launch the Agent tool with `subagent_type: "pbi-worker"`, and the route's `model` and `effort` (leave effort out when it is null). When `pbi-worker` is missing, use `general-purpose` and say so in STATUS.md.
- The description starts with the report name and a space: `PBI-007 <short text>`, `PBI-007.rework-1 <short text>`. `record_run.py` and `agent_stats.py` link the transcript to the run through it. Your own reviews are `PBI-<n>.review` and `PBI-<n>.design`.
- The prompt is the one above, given directly.

## opencode

**Prerequisites.** The opencode CLI (`npm i -g opencode-ai`) and a provider account. A provider that needs a privacy setting (DeepSeek through OpenCode Go needs Global) fails with 400 without it. Free models (ids starting with `opencode/`) may log inputs on the provider's side; priority templates never route a sensitive PBI to them.

**Find it first.** opencode installed into a Node from fnm or nvm may not be on Claude Code's PATH, so `which opencode` failing does not prove it is missing. Look it up and put its directory (`<OC_DIR>`) in front of PATH on every launch:

```bash
command -v opencode || ls -d "$(cygpath "$APPDATA")"/fnm/node-versions/*/installation/opencode.cmd 2>/dev/null
"<OC_DIR>/node_modules/opencode-ai/bin/opencode.exe" --version
```

When it is still not found, treat it as unusable and ask the user to install it.

**Launch.**

```bash
mkdir -p docs/pbi/opencode
PATH="<OC_DIR>:$PATH" OPENCODE_CONFIG="<skill>/opencode.config.json" opencode run -m <model>[#<effort>] --format json "Read docs/pbi/opencode/<report>.prompt.md and follow it." < /dev/null > docs/pbi/opencode/<report>.jsonl 2> docs/pbi/opencode/<report>.err
```

- Add `#<effort>` when the route's effort is not null. An unknown effort fails with `provider.no-route` (`Variant unavailable`).
- `opencode.config.json` allows edits and commands, and denies `git push`, `git reset --hard`, `git clean`, `rm -rf`, `rm -r` and anything outside the repository. Never pass `--auto`, and never change the user's own settings in `~/.config/opencode/`.
- `opencode.edit-only.config.json` allows only reading and writing files: no commands, subagents, skills, questions or web tools. You run the verify commands yourself. jev-mcp's `run` picks it with `setting: "edit-only"`.

**Unusable when.**

| Sign | Note |
|---|---|
| exit code other than 0 | |
| `grep -m 3 '"type":"error"'` matches | usage limit, authentication, region, connection |
| `.last.md` missing or empty | |
| `.err` says `database is locked` | two launches too close; retry once alone. Not a failed PBI |

**Known issues** (checked 2026-10 with opencode 2.0.x).

- A denial still exits 0. `grep -c 'rule which prevents' <report>.jsonl` of 1 or more means something was denied (a `"tool"` event with `"status":"error"`). When only verify commands were denied, run them yourself; when the work did not progress, take the next step.
- Free models report cost 0.

## codex

**Prerequisites.** The Codex CLI, signed in.

**Launch.**

```bash
mkdir -p docs/pbi/codex
codex exec -m <model> -c model_reasoning_effort='"<effort>"' -s workspace-write -o docs/pbi/codex/<report>.last.md - < docs/pbi/codex/<report>.prompt.md > docs/pbi/codex/<report>.log 2>&1
```

- `-o` writes the last message (the 5 lines of `worker.md`) to `.last.md`.
- Use the route's effort, never above `high` (no xhigh or max).

**Unusable when.**

| Sign | Note |
|---|---|
| exit code other than 0 with a usage-limit, authentication or connection error | |
| `.last.md` empty | |

**Known issues** (checked 2026-10 with codex-cli 0.153).

- Only the token total is reported (`tokens used` in the log), so the breakdown is null and the session stats leave Codex out.

## agy (Antigravity)

**Prerequisites.** The Antigravity CLI (`agy`; on Windows `%LOCALAPPDATA%\agy\bin\agy.exe`), signed in.

**Launch.**

```bash
mkdir -p docs/pbi/agy
agy -p "Read docs/pbi/agy/<report>.prompt.md and follow it." --model <model> --mode accept-edits --output-format json --print-timeout 3600s < /dev/null > docs/pbi/agy/<report>.json 2> docs/pbi/agy/<report>.err
```

**Unusable when.**

| Sign | Note |
|---|---|
| exit code other than 0 | |
| output mentions a usage limit, quota, rate limit, `RESOURCE_EXHAUSTED` or authentication | |
| `.last.md` missing or empty | |

**Known issues** (checked 2026-10-13 with agy 1.4).

- Headless runs auto-deny commands (`RunCommand`; `denied_actions: [{action: command}]`, no `.last.md`). When the first PBI shows this, write "Antigravity: commands denied headless (<time>)" at the top of STATUS.md and route the rest with `--skip agy`. A permission rule in `~/.gemini/antigravity-cli/settings.json` may fix it; that file is outside the repository, so ask the user first.
- A denial still exits 0 with status SUCCESS. Check `grep -o '"denied_actions":\[[^]]*\]' <report>.json` and `tail -n 5 <report>.err`.
- Tokens are not reported; the session stats leave agy out.

## dsh (DeepSeek Harness)

**Prerequisites.** The dsh CLI (`npm i -g @deepseek-ai/dsh`) and a key for the route's provider. The route's model is `<provider>/<model>` in dsh's names; effort becomes dsh's reasoningEffort, whose valid values depend on the provider and the dsh version. Providers and defaults live in `cordis.patch.yml` under `DSH_HOME`.

**Launch.**

```bash
mkdir -p docs/pbi/dsh
python <skill>/dsh_run.py <report> --model <model> [--effort <effort>]
```

- `dsh_run.py` writes a per-run patch (`<report>.patch.yml`: provider, model, reasoningEffort) and passes it with `--patch`. It writes the events to `.jsonl`, diagnostics to `.err`, the final text to `.final.md`, and start, end and exit code to `.run.json`. Leave out `--effort` when it is null.
- Keys: environment variables first, then `~/.config/deepseek/deepseek.key` (DeepSeek official) and `~/.config/opencode/opencode-go.key` (OpenCode Go). Values are never shown. A missing key exits 2: treat it as unusable and tell the user where the key goes.
- Location: `dsh` on the PATH, or `PBI_DSH_BIN` (`@deepseek-ai/dsh/lib/bin.js`) with `PBI_DSH_NODE` (default: node on the PATH). `DSH_HOME` is passed the same way.
- Permissions: `DSH_PERMISSION_MODE=workspace-write`. Writes stay in the repository; commands run in a sandbox (PowerShell on Windows). Headless runs have nobody to approve an escalation, so commands that need more are denied.

**Unusable when.**

| Sign | Note |
|---|---|
| exit code other than 0 | `dsh_run: dsh が見つかりません` means dsh is not found |
| `.err` has `dsh: <code>:` | `MISSING_CREDENTIAL`, `TRANSPORT`, `INVALID_REQUEST`, usage limit, low balance |
| `.last.md` missing or empty | |

**Known issues** (checked 2026-10 with dsh 0.2.0-rc).

- Builds that use named pipes (`npm run build`) can fail inside the sandbox. When only verify commands fail, run them yourself.
- Cost is not reported (null); the provider bills the account directly.

## claude (Claude Code headless)

Claude Code run as `claude -p`, separate from the `agent` runner. It is in no default route: it appears only when enabled in agents.json and named in `routing.json` or a priority template. The model is an alias (`haiku`, `sonnet`, `opus`) or a model id.

**Prerequisites.** The `claude` CLI, signed in. Location: `PBI_CLAUDE_BIN`, then `claude` on the PATH (agents.json's path also works).

**Launch.**

```bash
mkdir -p docs/pbi/claude
python <skill>/claude_run.py <report> --model <model> [--effort <effort>] [--setting default|edit-only]
```

- `claude_run.py` runs `claude -p --output-format json` with the model, effort, the setting's tools, `--permission-mode acceptEdits --permission-prompts none` and `--strict-mcp-config`, and passes the prompt file through stdin. Results go to `<report>.json`, diagnostics to `.err`, and times, exit code and setting to `.run.json`.
- `default`: Read, Write, Edit, Glob, Grep, Bash, PowerShell; no MCP tools, so refs reach it only when copied into the prompt. `edit-only`: Read, Edit, Write, Glob, Grep and `--restricted`; you run the verify commands yourself.
- It drops `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_BEDROCK_BASE_URL` and `CLAUDE_CODE_USE_BEDROCK` from the child's environment, so it runs on the Claude Code sign-in.

**Unusable when.**

| Sign | Note |
|---|---|
| exit code other than 0 | `claude_run: claude が見つかりません` (exit 2) means claude is not found |
| `is_error` true in `<report>.json` | |
| `.err` or `result` mentions `usage limit`, `rate limit`, `not logged in`, `Invalid API key` or `/login` | |
| `.last.md` missing or empty | |

**Known issues.**

- With a subscription sign-in, `total_cost_usd` is an API-price estimate, not a charge.

## mcp (jev-mcp)

An outside MCP client acts as the worker and takes the PBI from the jev-mcp server (`jev_mcp.py`). It is in no route; place a PBI here only when the user decides to. You do not launch the worker: write the prompt to `docs/pbi/mcp/<report>.prompt.md` (summary file `docs/pbi/mcp/<report>.last.md`) and wait for `.last.md`.

```bash
python <skill>/jev_mcp.py --root <absolute repository path>                  # stdio
python <skill>/jev_mcp.py --http --port 7830 --root <absolute repository path>  # http://127.0.0.1:7830/mcp
```

- Tools: `list_tasks`, `claim_task`, `submit_result`, `fail_task`, `verify`, `jev_gate`, `route`, `run`, `record`. `claim_task` creates `docs/pbi/mcp/<report>.claim.json` exclusively, so a report is handed out once.
- `route` returns one `select_model.py` judgment (`repo`, `report`, `tier`, `skip`) without writing `route.json`.
- `run` launches one report once on `dsh`, `opencode` or `claude` as described above, with `setting` `default` or `edit-only` (dsh: default only), and returns the exit code, `.last.md`, the report and log paths, the unusable verdict, and the setting's permissions.
- `record` calls `record_run.py` once, with the same columns.
- `submit_result` and `fail_task` write `docs/pbi/reports/<report>.md` and then `.last.md` (the 5 lines of `worker.md`). You read only `.last.md` and verify yourself; the client's `verify` result is its own claim.
- `jev_gate` calls `jev/jev.py` on the server side. The API key stays in the server's environment; only prose is sent.
- HTTP listens on 127.0.0.1 only. It refuses a Host other than 127.0.0.1, localhost or [::1] and a foreign Origin (403), and a POST that is not `application/json` (415). There is no authentication, so never expose it to other machines.

**Unusable when.** `.last.md` never comes, or the status is `NEEDS_CONTEXT` or `BLOCKED`: take the next step or rework, as with any runner. Remove a `.claim.json` left behind only after confirming the client has stopped.
