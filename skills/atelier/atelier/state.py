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
        "auto_fix_pbi": None,
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

    Never includes PBIs with status 'passed', 'held', or 'sensitive' (G8).
    """
    pbis = run.get("pbis", [])
    statuses = pbi_statuses(run)
    ready: list[dict[str, Any]] = []
    for pbi in pbis:
        if pbi.get("status", "pending") in ("passed", "held", "sensitive"):
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
