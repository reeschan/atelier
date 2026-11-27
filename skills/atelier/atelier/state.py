"""Run state load/save and PBI wave tracking under <root>/docs/pbi/."""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any

from . import SCHEMA_RUN, guards


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
            "round": 0,
            "launch_seq": 0,
            "answer_seq": 0,
            "plan_returns": 0,
            "fix_stages": 0,
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
        "e2e": None,
        "acceptance_first": None,
        "acceptance_reason": None,
        "acceptance_commands": [],
        "red": {},
        "unresolved": [],
        "plan_returns": [],
        "lock": None,
        "planner_mode": "plan",
        "plan_revision_request": "",
        "open_questions": [],
        "pbis": [],
        "findings": [],
        "fix_list": [],
        "triage_ask_items": [],
        "fix_groups": [],
        "fix_groups_done": [],
        "final_check": None,
        "leftovers": [],
        "batches": [],
        "launch_reads": {},
        "pbi_launches": {},
        "first_pass_checked_pbis": [],
        "approved_destructive": [],
        "approved_garbled": [],
        "carry_overs": [],
        "outside_edits": [],
        "last_check_failure": "",
        "last_failed_rules": [],
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


def buildable_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The PBIs of the current wave that need building, in plan order."""
    return [p for p in ready_wave_pbis(run) if p.get("status", "pending") in ("pending", "building", "failed")]


def next_buildable_pbi(run: dict[str, Any]) -> dict[str, Any] | None:
    """Return the next PBI in the current wave that needs building."""
    pbis = buildable_pbis(run)
    return pbis[0] if pbis else None


def fix_groups_left(run: dict[str, Any]) -> list[int]:
    """Indexes of the fix groups that no worker has finished yet, in order.

    A run saved before groups were tracked one by one counts the groups before its old fix_group_index as done.
    """
    groups = run.get("fix_groups") or []
    done = run.get("fix_groups_done")
    if done is None:
        done = list(range(min(run.get("fix_group_index", 0), len(groups))))
    return [i for i in range(len(groups)) if i not in set(done)]


def mark_fix_group_done(run: dict[str, Any], group: int) -> None:
    """Record that a worker finished a fix group.

    A run saved before groups were tracked one by one first gets its old fix_group_index moved over,
    so the groups it had finished stay finished.
    """
    if run.get("fix_groups_done") is None:
        left = fix_groups_left(run)
        run["fix_groups_done"] = [i for i in range(len(run.get("fix_groups") or [])) if i not in left]
        run.pop("fix_group_index", None)
    if group not in run["fix_groups_done"]:
        run["fix_groups_done"].append(group)


def reset_fix_groups(run: dict[str, Any], groups: list[list[dict[str, Any]]] | None = None) -> None:
    """Start the fix groups over: every group is left to do."""
    if groups is not None:
        run["fix_groups"] = groups
    run["fix_groups_done"] = []
    run.pop("fix_group_index", None)


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


# Why a PBI is held. A carry-over whose fix PBI failed past its tries holds its PBIs as unresolved; a worker's stop
# holds its PBI and the PBIs that depend on it until the user answers the plan-return question.
UNRESOLVED_HOLD = "unresolved"
STOP_HOLD = "stop"
HELD_REASON_LABELS = {
    UNRESOLVED_HOLD: "未解決による保留",
    STOP_HOLD: "作業者の stop で止めた（途中の戻りの問いの答えを待つ）",
    "sensitive_dependency": "sensitive で外した PBI に依る",
    "stale_dependency": "計画の変更で前提が変わった",
}
# How many lines of a failed check's output an unresolved carry-over keeps.
OUTPUT_TAIL_LINES = 20


def output_tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    """The last lines of a check's output."""
    return "\n".join(str(text or "").strip().splitlines()[-lines:])


_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")


def test_file_names(path: str) -> list[str]:
    """The names under which a test file shows in a check's output, longest first.

    Each trailing part of its path (tests/test_x.py, test_x.py), and for a Python file its unittest module names
    (skills.atelier.tests.test_x, tests.test_x); a bare module name only for a file at the root, since a test
    method can have the same name.
    """
    parts = [p for p in str(path or "").strip().replace("\\", "/").removeprefix("./").split("/") if p]
    if not parts:
        return []
    names = ["/".join(parts[i:]) for i in range(len(parts))]
    if parts[-1].endswith(".py"):
        dotted = [*parts[:-1], parts[-1][: -len(".py")]]
        if all(_IDENTIFIER.match(p) for p in dotted):
            names += [".".join(dotted[i:]) for i in range(len(dotted)) if len(dotted) - i >= 2 or len(dotted) == 1]
    return names


def names_test_file(text: str, path: str) -> bool:
    """True when the output names the test file: one of its names, not as part of a longer name."""
    body = str(text or "").replace("\\", "/")
    for name in test_file_names(path):
        dotted = "/" not in name and not name.endswith(".py")
        before = r"(?<![\w.])" if dotted else r"(?<![\w.\-])"
        if re.search(before + re.escape(name) + r"(?!\w)", body):
            return True
    return False


def named_test_owners(text: str, pbis: list[dict[str, Any]]) -> list[str]:
    """The PBIs with a test file in their targets that the output names (step 2 of the failure owners)."""
    return [
        p["id"]
        for p in pbis
        if any(guards.is_test_file(t) and names_test_file(text, t) for t in p.get("targets") or [] if isinstance(t, str))
    ]


def failure_owners(pbis: list[dict[str, Any]], command: str, output: str) -> tuple[list[str], str | None]:
    """Whose failure a failed command of a wave check is, among the PBIs of the wave, and by which step.

    (1) the PBIs that have the command in their own checks, else (2) the PBIs with a test file in their targets that
    the command's output names (a path or a unittest module name), else (3) none: the failure is left unresolved.
    Fix PBIs go by the same steps. Returns (PBI ids, "checks" | "targets" | None).
    """
    own = [p["id"] for p in pbis if command in (p.get("checks") or [])]
    if own:
        return own, "checks"
    named = named_test_owners(output, pbis)
    if named:
        return named, "targets"
    return [], None


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


def hold_unresolved(run: dict[str, Any], co: dict[str, Any]) -> list[str]:
    """Hold a carry-over whose fix PBI failed past its tries, without asking the user.

    The fix PBI, the PBIs the carry-over holds and every unfinished PBI that depends on them, directly or
    through another PBI, are held as unresolved, and so is the carry-over. A held PBI keeps its changes in the
    working tree; a PBI held before for another reason keeps that reason. The PBIs that do not depend on them
    go on. Returns the held PBI ids in plan order.
    """
    sources = [pid for pid in [*co.get("pbis", []), co.get("fix_pbi")] if pid and get_pbi(run, pid) is not None]
    order = [p["id"] for p in run.get("pbis", [])]
    held = sorted(set(sources) | set(dependents_of(run, sources)), key=order.index)
    for pid in held:
        pbi = get_pbi(run, pid)
        if pbi is not None and pbi.get("status") != "held":
            pbi["status"] = "held"
            pbi["held_reason"] = UNRESOLVED_HOLD
            pbi["held_carry_over"] = co["id"]
    co["status"] = "unresolved"
    co["held_pbis"] = list(held)
    return held


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


# A carry-over gets a fix PBI at its PBI's level, then one at the next level; after that it is left unresolved
# and held (hold_unresolved), without asking the user.
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
        do_not_touch += [t for t in pbi.get("do_not_touch") or [] if t not in do_not_touch]
    # A path one failed PBI targets and another keeps untouched stays untouched for the fix PBI.
    targets = [t for t in targets if t not in do_not_touch]
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



# What the final check leaves over: a failed command of the whole check, a failed E2E command, or an item a fix
# worker returned in `open` (could not fix); and what the fix stage left over: an unlock whose fixed lines could not be
# confirmed (its file went back to the lock commit's content). Nothing left over is fixed again in the same run.
LEFTOVER_KINDS = ("check_failure", "e2e_failure", "fix_open", "unlock_failed")


def add_leftover(
    run: dict[str, Any],
    kind: str,
    what: str,
    *,
    command: str | None = None,
    exit_code: Any = None,
    output: str = "",
    items: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Record one leftover on the run: its kind, the command, the end of its output and the retrospective items it
    comes from (the ids of the fix list items)."""
    if kind not in LEFTOVER_KINDS:
        raise StateError(f"unknown leftover kind: {kind!r}")
    leftovers = run.setdefault("leftovers", [])
    entry = {
        "id": f"LO-{len(leftovers) + 1}",
        "kind": kind,
        "what": what,
        "command": command,
        "exit_code": exit_code,
        "output": output_tail(output),
        "items": list(items or []),
        **extra,
    }
    leftovers.append(entry)
    return entry


def open_item_text(item: Any) -> str:
    """One item of a worker's `open` list as text (a string, or an object with what/why/command)."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        parts = [str(item.get(k)).strip() for k in ("what", "command", "why", "reason") if item.get(k)]
        return " — ".join(p for p in parts if p) or json.dumps(item, ensure_ascii=False)
    return json.dumps(item, ensure_ascii=False)


OBJECTION_STATES = ("green", "red")
STOP_EXCEPTIONS = (1, 2, 3, 4)
STOP_EXCEPTION_LABELS = {
    1: "公開の口の前提が崩れた",
    2: "鍵つきのケースどうしが矛盾する",
    3: "前提の外のものが無い",
    4: "安全の問題",
}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def worker_result_errors(result: dict[str, Any]) -> list[str]:
    """Errors in the shape of a worker result's objections (case, what, why, state, interpretation) and stop
    (exception 1-4, reason). Both are optional; a field that is there must have the right shape."""
    errors: list[str] = []
    if "objections" in result:
        items = result["objections"]
        if not isinstance(items, list):
            errors.append("objections は配列にしてください（無ければ省く）。")
        else:
            for idx, item in enumerate(items):
                label = f"objections[{idx}]"
                if not isinstance(item, dict):
                    errors.append(f"{label} はオブジェクトにしてください。")
                    continue
                for key in ("case", "what", "why", "interpretation"):
                    if not _text(item.get(key)):
                        errors.append(f"{label} の {key} は空でない文字列にしてください。")
                if item.get("state") not in OBJECTION_STATES:
                    errors.append(f"{label} の state は {' か '.join(OBJECTION_STATES)} にしてください。")
    if "stop" in result:
        stop = result["stop"]
        if not isinstance(stop, dict):
            errors.append("stop はオブジェクトにしてください（無ければ省く）。")
        else:
            exc = stop.get("exception")
            if type(exc) is not int or exc not in STOP_EXCEPTIONS:
                errors.append("stop の exception は 1 から 4 の整数にしてください。")
            if not _text(stop.get("reason")):
                errors.append("stop の reason は空でない文字列にしてください。")
    return errors


def keep_worker_notes(pbi: dict[str, Any], result: dict[str, Any]) -> None:
    """Keep a worker's objections and stop on its PBI, for the report and the plan-return question.

    An objection asks nothing. A stop of a PBI being built is held (hold_for_stop) and asked about once the wave has
    nothing left to launch.
    """
    if isinstance(result.get("objections"), list) and result["objections"]:
        pbi["objections"] = [dict(o) for o in result["objections"] if isinstance(o, dict)]
    if isinstance(result.get("stop"), dict):
        pbi["stop"] = dict(result["stop"])


# The answers to the plan-return question.
PLAN_RETURN_DECISIONS = {"replan": "計画を直す", "continue": "異議として続ける"}


def hold_for_stop(run: dict[str, Any], pbi_id: str) -> list[str]:
    """Hold a PBI whose worker returned a stop, and every unfinished PBI that depends on it, directly or through
    another PBI, until the user answers the plan-return question. The other PBIs of the wave go on.

    Each held PBI keeps the status it had (held_from), so that going on puts it back: the stopped PBI to the wave's
    check as its worker built it, the others to wait for it. A PBI held, carried or finished before keeps what it was.
    Returns the held PBI ids in plan order.
    """
    order = [p["id"] for p in run.get("pbis", [])]
    held: list[str] = []
    for pid in sorted({pbi_id, *dependents_of(run, [pbi_id])}, key=order.index):
        pbi = get_pbi(run, pid)
        if pbi is None or pbi.get("status") in ("held", "carried", *FINISHED_STATUSES):
            continue
        pbi["held_from"] = pbi.get("status", "pending")
        pbi["status"] = "held"
        pbi["held_reason"] = STOP_HOLD
        pbi["held_stop"] = pbi_id
        held.append(pid)
    return held


def stop_held_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The PBIs a stop holds until the user answers the plan-return question, in plan order."""
    return [p for p in run.get("pbis", []) if p.get("status") == "held" and p.get("held_reason") == STOP_HOLD]


def stopped_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The PBIs whose worker returned a stop that the user has not answered yet."""
    return [p for p in stop_held_pbis(run) if p.get("held_stop") == p["id"]]


def stop_entry(pbi: dict[str, Any]) -> dict[str, Any]:
    """A stopped PBI's stop: the PBI, its title, the exception's number and label, and the reason."""
    stop = pbi.get("stop") if isinstance(pbi.get("stop"), dict) else {}
    exc = stop.get("exception")
    return {
        "pbi": pbi["id"],
        "title": pbi.get("title") or pbi["id"],
        "exception": exc,
        "label": STOP_EXCEPTION_LABELS.get(exc, "") if type(exc) is int else "",
        "reason": str(stop.get("reason") or ""),
    }


def record_plan_return(run: dict[str, Any], decision: str, note: str = "") -> dict[str, Any]:
    """Keep the user's answer to the plan-return question as a plan return and count it.

    The record (kind plan_return) holds the decision, each unanswered stop with its exception and reason, the
    stopped workers' objections, the PBIs that were held and the user's note; it goes on the retrospective list.
    Every answer adds one to counters["plan_returns"].
    """
    items = run.setdefault("plan_returns", [])
    stopped = stopped_pbis(run)
    record = {
        "id": f"PR-{len(items) + 1}",
        "kind": "plan_return",
        "decision": decision,
        "stops": [stop_entry(p) for p in stopped],
        "objections": [{"pbi": p["id"], **o} for p in stopped for o in p.get("objections") or [] if isinstance(o, dict)],
        "held_pbis": [p["id"] for p in stop_held_pbis(run)],
        "note": note or "",
    }
    items.append(record)
    counters = run.setdefault("counters", {})
    counters["plan_returns"] = int(counters.get("plan_returns") or 0) + 1
    return record


def release_stop_hold(run: dict[str, Any], plan_return_id: str) -> list[str]:
    """Put back the PBIs a stop held, when the user goes on with it as an objection.

    The stopped PBI goes to the wave's check as its worker built it, marked with the plan return; the PBIs that depend
    on it wait for it again. Returns their ids in plan order.
    """
    released: list[str] = []
    for pbi in stop_held_pbis(run):
        if pbi.get("held_stop") == pbi["id"]:
            pbi["plan_return"] = plan_return_id
        pbi["status"] = pbi.pop("held_from", None) or "pending"
        pbi.pop("held_reason", None)
        pbi.pop("held_stop", None)
        released.append(pbi["id"])
    return released
