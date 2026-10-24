# Appendix: priority templates and applications

Read this for the start-time sync, for applying, extending or stopping one in chat, for an expired application, and for creating or editing a template in chat. For the editor, read `appendix/setup.md` (`## templates`). Terms follow `CONTEXT.md`; the spec is `docs/specs/priority-templates.md` in the repository.

## Terms and files

- **Priority template**: a named override of routes. It holds a list of steps per tier (haiku, sonnet, opus) and does not apply to a tier it has no override for.
- **Application**: a priority template turned on, with an expiry. Only one exists at a time. Fields: id, template, until (null for no expiry), on_expire (`ask` or `drop`), reason, started_at, started_via.
- **On expiry**: `ask` stops applying and asks the user "extend / stop / no expiry"; `drop` stops applying. The application states are none, active, expired_ask and expired_drop.
- **The source of truth is the local `<home>/overrides.json`**: `{"synced_at": ISO|null, "updated_at": ISO|null, "application": <application>|null}`. updated_at is when the application last changed locally. Missing or broken means no application, not an error. The web page (a claude.ai artifact) is an optional copy, synced only while `<home>/web.json` holds its URL. Applying, extending, stopping and expiry checks in chat work locally alone.
- **`<home>/history.jsonl`**: one line per finished application (`dict(application, ended_at=..., ended_how=...)`); ended_how is replaced, stopped, declined or expired.
- **`<home>/web.json`**: `{"url": "https://claude.ai/..."}`, the optional sync target.
- **home**: where it is and what it holds are in `appendix/setup.md` (`## home`). Never commit it; it is personal.
- **`<home>/templates/<name>.json`**: one template, UTF-8. name matches `^[a-z0-9][a-z0-9-]{0,62}$` and the file name without extension. description is a string (may be empty). tiers is a non-empty object. Each step has runner (opencode, codex, agy, dsh or agent), model (non-empty string) and effort (string or null). A step can be a rotate group instead.
- **Rotate group**: `{"rotate": [step, step, ...]}` as one element of a tier's steps. Two or more ordinary steps; no agent, no nesting, no key other than `rotate`. The first member changes per PBI and the rest follow as that PBI's fallbacks, which spreads PBIs evenly across runners.
- **Rotation number**: the PBI number (7 for PBI-007). The first member is number `mod` member count (from 0), followed by the rest in rotated order. `select_model.py` takes it from the facts file name; `--pbi N` overrides it for a single judgment; 0 when unknown. A rework or tier up of the same PBI gets the same order.
- Example: `[{"rotate": [A free, B free]}, {"rotate": [A paid, B paid]}]` tries A free, B free, A paid, B paid for even PBIs and B free, A free, B paid, A paid for odd ones, then the routing template's agent for the tier. The route in `route.json` is the expanded flat list, filtered afterwards by `--skip` (runner name or model id) and by until.
- Every time is an ISO 8601 string with a time zone (`2026-10-22T23:59:00+09:00`). A string without a time zone is invalid.

## The web page (optional)

Skip this when you work locally only.

- Publish `<skill>/web/priority-templates.html` as the user's own private artifact, with `capabilities: {db: {}, user: {}}`. Never embed personal data in the page; it reads everything from its database.
- Write the URL to `<home>/web.json`.
- Run `python <skill>/templates.py push` and write its `writes` with an ArtifactData batch. A read-only copy of each template lands in `templates/<name>`.
- The page cannot create or change templates. Do that in chat (`## Create or edit a template`) or in the editor.

## Start-time sync

Once, before the split. `<home>/web.json` decides whether to sync.

1. Read `<home>/web.json`.
2. No URL: do not sync. Run `python <skill>/templates.py status` and read the local state (`{"status", "application", "writes"}`). For `expired_drop`, this command appends to history.jsonl and cleans up overrides.json. Write "using the local copy" in the approval list.
3. A URL: read `state/current` with ArtifactData get. When the get fails, continue locally as in step 2.
4. Write its data (`{"application": ..., "updated_at": ...}`) to `docs/pbi/state.json`. A missing file or `{}` means no application.
5. Run `python <skill>/templates.py sync --state docs/pbi/state.json`. It keeps whichever of the web and local updated_at is newer, writes overrides.json, and prints `{"status", "application", "writes"}`.
6. Write any `writes` with an ArtifactData batch, adding the version you read as `if_version`. A write to an existing document needs `if_version`; a new document takes none.
7. Status `unknown_template`: the template is missing locally or invalid, and the copy keeps the application as it is. Fix the template or ask the user.

Use this result until the work ends. Do not read it again when an expiry passes mid-work.

## Apply, extend or stop in chat

Write locally first. Only with the web page, also write the printed `writes` with an ArtifactData batch. `--state` only computes writes from the database copy and leaves the local file alone, so leave it out in chat.

**Apply**

1. Decide the template name, the expiry (ISO 8601, or none), on_expire (`ask` or `drop`), and the reason.
2. Run `python <skill>/templates.py apply --template NAME --until ISO|none --on-expire ask|drop --reason TEXT --via chat`.
3. It prints the new `application` and `writes`. A previous application gets a history.jsonl line, and `writes` starts with a set on history (usually replaced; expired for an expired `drop`).
4. Only with a URL in `<home>/web.json`: write `writes` with an ArtifactData batch, adding `if_version` from the version you read.
5. Only with a URL: get `state/current` again, write it to the file, and run `templates.py sync --state` again. Without one, check with `templates.py status`.

**Extend, or remove the expiry**

- Extend: `python <skill>/templates.py answer --choice extend --until ISO`, with until after now.
- No expiry: `--choice forever` (until becomes null).
- Write `application` and `writes` as when applying; check the local state with `status`.

**Stop**

- `python <skill>/templates.py answer --choice stop`.
- It appends to history.jsonl and prints writes that set `state/current` to null (stopped when active, declined when expired_ask). Write them only with the web page.

## Expired application

- Before the approval, when the state is `expired_ask`, ask "extend (ask for the new expiry) / stop / no expiry" together with the approval. This adds no new stop.
- Apply the answer with `answer`: `extend`, `stop` (ended_how declined) or `forever`, without `--state`.
- Only with the web page: write `writes` in a batch and sync again. Without it, check with `templates.py status`.
- Run `prepare.py` again without `--only` to judge the PBIs again.
- `expired_drop` never applies on its own. The start-time sync's writes clean it up (ended_how expired, ended_at until).

## Create or edit a template

In chat or in the editor (`appendix/setup.md`, `## templates`).

1. Write the JSON to `<home>/templates/<name>.json` with the Write tool; name matches the file name. The editor saves the same file from its priority tab.
2. Check its shape with `python <skill>/templates.py check [NAME]`: one line each, `ok <name>` or `ng <name>: <problem>`. Do not keep it while any line is ng.
3. Confirm that each model and effort exists on its runner, rotate members one by one. opencode: `opencode run -m <model>#<effort> "Reply with OK"` must not fail with `provider.no-route` (`Variant unavailable`); leave out `#` when effort is null. dsh: run `dsh_run.py` with a prompt that says only "Reply with OK"; it must exit 0 with OK in `.final.md` (an unknown effort shows in `.err`).
4. Only when the web page should show it: run `python <skill>/templates.py push --existing NAME,...`. It prints sets of `templates/<name>` for valid templates and deletes for names missing locally; `--existing` takes the names on the web (read with ArtifactData).
5. Write those `writes` with an ArtifactData batch. The web only reads.

A template applies after the decision table and before the routing template (`routing.json`). It never applies to a sensitive PBI, and the last step of a route is always the routing template's agent for that tier. Steps may carry an until date, as in `routing.json`; a step past it drops out. A tier without an override is not touched.

## Morning notice task

- Create one scheduled task that runs at 9:00 every morning.
- Its prompt: read `<home>/overrides.json`; when the application's `until` is within 24 hours, notify the user (template name, expiry, time left, reason). When `until` is null, missing or past, end without doing anything.
- The window is 24 hours by default. One notice per expiry is enough.
- Read only; change nothing.
