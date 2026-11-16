"""State machine transitions, guards, and the six never invariants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class TransitionError(Exception):
    """Raised when an event is not valid for the current state and guards."""


class InvariantError(Exception):
    """Raised when a state violates one of the six never rules."""


STATES: tuple[str, ...] = (
    "surveying",
    "asking_interview",
    "interviewing",
    "awaiting_spec_approval",
    "planning",
    "awaiting_plan_approval",
    "building",
    "checking",
    "auto_fixing",
    "asking_rebuild",
    "reviewing",
    "triaging",
    "awaiting_fix_approval",
    "fixing",
    "running_e2e",
    "asking_e2e_limit",
    "reporting",
    "done",
    "asking_budget",
    "asking_plan_broken",
    "asking_sensitive",
    "asking_template_switch",
    "asking_guard",
)

CROSS_CUTTING_STATES: frozenset[str] = frozenset(
    {
        "asking_budget",
        "asking_plan_broken",
        "asking_sensitive",
        "asking_template_switch",
        "asking_guard",
    }
)
TERMINAL_STATES: frozenset[str] = frozenset({"reporting", "done"})


@dataclass(frozen=True)
class Transition:
    source: str
    event: str
    target: str
    guard: Callable[[dict[str, Any]], bool] | None = None
    effect: Callable[[dict[str, Any]], None] | None = None


def check_invariants(run: dict[str, Any]) -> None:
    """Enforce the six never rules from the Atelier v8 state diagram."""
    s = run["state"]
    approvals = run["approvals"]
    c = run["counters"]

    if s == "building" and not approvals["plan"]:
        raise InvariantError("never BuildWithoutPlan: entered building without approved plan")
    if s == "planning" and not approvals["spec"]:
        raise InvariantError("never PlanWithoutSpec: entered planning without approved spec")
    if s == "auto_fixing":
        pbis = run.get("pbis", [])
        if pbis:
            active_id = run.get("auto_fix_pbi")
            if active_id:
                target = next((p for p in pbis if p.get("id") == active_id), None)
                if target is not None and target.get("auto_fix_count", 0) >= 2:
                    raise InvariantError(
                        f"never TwoAutoFixes: {active_id} entered auto_fixing with auto_fix_count >= 2"
                    )
            elif any(p.get("status") == "failed" and p.get("auto_fix_count", 0) >= 2 for p in pbis):
                raise InvariantError("never TwoAutoFixes: failed PBI entered auto_fixing with auto_fix_count >= 2")
        elif c["auto_fix_count"] >= 2:
            raise InvariantError("never TwoAutoFixes: entered auto_fixing with auto_fix_count >= 2")
    if s == "reviewing" and c["review_count"] >= 1:
        raise InvariantError("never SecondReview: entered reviewing with review_count >= 1")
    if s == "fixing" and not approvals["fix_list"] and c["e2e_failures"] == 0:
        raise InvariantError("never FixWithoutList: entered fixing without approved fix list or e2e failure")
    if s == "fixing" and c["e2e_failures"] > c["e2e_limit"] and not c["e2e_continuing"]:
        raise InvariantError("never FixPastLimit: entered fixing past e2e limit without continue approval")


def _approve_spec(run: dict[str, Any]) -> None:
    run["approvals"]["spec"] = True


def _clear_spec_approval(run: dict[str, Any]) -> None:
    run["approvals"]["spec"] = False


def _approve_plan(run: dict[str, Any]) -> None:
    run["approvals"]["plan"] = True


def _clear_plan_approval(run: dict[str, Any]) -> None:
    run["approvals"]["plan"] = False
    run["approvals"].pop("plan_sends", None)


def _approve_fix_list(run: dict[str, Any]) -> None:
    run["approvals"]["fix_list"] = True


def _failed_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in run.get("pbis", []) if p.get("status") == "failed"]


def _can_auto_fix(run: dict[str, Any]) -> bool:
    failed = _failed_pbis(run)
    if run.get("pbis") and failed:
        return all(p.get("auto_fix_count", 0) == 0 for p in failed)
    return run["counters"]["auto_fix_count"] == 0


def _needs_rebuild_ask(run: dict[str, Any]) -> bool:
    return not _can_auto_fix(run)


def _reset_auto_fix(run: dict[str, Any]) -> None:
    run["auto_fix_pbi"] = None
    if not run.get("pbis"):
        run["counters"]["auto_fix_count"] = 0


def _clear_active_auto_fix(run: dict[str, Any]) -> None:
    run["auto_fix_pbi"] = None


def _inc_auto_fix(run: dict[str, Any]) -> None:
    failed = _failed_pbis(run)
    if failed:
        target = failed[0]
        target["auto_fix_count"] = target.get("auto_fix_count", 0) + 1
        run["auto_fix_pbi"] = target["id"]
    run["counters"]["auto_fix_count"] += 1


def _inc_rebuild_fail(run: dict[str, Any]) -> None:
    if not _failed_pbis(run):
        run["counters"]["auto_fix_count"] += 1


def _inc_review(run: dict[str, Any]) -> None:
    run["counters"]["review_count"] += 1


def _inc_e2e_within_limit(run: dict[str, Any]) -> None:
    run["counters"]["e2e_failures"] += 1
    run["counters"]["e2e_continuing"] = False


def _inc_e2e_past_limit(run: dict[str, Any]) -> None:
    run["counters"]["e2e_failures"] += 1
    run["counters"]["e2e_continuing"] = False


def _continue_e2e(run: dict[str, Any]) -> None:
    run["counters"]["e2e_continuing"] = True


def _clear_e2e_continuing(run: dict[str, Any]) -> None:
    run["counters"]["e2e_continuing"] = False


def _can_e2e_auto_fix(run: dict[str, Any]) -> bool:
    c = run["counters"]
    return not c["e2e_continuing"] and (c["e2e_failures"] + 1) <= c["e2e_limit"]


def _e2e_exceeded(run: dict[str, Any]) -> bool:
    c = run["counters"]
    return c["e2e_continuing"] or (c["e2e_failures"] + 1) > c["e2e_limit"]


TRANSITIONS: tuple[Transition, ...] = (
    # Survey -> Interview / Plan
    Transition("surveying", "survey_done", "asking_interview"),
    Transition("asking_interview", "start_interview", "interviewing"),
    Transition("asking_interview", "skip_interview", "planning", effect=_approve_spec),
    # Interviewing & Spec approval
    Transition("interviewing", "interview_round", "interviewing"),
    Transition("interviewing", "interview_done", "awaiting_spec_approval"),
    Transition("awaiting_spec_approval", "revise_spec", "interviewing", effect=_clear_spec_approval),
    Transition("awaiting_spec_approval", "spec_approved", "planning", effect=_approve_spec),
    # Planning & Plan approval
    Transition("planning", "plan_done", "awaiting_plan_approval", guard=lambda r: r["approvals"]["spec"]),
    Transition(
        "awaiting_plan_approval",
        "revise_plan",
        "planning",
        guard=lambda r: r["approvals"]["spec"],
        effect=_clear_plan_approval,
    ),
    Transition("awaiting_plan_approval", "plan_approved", "building", effect=_approve_plan),
    # Building & Checking & Auto-fix & Rebuild
    Transition("building", "worker_returned", "building", guard=lambda r: r["approvals"]["plan"]),
    Transition("building", "wave_built", "checking", guard=lambda r: r["approvals"]["plan"]),
    Transition("building", "no_buildable_pbis_review", "reviewing", guard=lambda r: r["counters"]["review_count"] == 0),
    Transition("building", "no_buildable_pbis_report", "reporting"),
    Transition(
        "checking",
        "check_failed",
        "auto_fixing",
        guard=_can_auto_fix,
        effect=_inc_auto_fix,
    ),
    Transition(
        "checking",
        "check_failed",
        "asking_rebuild",
        guard=_needs_rebuild_ask,
        effect=_inc_rebuild_fail,
    ),
    Transition("auto_fixing", "auto_fix_returned", "checking", effect=_clear_active_auto_fix),
    Transition(
        "asking_rebuild",
        "rebuild_retry",
        "building",
        guard=lambda r: r["approvals"]["plan"],
        effect=_reset_auto_fix,
    ),
    Transition(
        "asking_rebuild",
        "rebuild_hold",
        "building",
        guard=lambda r: r["approvals"]["plan"],
        effect=_reset_auto_fix,
    ),
    Transition(
        "checking",
        "wave_passed_next",
        "building",
        guard=lambda r: r["approvals"]["plan"],
        effect=_reset_auto_fix,
    ),
    Transition(
        "checking",
        "all_pbis_passed",
        "reviewing",
        guard=lambda r: r["counters"]["review_count"] == 0,
        effect=_reset_auto_fix,
    ),
    # Final review (once) & Triage & Fix approval & Fixing
    Transition(
        "reviewing",
        "review_returned",
        "triaging",
        guard=lambda r: r["counters"]["review_count"] == 0,
        effect=_inc_review,
    ),
    Transition("triaging", "triaged", "awaiting_fix_approval"),
    Transition("awaiting_fix_approval", "fix_list_approved", "fixing", effect=_approve_fix_list),
    Transition("awaiting_fix_approval", "nothing_to_fix", "running_e2e"),
    Transition(
        "fixing",
        "fix_check_passed",
        "running_e2e",
        guard=lambda r: r["approvals"]["fix_list"] or r["counters"]["e2e_failures"] > 0,
        effect=_clear_e2e_continuing,
    ),
    # E2E & E2E limit
    Transition("running_e2e", "e2e_passed", "reporting"),
    Transition(
        "running_e2e",
        "e2e_failed",
        "fixing",
        guard=_can_e2e_auto_fix,
        effect=_inc_e2e_within_limit,
    ),
    Transition(
        "running_e2e",
        "e2e_failed",
        "asking_e2e_limit",
        guard=_e2e_exceeded,
        effect=_inc_e2e_past_limit,
    ),
    Transition("asking_e2e_limit", "e2e_continue", "fixing", effect=_continue_e2e),
    Transition("asking_e2e_limit", "stop", "reporting"),
    # Reporting -> Done
    Transition("reporting", "report_written", "done"),
    # Cross-cutting resume & stop
    Transition("asking_budget", "resume", "$return_state"),
    Transition("asking_budget", "stop", "reporting"),
    Transition("asking_plan_broken", "resume", "$return_state"),
    Transition("asking_plan_broken", "stop", "reporting"),
    Transition("asking_sensitive", "resume", "$return_state"),
    Transition("asking_sensitive", "stop", "reporting"),
    Transition("asking_template_switch", "resume", "$return_state"),
    Transition("asking_template_switch", "stop", "reporting"),
    Transition("asking_guard", "resume", "$return_state"),
    Transition("asking_guard", "stop", "reporting"),
)

CROSS_CUTTING_EVENTS: dict[str, str] = {
    "budget_exceeded": "asking_budget",
    "plan_broken": "asking_plan_broken",
    "sensitive_detected": "asking_sensitive",
    "switch_expired": "asking_template_switch",
    "guard_violation": "asking_guard",
}


def apply_event(run: dict[str, Any], event: str) -> str:
    """Apply event to run state using the transition table, enforcing guards and never rules."""
    check_invariants(run)
    src = run["state"]

    if event in CROSS_CUTTING_EVENTS:
        if src in CROSS_CUTTING_STATES or src in TERMINAL_STATES:
            raise TransitionError(f"cannot raise {event} from state {src}")
        run["return_state"] = src
        run["return_subphase"] = run.get("subphase")
        run["state"] = CROSS_CUTTING_EVENTS[event]
        run["subphase"] = None
        run["history"].append({"from": src, "event": event, "to": run["state"]})
        check_invariants(run)
        return run["state"]

    matches = [
        t for t in TRANSITIONS
        if t.source == src and t.event == event and (t.guard is None or t.guard(run))
    ]
    if len(matches) != 1:
        raise TransitionError(f"invalid transition: ({src}, {event}) matched {len(matches)} rules")

    rule = matches[0]
    target = rule.target
    restored_subphase = None
    if target == "$return_state":
        if not run.get("return_state"):
            raise TransitionError(f"no return_state recorded for {src}")
        target = run["return_state"]
        restored_subphase = run.pop("return_subphase", None)
        run["return_state"] = None
    elif src in CROSS_CUTTING_STATES and event == "stop":
        run["return_state"] = None
        run.pop("return_subphase", None)

    if rule.effect is not None:
        rule.effect(run)
    run["state"] = target
    run["subphase"] = restored_subphase
    run["history"].append({"from": src, "event": event, "to": target})
    check_invariants(run)
    return target
