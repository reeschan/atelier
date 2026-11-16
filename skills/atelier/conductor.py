"""Atelier v8 conductor CLI: start | next | done | answer | status."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from atelier import actions, flow, state


class ConductorError(Exception):
    """Refused command error returned as exit code 2 with JSON {"error": ...}."""


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ConductorError(message)


def _emit(payload: dict[str, Any]) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _filter_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # TODO(ticket 08): move finding filtering and schema checks to plan.py
    kept: list[dict[str, Any]] = []
    for item in findings:
        confidence = item.get("confidence", 100)
        severity = item.get("severity", "major")
        kind = item.get("kind", "")
        if confidence < 80:
            continue
        if severity in ("minor", "nit") and kind == "style":
            continue
        kept.append(item)
    return kept


def _validate_no_answer_field(result: dict[str, Any]) -> None:
    # G6: role results must never supply user answers
    for forbidden in ("answer", "answers", "user_answer"):
        if forbidden in result:
            raise ConductorError(f"G6: role result must not contain '{forbidden}'")


def _read_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConductorError(f"result file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConductorError(f"invalid result JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConductorError(f"result must be a JSON object: {path}")
    _validate_no_answer_field(data)
    return data


def _advance_and_save(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    run["pending_action"] = None
    act = actions.ensure_pending_action(root, run)
    state.save_state(root, run)
    return act


def _cmd_start(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    if not root.is_dir():
        raise ConductorError(f"work root does not exist: {root}")
    goal_file = Path(args.goal_file)
    if not goal_file.is_file():
        raise ConductorError(f"goal file does not exist: {goal_file}")
    spec_file = Path(args.spec) if args.spec else None
    if spec_file is not None and not spec_file.is_file():
        raise ConductorError(f"spec file does not exist: {spec_file}")

    # G7: refuse overwriting an unfinished run unless --force is passed
    if state.has_run(root):
        try:
            existing = state.load_state(root)
            unfinished = existing.get("state") != "done"
        except state.StateError:
            unfinished = True
        if unfinished and not args.force:
            raise ConductorError("G7: an unfinished run exists; pass --force to overwrite")

    run = state.new_run_state(root, goal_file, spec_file)
    act = actions.ensure_pending_action(root, run)
    state.save_state(root, run)
    return {"schema": run["schema"], "run_id": run["run_id"], "state": run["state"], "next": act}


def _cmd_next(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    run = state.load_state(root)
    before = json.dumps(run, sort_keys=True, ensure_ascii=False)
    act = actions.ensure_pending_action(root, run)
    after = json.dumps(run, sort_keys=True, ensure_ascii=False)
    if before != after:
        state.save_state(root, run)
    return act


def _handle_check_done(run: dict[str, Any], result: dict[str, Any]) -> None:
    exit_code = result.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ConductorError("G9: check result must include integer 'exit_code'")

    s = run["state"]
    if s == "checking":
        if exit_code == 0:
            for pbi in run.get("pbis", []):
                if pbi.get("status") in ("built", "failed"):
                    pbi["status"] = "passed"
            if state.any_pending_pbis(run):
                flow.apply_event(run, "wave_passed_next")
            else:
                flow.apply_event(run, "all_pbis_passed")
        else:
            failed_ids = set(result.get("failed_pbis") or [])
            if failed_ids:
                for pbi in run.get("pbis", []):
                    if pbi.get("status") in ("built", "failed"):
                        pbi["status"] = "failed" if pbi["id"] in failed_ids else "passed"
            else:
                for pbi in run.get("pbis", []):
                    if pbi.get("status") == "built":
                        pbi["status"] = "failed"
            flow.apply_event(run, "check_failed")
    elif s == "fixing":
        if exit_code == 0:
            flow.apply_event(run, "fix_check_passed")
        else:
            run["subphase"] = "build"
    elif s == "running_e2e":
        if exit_code == 0:
            flow.apply_event(run, "e2e_passed")
        else:
            flow.apply_event(run, "e2e_failed")
    else:
        raise ConductorError(f"unexpected check completion in state {s}")


def _handle_launch_done(run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> None:
    # Cross-cutting flags reported by a role
    for flag, event in (
        ("plan_broken", "plan_broken"),
        ("sensitive_detected", "sensitive_detected"),
        ("budget_exceeded", "budget_exceeded"),
    ):
        if result.get(flag):
            if pending.get("pbi"):
                state.mark_pbi_status(run, pending["pbi"], "pending")
            flow.apply_event(run, event)
            return

    s = run["state"]
    if s == "surveying":
        flow.apply_event(run, "survey_done")
    elif s == "interviewing":
        run["counters"]["round"] += 1
        if result.get("done"):
            flow.apply_event(run, "interview_done")
        else:
            run["subphase"] = "ask"
    elif s == "planning":
        raw_pbis = result.get("pbis")
        if raw_pbis is None:
            raw_pbis = [{"id": "PBI-1", "title": "Default PBI", "depends_on": []}]
        if not isinstance(raw_pbis, list) or not raw_pbis:
            raise ConductorError("planner result must include a non-empty 'pbis' list")
        run["pbis"] = [
            {
                "id": item["id"],
                "title": item.get("title", item["id"]),
                "depends_on": list(item.get("depends_on") or []),
                "level": item.get("level", "L2"),
                "reason": item.get("reason", ""),
                "status": "pending",
            }
            for item in raw_pbis
        ]
        flow.apply_event(run, "plan_done")
    elif s == "building":
        pbi_id = pending.get("pbi")
        if pbi_id:
            state.mark_pbi_status(run, pbi_id, "built")
        if state.next_buildable_pbi(run) is not None:
            flow.apply_event(run, "worker_returned")
        else:
            flow.apply_event(run, "wave_built")
    elif s == "auto_fixing":
        pbi_id = pending.get("pbi")
        if pbi_id:
            state.mark_pbi_status(run, pbi_id, "built")
        flow.apply_event(run, "auto_fix_returned")
    elif s == "reviewing":
        findings = _filter_findings(list(result.get("findings") or []))
        run["findings"] = findings
        flow.apply_event(run, "review_returned")
        if not findings:
            run["fix_list"] = []
            flow.apply_event(run, "triaged")
            flow.apply_event(run, "nothing_to_fix")
    elif s == "triaging":
        run["fix_list"] = list(result.get("fix_list") or [])
        flow.apply_event(run, "triaged")
    elif s == "fixing":
        run["subphase"] = "check"
    else:
        raise ConductorError(f"unexpected launch completion in state {s}")


def _cmd_done(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    run = state.load_state(root)
    pending = run.get("pending_action")
    if not pending or pending.get("action") not in ("launch", "check"):
        raise ConductorError(f"no pending launch or check in state {run['state']}")

    launch_id = args.launch_id
    if launch_id and launch_id in run.get("completed_launches", []):
        raise ConductorError(f"G8: launch '{launch_id}' already completed")

    expected_id = pending.get("launch_id")
    if pending["action"] == "launch":
        if not launch_id or launch_id != expected_id:
            raise ConductorError(f"launch_id mismatch: expected {expected_id}, got {launch_id}")
    elif launch_id and launch_id != expected_id:
        raise ConductorError(f"launch_id mismatch: expected {expected_id}, got {launch_id}")

    result = _read_result(Path(pending["result"]))
    if expected_id:
        run.setdefault("completed_launches", []).append(expected_id)

    if pending["action"] == "check":
        _handle_check_done(run, result)
    else:
        _handle_launch_done(run, pending, result)

    return _advance_and_save(root, run)


def _record_answer_file(root: Path, run: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    # G6: conductor answer is the sole writer of answer files
    run["counters"]["answer_seq"] += 1
    seq = run["counters"]["answer_seq"]
    ans_path = state.run_dir(root) / "answers" / f"{seq:04d}-{kind}.json"
    ans_path.parent.mkdir(parents=True, exist_ok=True)
    ans_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _extract_choice_and_text(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.reply:
        reply_path = Path(args.reply)
        if not reply_path.is_file():
            raise ConductorError(f"reply file not found: {reply_path}")
        # TODO(ticket 05): parse structured HTML/markdown reply metadata
        raw = reply_path.read_text(encoding="utf-8").strip()
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj.get("choice"), obj.get("text")
        except json.JSONDecodeError:
            pass
        return None, raw
    return args.choice, args.text


def _cmd_answer(args: argparse.Namespace) -> dict[str, Any]:
    provided = [bool(args.text is not None), bool(args.reply is not None), bool(args.choice is not None)]
    if sum(provided) != 1:
        raise ConductorError("specify exactly one of --text, --reply, or --choice")

    root = Path(args.root)
    run = state.load_state(root)
    pending = run.get("pending_action")
    if not pending or pending.get("action") != "ask":
        raise ConductorError(f"no pending question in state {run['state']}")

    choice, text = _extract_choice_and_text(args)
    _record_answer_file(
        root,
        run,
        pending["kind"],
        {"kind": pending["kind"], "choice": choice, "text": text},
    )

    s = run["state"]
    if s == "asking_interview":
        if choice == "skip":
            flow.apply_event(run, "skip_interview")
        elif choice == "interview":
            flow.apply_event(run, "start_interview")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "interviewing":
        flow.apply_event(run, "interview_round")
    elif s == "awaiting_spec_approval":
        if choice == "approve":
            flow.apply_event(run, "spec_approved")
        elif choice == "revise":
            flow.apply_event(run, "revise_spec")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "awaiting_plan_approval":
        if choice == "approve":
            flow.apply_event(run, "plan_approved")
        elif choice == "revise":
            flow.apply_event(run, "revise_plan")
        elif choice == "sensitive":
            flow.apply_event(run, "sensitive_detected")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "asking_rebuild":
        if choice in ("retry", "retry_up"):
            for pbi in run.get("pbis", []):
                if pbi.get("status") in ("failed", "built"):
                    pbi["status"] = "pending"
            flow.apply_event(run, "rebuild_retry")
        elif choice == "hold":
            for pbi in run.get("pbis", []):
                if pbi.get("status") == "failed":
                    pbi["status"] = "held"
            if not any(p.get("status") == "held" for p in run.get("pbis", [])):
                for pbi in run.get("pbis", []):
                    if pbi.get("status") == "built":
                        pbi["status"] = "held"
            flow.apply_event(run, "rebuild_hold")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "awaiting_fix_approval":
        if choice == "approve" and run.get("fix_list"):
            flow.apply_event(run, "fix_list_approved")
        elif choice in ("nothing_to_fix", "approve"):
            flow.apply_event(run, "nothing_to_fix")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "asking_e2e_limit":
        if choice == "continue":
            flow.apply_event(run, "e2e_continue")
        elif choice == "stop":
            flow.apply_event(run, "stop")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s in flow.CROSS_CUTTING_STATES:
        if choice == "resume":
            flow.apply_event(run, "resume")
        elif choice == "stop":
            flow.apply_event(run, "stop")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    else:
        raise ConductorError(f"cannot answer in state {s}")

    return _advance_and_save(root, run)


def _cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    run = state.load_state(Path(args.root))
    return {
        "schema": run["schema"],
        "run_id": run["run_id"],
        "state": run["state"],
        "counters": run["counters"],
        "approvals": run["approvals"],
        "pbis": run["pbis"],
        "pending_action": run.get("pending_action"),
    }


def _build_parser() -> _JsonArgumentParser:
    parser = _JsonArgumentParser(prog="conductor.py", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--root", required=True)
    p_start.add_argument("--goal-file", required=True)
    p_start.add_argument("--spec")
    p_start.add_argument("--force", action="store_true")

    p_next = sub.add_parser("next")
    p_next.add_argument("--root", required=True)

    p_done = sub.add_parser("done")
    p_done.add_argument("--root", required=True)
    p_done.add_argument("--launch-id")

    p_ans = sub.add_parser("answer")
    p_ans.add_argument("--root", required=True)
    p_ans.add_argument("--text")
    p_ans.add_argument("--reply")
    p_ans.add_argument("--choice")

    p_st = sub.add_parser("status")
    p_st.add_argument("--root", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        handlers = {
            "start": _cmd_start,
            "next": _cmd_next,
            "done": _cmd_done,
            "answer": _cmd_answer,
            "status": _cmd_status,
        }
        _emit(handlers[args.command](args))
        return 0
    except (ConductorError, state.StateError, flow.TransitionError, flow.InvariantError) as exc:
        _emit({"error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
