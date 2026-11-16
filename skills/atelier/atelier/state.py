"""Run state load/save and PBI wave tracking under <root>/docs/pbi/."""

from __future__ import annotations

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


def has_run(root: Path) -> bool:
    return state_path(root).exists()


def new_run_state(root: Path, goal_file: Path, spec_file: Path | None) -> dict[str, Any]:
    return {
        "schema": SCHEMA_RUN,
        "run_id": f"run-{uuid.uuid4().hex[:8]}",
        "root": str(root.resolve()),
        "goal_file": str(goal_file.resolve()),
        "spec_file": str(spec_file.resolve()) if spec_file else None,
        "state": "surveying",
        "subphase": None,
        "return_state": None,
        "counters": {
            "auto_fix_count": 0,
            "review_count": 0,
            "e2e_failures": 0,
            "e2e_limit": 2,  # TODO(ticket 02): load e2e_limit from template
            "e2e_continuing": False,
            "round": 0,
            "launch_seq": 0,
            "answer_seq": 0,
        },
        "approvals": {
            "spec": False,
            "plan": False,
            "fix_list": False,
        },
        "pbis": [],
        "findings": [],
        "fix_list": [],
        "pending_action": None,
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
    """Return non-terminal PBIs whose dependencies have all passed.

    Never includes PBIs with status 'passed', 'held', or 'sensitive' (G8).
    """
    pbis = run.get("pbis", [])
    statuses = pbi_statuses(run)
    ready: list[dict[str, Any]] = []
    for pbi in pbis:
        if pbi.get("status", "pending") in ("passed", "held", "sensitive"):
            continue
        deps = pbi.get("depends_on") or []
        if all(statuses.get(dep) == "passed" for dep in deps):
            ready.append(pbi)
    return ready


def next_buildable_pbi(run: dict[str, Any]) -> dict[str, Any] | None:
    """Return the next PBI in the current wave that needs building."""
    for pbi in ready_wave_pbis(run):
        if pbi.get("status", "pending") in ("pending", "building", "failed"):
            return pbi
    return None


def mark_pbi_status(run: dict[str, Any], pbi_id: str, status: str) -> None:
    for pbi in run.get("pbis", []):
        if pbi["id"] == pbi_id:
            pbi["status"] = status
            return


def any_pending_pbis(run: dict[str, Any]) -> bool:
    return any(p.get("status", "pending") == "pending" for p in run.get("pbis", []))
