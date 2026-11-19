"""Run state load/save and PBI wave tracking under <root>/docs/pbi/."""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

from . import SCHEMA_RUN


class StateError(Exception):
    """Raised when run state cannot be loaded or is invalid."""


def run_dir(root: Path) -> Path:
    return root / "docs" / "pbi"


def state_path(root: Path) -> Path:
    return run_dir(root) / "run.json"


def record_path(root: Path) -> Path:
    return run_dir(root) / "record.jsonl"


def has_run(root: Path) -> bool:
    return state_path(root).exists()


def new_run_state(
    root: Path,
    goal_file: Path,
    spec_file: Path | None,
    *,
    template_path: str | None = None,
    switch: dict[str, Any] | None = None,
    agents_path: str | None = None,
    active_template: dict[str, Any] | None = None,
    components: dict[str, Any] | None = None,
    base_commit: str | None = None,
    omm_cache_dir: str | None = None,
) -> dict[str, Any]:
    limits = copy.deepcopy(active_template["limits"]) if active_template else {
        "auto_fix": 1,
        "e2e_rounds": 2,
        "budget_tokens": None,
        "budget_usd": None,
    }
    return {
        "schema": SCHEMA_RUN,
        "run_id": f"run-{uuid.uuid4().hex[:8]}",
        "root": str(root.resolve()),
        "goal_file": str(goal_file.resolve()),
        "spec_file": str(spec_file.resolve()) if spec_file else None,
        "base_commit": base_commit,
        "state": "surveying",
        "subphase": None,
        "return_state": None,
        "template_path": template_path,
        "switch": copy.deepcopy(switch) if switch is not None else None,
        "agents_path": agents_path,
        "active_template": copy.deepcopy(active_template) if active_template is not None else None,
        "components": copy.deepcopy(components) if components is not None else {},
        "limits": limits,
        "counters": {
            "auto_fix_count": 0,
            "review_count": 0,
            "e2e_failures": 0,
            "e2e_limit": limits.get("e2e_rounds", 2),
            "e2e_continuing": False,
            "round": 0,
            "launch_seq": 0,
            "answer_seq": 0,
        },
        "approvals": {
            "spec": False,
            "plan": False,
            "fix_list": False,
            "revisions": [],
        },
        "usage": {
            "tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "unknown_tokens": 0,
            "unknown_cost": 0,
            "launches": [],
        },
        "omm_cache_dir": omm_cache_dir,
        "omm": None,
        "omm_asked": False,
        "data_ready": False,
        "plan_checks": [],
        "plan_e2e": [],
        "final_review": None,
        "findings_dropped": [],
        "triage_skipped": [],
        "e2e_skipped": False,
        "planner_mode": "plan",
        "plan_revision_request": "",
        "open_questions": [],
        "pbis": [],
        "findings": [],
        "fix_list": [],
        "triage_ask_items": [],
        "fix_groups": [],
        "fix_group_index": 0,
        "launch_reads": {},
        "pbi_launches": {},
        "first_pass_checked_pbis": [],
        "approved_destructive": [],
        "approved_garbled": [],
        "carry_overs": [],
        "outside_edits": [],
        "last_check_failure": "",
        "last_failed_rules": [],
        "last_e2e_failure": "",
        "last_fix_launch_ids": [],
        "last_fix_model": "",
        "pending_action": None,
        "pending_snapshot": None,
        "pending_tree_snapshot": None,
        "pending_plan_sends": None,
        "pending_interview_questions": None,
        "guard_issue": None,
        "pending_plan_change": None,
        "budget_approved_next": False,
        "completed_launches": [],
        "history": [],
    }


def load_state(root: Path) -> dict[str, Any]:
    path = state_path(root)
    if not path.exists():
        raise StateError(f"no active run at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"failed to read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_RUN:
        raise StateError(f"invalid run state schema in {path}")
    return data


def save_state(root: Path, run: dict[str, Any]) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def next_launch_id(run: dict[str, Any], prefix: str) -> str:
    run["counters"]["launch_seq"] += 1
    return f"{prefix}-{run['counters']['launch_seq']:04d}"


def pbi_statuses(run: dict[str, Any]) -> dict[str, str]:
    return {p["id"]: p.get("status", "pending") for p in run.get("pbis", [])}


def ready_wave_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return non-terminal PBIs whose dependencies have all passed or been waived.

    Never includes PBIs with status 'passed', 'held', 'sensitive' or 'carried' (G8).
    """
    pbis = run.get("pbis", [])
    statuses = pbi_statuses(run)
    ready: list[dict[str, Any]] = []
    for pbi in pbis:
        if pbi.get("status", "pending") in ("passed", "held", "sensitive", "carried"):
            continue
        deps = pbi.get("depends_on") or []
        waived = set(pbi.get("waived_deps") or [])
        if all(statuses.get(dep) == "passed" or dep in waived for dep in deps):
            ready.append(pbi)
    return ready


def next_buildable_pbi(run: dict[str, Any]) -> dict[str, Any] | None:
    """Return the next PBI in the current wave that needs building."""
    for pbi in ready_wave_pbis(run):
        if pbi.get("status", "pending") in ("pending", "building", "failed"):
            return pbi
    return None


def get_pbi(run: dict[str, Any], pbi_id: str) -> dict[str, Any] | None:
    for pbi in run.get("pbis", []):
        if pbi.get("id") == pbi_id:
            return pbi
    return None


def mark_pbi_status(run: dict[str, Any], pbi_id: str, status: str) -> None:
    pbi = get_pbi(run, pbi_id)
    if pbi is not None:
        pbi["status"] = status


def any_pending_pbis(run: dict[str, Any]) -> bool:
    return any(p.get("status", "pending") == "pending" for p in run.get("pbis", []))


FINISHED_STATUSES = ("passed", "sensitive")

STATUS_LABELS = {
    "pending": "未着手",
    "building": "作っている途中",
    "built": "検査の前",
    "failed": "検査に落ちた",
    "held": "保留",
    "carried": "検査に落ち、持ち越した",
}


def rebuild_hold_targets(run: dict[str, Any]) -> list[dict[str, Any]]:
    """PBIs that the rebuild question's hold applies to: the failed ones, else the built ones."""
    pbis = run.get("pbis", [])
    failed = [p for p in pbis if p.get("status") == "failed"]
    return failed or [p for p in pbis if p.get("status") == "built"]


def dependents_of(run: dict[str, Any], ids: list[str]) -> list[str]:
    """IDs of unfinished PBIs that depend on any of ids, directly or through another PBI, in plan order."""
    sources = set(ids)
    found: list[str] = []
    added = True
    while added:
        added = False
        for pbi in run.get("pbis", []):
            pid = pbi["id"]
            if pid in sources or pbi.get("status") in FINISHED_STATUSES:
                continue
            if any(dep in sources for dep in pbi.get("depends_on") or []):
                sources.add(pid)
                found.append(pid)
                added = True
    order = [p["id"] for p in run.get("pbis", [])]
    return sorted(found, key=order.index)


def hold_after_rebuild(run: dict[str, Any], *, continue_dependents: bool) -> list[str]:
    """Hold the PBIs the rebuild question was about; optionally let the PBIs that depend on them go on.

    A held PBI keeps its changes in the working tree. With continue_dependents, each pending PBI that
    depends on a held one waives that dependency, and the held PBI is checked again with it (recheck_held_pbis).
    """
    held = [p["id"] for p in rebuild_hold_targets(run)]
    for pbi in run.get("pbis", []):
        if pbi["id"] in held:
            pbi["status"] = "held"
            pbi["held_reason"] = "check_failed"
    if continue_dependents:
        for pbi in run.get("pbis", []):
            if pbi.get("status", "pending") != "pending":
                continue
            waive = [dep for dep in pbi.get("depends_on") or [] if dep in held]
            if waive:
                pbi["waived_deps"] = sorted(set(pbi.get("waived_deps") or []) | set(waive))
    return held


def recheck_held_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    """PBIs held after a failed check that a PBI now being checked went on without.

    Their checks run again with that PBI's, so a PBI that only failed for want of its dependent
    (for example an export not used yet) passes once the dependent is built. A held PBI picked up
    this way brings in the held PBIs it went on without in turn.
    """
    pbis = run.get("pbis", [])
    sources = [p for p in pbis if p.get("status") in ("built", "failed")]
    picked: list[dict[str, Any]] = []
    while sources:
        waived = {dep for p in sources for dep in p.get("waived_deps") or []}
        sources = [
            p
            for p in pbis
            if p["id"] in waived
            and p.get("status") == "held"
            and p.get("held_reason") == "check_failed"
            and p not in picked
        ]
        picked += sources
    order = [p["id"] for p in pbis]
    return sorted(picked, key=lambda p: order.index(p["id"]))


def unfinished_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    """PBIs that neither passed nor left the run as sensitive."""
    return [p for p in run.get("pbis", []) if p.get("status", "pending") not in FINISHED_STATUSES]


def unmet_deps(run: dict[str, Any], pbi: dict[str, Any]) -> list[str]:
    statuses = pbi_statuses(run)
    waived = set(pbi.get("waived_deps") or [])
    return [d for d in pbi.get("depends_on") or [] if statuses.get(d) != "passed" and d not in waived]


def write_sensitive_handoff(root: Path, pbi: dict[str, Any]) -> Path:
    """Write docs/pbi/handoff/<PBI>.md with goal, files, and dependencies."""
    handoff_dir = run_dir(root) / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    path = handoff_dir / f"{pbi['id']}.md"
    goal = pbi.get("goal") or pbi.get("title") or pbi["id"]
    files = pbi.get("files") or pbi.get("allowed_paths") or []
    deps = pbi.get("depends_on") or []
    reason = pbi.get("sensitive_reason") or ""

    lines = [
        f"# 引き継ぎメモ: {pbi['id']} ({pbi.get('title', pbi['id'])})",
        "",
        "## 目的 (Goal)",
        str(goal),
        "",
        "## 対象ファイル (Files)",
        *([f"- {f}" for f in files] or ["- (指定なし)"]),
        "",
        "## 依存関係 (Dependencies)",
        *([f"- {d}" for d in deps] or ["- なし"]),
        "",
        "## sensitive の理由と進め方の提案",
    ]
    if reason:
        lines.append(f"- 理由: {reason}")
    lines.append("- この PBI は run から外されました。文脈を分けるため、別のセッションまたは別の設計で進めてください。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def apply_sensitive_decision(
    root: Path,
    run: dict[str, Any],
    sensitive_ids: list[str],
    proceed_ids: list[str] | None = None,
) -> list[Path]:
    """Exclude sensitive PBIs, write handoffs, and hold dependents unless waived."""
    sensitive_set = set(sensitive_ids)
    proceed_set = set(proceed_ids or [])
    written: list[Path] = []
    if not sensitive_set:
        return written

    blocked = set(sensitive_set)
    changed = True
    while changed:
        changed = False
        for pbi in run.get("pbis", []):
            pid = pbi["id"]
            if pid in blocked or pid in proceed_set:
                continue
            if any(dep in blocked for dep in (pbi.get("depends_on") or [])):
                blocked.add(pid)
                changed = True

    for pbi in run.get("pbis", []):
        pid = pbi["id"]
        if pid in sensitive_set:
            pbi["status"] = "sensitive"
            written.append(write_sensitive_handoff(root, pbi))
        elif pid in proceed_set:
            waived = set(pbi.get("waived_deps") or [])
            for dep in pbi.get("depends_on") or []:
                if dep in blocked:
                    waived.add(dep)
            pbi["waived_deps"] = sorted(waived)
        elif pid in blocked:
            pbi["status"] = "held"
            pbi["held_reason"] = "sensitive_dependency"
    return written


def append_approval_revision(
    run: dict[str, Any],
    *,
    action: str,
    changed_pbis: list[str] | None = None,
    summary: str = "",
) -> dict[str, Any]:
    revs = run["approvals"].setdefault("revisions", [])
    rev_num = (revs[-1]["revision"] if revs else 0) + 1
    entry = {
        "kind": "plan",
        "revision": rev_num,
        "action": action,
        "changed_pbis": list(changed_pbis or []),
        "summary": summary,
    }
    revs.append(entry)
    return entry


def hold_stale_dependents(run: dict[str, Any], changed_pbis: list[str]) -> list[str]:
    """Hold only PBIs depending (directly or transitively) on a changed PBI."""
    changed_set = set(changed_pbis)
    if not changed_set:
        return []
    stale_sources = set(changed_set)
    held: list[str] = []
    added = True
    while added:
        added = False
        for pbi in run.get("pbis", []):
            pid = pbi["id"]
            if pbi.get("status") == "sensitive":
                continue
            deps = pbi.get("depends_on") or []
            if any(dep in stale_sources for dep in deps) and pid not in held:
                if pbi.get("status") != "held":
                    pbi["status"] = "held"
                pbi["stale"] = True
                pbi["held_reason"] = "stale_dependency"
                held.append(pid)
                stale_sources.add(pid)
                added = True
    return held


def record_plan_change(run: dict[str, Any], changed_pbis: list[str], summary: str = "") -> list[str]:
    """Append a plan revision after approval and hold PBIs depending on changed PBIs."""
    append_approval_revision(run, action="changed", changed_pbis=changed_pbis, summary=summary)
    return hold_stale_dependents(run, changed_pbis)


def record_usage(
    run: dict[str, Any],
    *,
    launch_id: str,
    role: str,
    runner: str,
    model: str,
    pbi_id: str | None,
    tokens: Any,
    cost: Any,
) -> None:
    usage = run.setdefault(
        "usage",
        {"tokens": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "unknown_tokens": 0, "unknown_cost": 0, "launches": []},
    )
    if isinstance(tokens, dict):
        inp = tokens.get("input", 0) if isinstance(tokens.get("input"), int) else 0
        out = tokens.get("output", 0) if isinstance(tokens.get("output"), int) else 0
        tot = tokens.get("total") if isinstance(tokens.get("total"), int) else inp + out
        usage["input_tokens"] += inp
        usage["output_tokens"] += out
        usage["tokens"] += tot
    elif isinstance(tokens, int) and not isinstance(tokens, bool):
        usage["tokens"] += tokens
    else:
        tokens = "unknown"
        usage["unknown_tokens"] += 1

    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        usage["cost_usd"] = round(float(usage["cost_usd"]) + float(cost), 6)
    else:
        cost = "unknown"
        usage["unknown_cost"] += 1

    usage["launches"].append(
        {
            "launch_id": launch_id,
            "role": role,
            "runner": runner,
            "model": model,
            "pbi": pbi_id,
            "tokens": tokens,
            "cost": cost,
        }
    )


# A carry-over gets a fix PBI at its PBI's level, then one at the next level; after that the user is asked.
FIX_PBI_TRIES = 2


def open_carry_overs(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in run.get("carry_overs") or [] if c.get("status") == "open"]


def carry_over_of(run: dict[str, Any], co_id: str) -> dict[str, Any] | None:
    return next((c for c in run.get("carry_overs") or [] if c["id"] == co_id), None)


def record_carry_over(run: dict[str, Any], pbis: list[dict[str, Any]], commands: list[str], output: str) -> dict[str, Any]:
    """Record a failed check of these PBIs that the run goes on past; the PBIs wait for it as carried."""
    carry_overs = run.setdefault("carry_overs", [])
    notes = [note for p in pbis for note in p.get("worker_carry_over") or []]
    if not commands:
        commands = list(dict.fromkeys(c for p in pbis for c in p.get("checks") or []))
    co = {
        "id": f"CO-{len(carry_overs) + 1}",
        "pbis": [p["id"] for p in pbis],
        "commands": list(commands),
        "output": output,
        "worker_notes": notes,
        "status": "open",
        "tries": 0,
        "fix_pbi": None,
    }
    carry_overs.append(co)
    for pbi in pbis:
        pbi["status"] = "carried"
    return co


def fix_pbi_for(run: dict[str, Any], co: dict[str, Any]) -> dict[str, Any]:
    """The fix PBI of a carry-over: the failed checks to pass, on the failed PBIs' targets and level."""
    originals = [p for p in (get_pbi(run, pid) for pid in co["pbis"]) if p is not None]
    targets: list[str] = []
    do_not_touch: list[str] = []
    checks = list(co["commands"])
    for pbi in originals:
        targets += [t for t in pbi.get("targets") or [] if t not in targets]
        do_not_touch += [t for t in pbi.get("do_not_touch") or [] if t not in do_not_touch and t not in targets]
    levels = [p.get("level", "L2") for p in originals]
    return {
        "id": f"{co['id']}-FIX",
        "kind": "fix",
        "carry_over": co["id"],
        "title": "修正: " + "、".join(p.get("title") or p["id"] for p in originals),
        "goal": f"持ち越し {co['id']} の落ちた検査を通す。元の PBI（{'、'.join(co['pbis'])}）の目的は変えない。",
        "depends_on": [],
        "level": max(levels) if levels else "L2",
        "level_reason": f"持ち越し {co['id']} の元の PBI の難度",
        "targets": targets,
        "files": list(targets),
        "do_not_touch": do_not_touch,
        "checks": checks,
        "facts": [],
        "acceptance": [f"落ちた検査が通る: {'、'.join(checks)}"],
        "evidence": [],
        "data_needs": [],
        "sensitive_candidate": False,
        "sensitive_reason": "",
        "status": "pending",
        "route_index": 0,
    }


def insert_fix_pbi(run: dict[str, Any], co: dict[str, Any], fix: dict[str, Any]) -> None:
    """Put the fix PBI before the PBIs left, and make the PBIs that need the carried ones wait for it."""
    pbis = run.setdefault("pbis", [])
    at = next((i for i, p in enumerate(pbis) if p.get("status", "pending") == "pending"), len(pbis))
    pbis.insert(at, fix)
    co["fix_pbi"] = fix["id"]
    carried = set(co["pbis"])
    for pbi in pbis:
        if pbi is fix or pbi.get("status", "pending") != "pending":
            continue
        needed = [dep for dep in pbi.get("depends_on") or [] if dep in carried]
        if needed:
            pbi["depends_on"] = [*pbi["depends_on"], fix["id"]]
            pbi["waived_deps"] = sorted(set(pbi.get("waived_deps") or []) | set(needed))


def settle_carry_over(run: dict[str, Any], co: dict[str, Any], status: str) -> None:
    """Close a carry-over that its fix PBI or the fix stage fixed: its PBIs pass with it."""
    co["status"] = status
    for pid in [*co["pbis"], co.get("fix_pbi")]:
        pbi = get_pbi(run, pid) if pid else None
        if pbi is not None:
            pbi["status"] = "passed"
            pbi.pop("held_reason", None)


def settle_passed_fix_pbis(run: dict[str, Any]) -> None:
    for co in open_carry_overs(run):
        fix = get_pbi(run, co.get("fix_pbi") or "")
        if fix is not None and fix.get("status") == "passed":
            settle_carry_over(run, co, "fixed_by_pbi")


def carry_over_ticket(run: dict[str, Any], fix: dict[str, Any]) -> dict[str, Any]:
    """What the worker of a fix PBI needs: the failed checks, their output and the original PBIs' goals."""
    co = carry_over_of(run, fix.get("carry_over") or "") or {}
    originals = [p for p in (get_pbi(run, pid) for pid in co.get("pbis") or []) if p is not None]
    return {
        "id": co.get("id"),
        "failed_commands": list(co.get("commands") or []),
        "output": co.get("output", ""),
        "worker_notes": list(co.get("worker_notes") or []),
        "original_pbis": [{"id": p["id"], "title": p.get("title", ""), "goal": p.get("goal", "")} for p in originals],
        "no_test_edits": True,
    }


def carry_over_fix_items(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The open carry-overs as items of the fix list; they skip triage."""
    items = []
    for co in open_carry_overs(run):
        files: list[str] = []
        for pid in co["pbis"]:
            pbi = get_pbi(run, pid) or {}
            files += [t for t in pbi.get("targets") or [] if t not in files]
        commands = "、".join(co["commands"]) or "PBI の検査"
        items.append({
            "id": co["id"],
            "kind": "carry_over",
            "title": f"落ちた検査を通す: {commands}",
            "reason": f"持ち越し（{'、'.join(co['pbis'])}）",
            "why": co.get("output", ""),
            "fix": f"{commands} を通す。元の PBI の目的は変えない。",
            "files": files,
        })
    return items
