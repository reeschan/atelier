# Appendix: Kiln (optional)

**Name.** The English term is **Kiln**, from Compaction (working-context cool-down / strategic compaction). The Japanese reading is **窯**. Japanese prose introduces it as Compaction 文脈の Kiln（読みは窯）, and does not lead with 窯 as the source term.

Read this when `docs/pbi/kiln.json` exists, the workflow template has a `kiln` object, the user asks about context budgets, or setup offers the compaction skill.

## Concept

**Kiln** (Japanese reading 窯) is Atelier's forge-wide constraint on working context. Stages still hand off through artifacts; the kiln only says how hot (how large) the orchestrator's conversation may run before it must cool down.

| Line | Meaning | Default when kiln is on |
|---|---|---|
| Soft | Cool at the next stage / wave boundary | designer 36000 / implementer 18000 |
| Peak | Must cool before the next stage starts | designer 48000 / implementer 24000 |
| Aux | Optional output compressor (rtk) | 150000 (same as handoff.md rtk) |
| Handoff | Write HANDOFF and stop for `/clear` + resume | 250000 (same as handoff.md) |

Roles map to the glossary: orchestrator = designer chamber, worker = implementer chamber.

Kiln is **off** unless `docs/pbi/kiln.json` exists or the template sets `kiln.enabled` to true. A `kiln` object with `"enabled": false` does not turn it on. The bundled templates ship that stub so `prompt_install` can offer the companion skill without changing the run. While kiln is off, keep using only the fixed thresholds in `appendix/handoff.md`. Kiln never replaces verify, STATUS, or `worker.md`. Atelier does not depend on sac-strategic-compact.

## Enable

1. Turn Kiln on by setting `kiln.enabled` to true on the workflow template, or by copying `<skill>/examples/kiln.json` to `docs/pbi/kiln.json` when that file is absent (the sample has `"enabled": true`). The run file wins over the template, so creating it overrides the template's thresholds. If `docs/pbi/kiln.json` already exists, keep it unless the user explicitly approves replacing it.
2. Install the companion skill **sac-strategic-compact** only after the user says yes. It does strategic cool-down and compact-event logs. Without it, still obey the kiln numbers using this appendix and `appendix/handoff.md` alone.
3. Keep one `run_id` for the whole Atelier run and put it on every `.kiln/compact-events.jsonl` line. When you write a handoff, store that same id in `docs/pbi/HANDOFF.md` as `Run id: <run_id>` (`appendix/handoff.md`). After `/clear`, resume reads it back and does not create another, so events stay on one run.

Companion skill: https://github.com/reeschan/sac-strategic-compact
Atelier mapping in that repo: `docs/atelier-mapping.md`

## Measure and cool

While Kiln is on, measure at each stage boundary and at every wave boundary, before the next stage or wave starts. A wave-boundary check alone cannot see a soft or peak crossing in an earlier stage.

1. `python <skill>/agent_stats.py --ctx`
2. If `docs/pbi/kiln.json` exists, use its numbers. Otherwise, when the template's kiln is enabled, use the template's numbers. Otherwise use `appendix/handoff.md` alone.
3. Order: aux (rtk) → soft/peak cool-down (state tablet + `/compact` or short resume) → handoff threshold (existing Hand off procedure).
4. Never cool mid-worker attempt.
5. When sac-strategic-compact is installed, follow it for the state tablet and **always** append a compact-event log line (including `noop`). Use this run's `run_id`, including the one restored from `HANDOFF.md` after resume. Log path default: `docs/pbi/.kiln/compact-events.jsonl`.

State tablet path when kiln is on: `docs/pbi/CONTEXT_STATE.md` (in addition to HANDOFF.md when handing off).

## Template field

Bundled templates keep kiln off and only name the companion skill:

```json
"kiln": {
  "enabled": false,
  "require_skill": false,
  "prompt_install": true,
  "skill": "sac-strategic-compact",
  "skill_url": "https://github.com/reeschan/sac-strategic-compact"
}
```

When `prompt_install` is true and sac-strategic-compact is missing, say so at Approve and during setup, and offer `skill_url`. Do not install it, and do not block the run.

A template that turns kiln on can also carry the lines (the run's `docs/pbi/kiln.json` still wins):

```json
"kiln": {
  "enabled": true,
  "require_skill": false,
  "designer_peak": 48000,
  "designer_soft": 36000,
  "implementer_peak": 24000,
  "implementer_soft": 18000,
  "handoff_at": 250000,
  "aux_compress_at": 150000,
  "log_path": "docs/pbi/.kiln/compact-events.jsonl"
}
```

`require_skill: true` means: if sac-strategic-compact is missing, say so at Approve and offer install. Do not block the run unless the user asked to require it.
