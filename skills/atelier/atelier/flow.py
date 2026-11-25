"""State machine transitions, guards, and the never invariants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .acceptance import red_pending, review_pending, stage_pending


class TransitionError(Exception):
    """Raised when an event is not valid for the current state and guards."""


class InvariantError(Exception):
    """Raised when a state violates one of the never rules."""


STATES: tuple[str, ...] = (
    "asking_omm",
    "scanning_omm",
    "surveying",
    "asking_interview",
    "interviewing",
    "awaiting_spec_approval",
    "planning",
    "awaiting_plan_approval",
    "preparing_data",
    "building_acceptance",
    "checking_red",
    "reviewing_acceptance",
    "building",
    "checking",
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
# The acceptance stage: the acceptance and stub PBIs are built, their Red check runs, then the planner reads the tests.
ACCEPTANCE_STATES: frozenset[str] = frozenset({"building_acceptance", "checking_red", "reviewing_acceptance"})
# With acceptance first, a PBI of these kinds is never launched before the acceptance tests are locked.
LOCK_GUARDED_KINDS: tuple[str, ...] = ("build", "e2e")


@dataclass(frozen=True)
class Transition:
    source: str
    event: str
    target: str
    guard: Callable[[dict[str, Any]], bool] | None = None
    effect: Callable[[dict[str, Any]], None] | None = None


def check_invariants(run: dict[str, Any]) -> None:
    """Enforce the never rules from the Atelier v8 state diagram."""
    s = run["state"]
    approvals = run["approvals"]
    c = run["counters"]

    if s in ACCEPTANCE_STATES and not run.get("acceptance_first"):
        raise InvariantError(
            "never AcceptanceWithoutToggle: entered the acceptance stage in a run that does not put acceptance first"
        )
    if s in ("building", "building_acceptance") and not approvals["plan"]:
        raise InvariantError(f"never BuildWithoutPlan: entered {s} without approved plan")
    if s in ("building", "building_acceptance") and needs_data(run) and not run.get("data_ready"):
        raise InvariantError(f"never BuildBeforeData: entered {s} before the approved data was in place")
    if s == "building" and stage_pending(run):
        raise InvariantError("never BuildBeforeRed: entered building before the acceptance stage and its Red check were done")
    if run.get("acceptance_first") and not run.get("lock"):
        early = [
            p.get("id") for p in run.get("pbis", [])
            if (p.get("kind") or "build") in LOCK_GUARDED_KINDS and p.get("status") == "building"
        ]
        if early:
            raise InvariantError(
                f"never BuildBeforeLock: launched {', '.join(map(str, early))} before the acceptance tests were locked"
            )
    if s == "planning" and not approvals["spec"]:
        raise InvariantError("never PlanWithoutSpec: entered planning without approved spec")
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


def needs_data(run: dict[str, Any]) -> bool:
    """True when a PBI that has not passed needs files copied in from outside the repository."""
    return any(p.get("data_needs") and p.get("status") != "passed" for p in run.get("pbis", []))


def _approve_plan(run: dict[str, Any]) -> None:
    run["approvals"]["plan"] = True


def _approve_plan_before_data(run: dict[str, Any]) -> None:
    run["approvals"]["plan"] = True
    run["data_ready"] = False


def _data_in_place(run: dict[str, Any]) -> None:
    run["data_ready"] = True


def _start_replan(run: dict[str, Any]) -> None:
    _clear_plan_approval(run)
    run["data_ready"] = False
    run["return_state"] = None
    run.pop("return_subphase", None)


def _clear_plan_approval(run: dict[str, Any]) -> None:
    run["approvals"]["plan"] = False
    run["approvals"].pop("plan_sends", None)


def _approve_fix_list(run: dict[str, Any]) -> None:
    run["approvals"]["fix_list"] = True


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
    # OMM cache: ask to scan when no usable scan exists
    Transition("surveying", "omm_missing", "asking_omm"),
    Transition("asking_omm", "omm_scan", "scanning_omm"),
    Transition("asking_omm", "omm_skip", "surveying"),
    Transition("scanning_omm", "omm_scanned", "surveying"),
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
    Transition(
        "awaiting_plan_approval",
        "plan_approved",
        "building",
        guard=lambda r: not needs_data(r) and not stage_pending(r),
        effect=_approve_plan,
    ),
    Transition(
        "awaiting_plan_approval",
        "plan_approved",
        "building_acceptance",
        guard=lambda r: not needs_data(r) and stage_pending(r),
        effect=_approve_plan,
    ),
    Transition(
        "awaiting_plan_approval",
        "plan_approved",
        "preparing_data",
        guard=needs_data,
        effect=_approve_plan_before_data,
    ),
    Transition(
        "preparing_data",
        "data_ready",
        "building",
        guard=lambda r: r["approvals"]["plan"] and not stage_pending(r),
        effect=_data_in_place,
    ),
    Transition(
        "preparing_data",
        "data_ready",
        "building_acceptance",
        guard=lambda r: r["approvals"]["plan"] and stage_pending(r),
        effect=_data_in_place,
    ),
    # Acceptance stage (acceptance_first only): build the acceptance and stub PBIs, then check them Red.
    # A requirement that fails the wrong way is written again; once every one passed or was left unresolved, build.
    Transition("building_acceptance", "worker_returned", "building_acceptance", guard=lambda r: r["approvals"]["plan"]),
    Transition("building_acceptance", "acceptance_built", "checking_red", guard=lambda r: r["approvals"]["plan"]),
    Transition("checking_red", "red_rebuild", "building_acceptance", guard=lambda r: r["approvals"]["plan"]),
    Transition("checking_red", "red_done", "building", guard=lambda r: r["approvals"]["plan"] and not stage_pending(r)),
    # Once the Red check is done for every requirement, the planner reads the acceptance test files once.
    Transition(
        "checking_red",
        "red_done",
        "reviewing_acceptance",
        guard=lambda r: r["approvals"]["plan"] and not red_pending(r) and review_pending(r),
    ),
    # A file the planner says to fix is built again by its acceptance PBI and checked Red again.
    Transition("reviewing_acceptance", "acceptance_rebuild", "building_acceptance", guard=lambda r: r["approvals"]["plan"]),
    Transition(
        "reviewing_acceptance",
        "acceptance_reviewed",
        "building",
        guard=lambda r: r["approvals"]["plan"] and not stage_pending(r),
    ),
    # Building & Checking & Auto-fix & Rebuild
    Transition("building", "worker_returned", "building", guard=lambda r: r["approvals"]["plan"]),
    Transition("building", "wave_built", "checking", guard=lambda r: r["approvals"]["plan"]),
    Transition("building", "no_buildable_pbis_review", "reviewing", guard=lambda r: r["counters"]["review_count"] == 0),
    Transition("building", "no_buildable_pbis_report", "reporting"),
    # A failed check is carried over: a fix PBI goes before the PBIs left, or, after the last wave,
    # the fix stage fixes it. Only a fix PBI that failed past its tries asks the user.
    Transition("checking", "carried_next", "building", guard=lambda r: r["approvals"]["plan"]),
    Transition("checking", "carried_last", "reviewing", guard=lambda r: r["counters"]["review_count"] == 0),
    Transition("checking", "check_failed", "asking_rebuild"),
    Transition("asking_rebuild", "rebuild_retry", "building", guard=lambda r: r["approvals"]["plan"]),
    Transition("asking_rebuild", "rebuild_hold", "building", guard=lambda r: r["approvals"]["plan"]),
    Transition("checking", "wave_passed_next", "building", guard=lambda r: r["approvals"]["plan"]),
    Transition("checking", "all_pbis_passed", "reviewing", guard=lambda r: r["counters"]["review_count"] == 0),
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
    Transition("running_e2e", "e2e_skipped", "reporting"),
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
    Transition(
        "asking_plan_broken",
        "replan",
        "planning",
        guard=lambda r: r["approvals"]["spec"] and r.get("return_state") in REPLAN_FROM,
        effect=_start_replan,
    ),
    Transition("asking_sensitive", "resume", "$return_state"),
    Transition("asking_sensitive", "stop", "reporting"),
    Transition("asking_template_switch", "resume", "$return_state"),
    Transition("asking_template_switch", "stop", "reporting"),
    Transition("asking_guard", "resume", "$return_state"),
    Transition("asking_guard", "stop", "reporting"),
)

# States a broken plan can be rebuilt from: before the final review, while PBIs are still being built.
REPLAN_FROM: frozenset[str] = frozenset({"building", "checking"})

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
