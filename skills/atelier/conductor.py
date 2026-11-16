"""Atelier v8 conductor CLI: start | next | run | done | answer | status."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from atelier import actions, flow, guards, runners, state, survey, template


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


def _is_git_repo(root: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _ensure_snapshot(root: Path, run: dict[str, Any], act: dict[str, Any]) -> None:
    if act.get("action") == "launch" and act.get("runner") == "agent":
        existing = run.get("pending_snapshot")
        if existing is not None and existing.get("launch_id") == act.get("launch_id"):
            return
        if _is_git_repo(root):
            snap = guards.snapshot(
                root,
                allowed_paths=actions.allowed_paths_for(run, act["role"], act.get("pbi")),
                report_path=act["result"],
            )
            snap["launch_id"] = act["launch_id"]
            run["pending_snapshot"] = snap
        else:
            run["pending_snapshot"] = None
    else:
        run["pending_snapshot"] = None


def _ensure_action_and_snapshot(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    act = actions.ensure_pending_action(root, run)
    _ensure_snapshot(root, run, act)
    return act


def _advance_and_save(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    run["pending_action"] = None
    run["pending_snapshot"] = None
    act = _ensure_action_and_snapshot(root, run)
    state.save_state(root, run)
    return act


def _load_switch(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    p = Path(raw)
    text = p.read_text(encoding="utf-8-sig") if p.is_file() else raw
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConductorError(f"invalid switch JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConductorError("switch must be a JSON object")
    return data


def _apply_resolved_template(run: dict[str, Any], tmpl: dict[str, Any]) -> None:
    run["active_template"] = tmpl
    run["limits"] = dict(tmpl["limits"])
    run["counters"]["e2e_limit"] = tmpl["limits"].get("e2e_rounds", 2)


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

    switch_obj = _load_switch(args.switch)
    selection = template.resolve_template(args.template, switch_obj, agents_path=args.agents)

    run = state.new_run_state(
        root,
        goal_file,
        spec_file,
        template_path=args.template,
        switch=switch_obj,
        agents_path=args.agents,
        active_template=selection["template"],
    )
    if selection["ask"]:
        flow.apply_event(run, "switch_expired")

    act = _ensure_action_and_snapshot(root, run)
    state.save_state(root, run)
    return {"schema": run["schema"], "run_id": run["run_id"], "state": run["state"], "next": act}


def _cmd_next(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    run = state.load_state(root)
    before = json.dumps(run, sort_keys=True, ensure_ascii=False)
    if (
        run.get("pending_action") is None
        and run.get("switch") is not None
        and run["state"] not in flow.CROSS_CUTTING_STATES
        and run["state"] not in flow.TERMINAL_STATES
    ):
        selection = template.resolve_template(
            run.get("template_path"),
            run.get("switch"),
            agents_path=run.get("agents_path"),
        )
        if selection["ask"]:
            flow.apply_event(run, "switch_expired")
        elif selection["template"] is not None:
            _apply_resolved_template(run, selection["template"])
    act = _ensure_action_and_snapshot(root, run)
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
            run["last_check_failure"] = ""
            for pbi in run.get("pbis", []):
                if pbi.get("status") in ("built", "failed"):
                    pbi["status"] = "passed"
            if state.any_pending_pbis(run):
                flow.apply_event(run, "wave_passed_next")
            else:
                flow.apply_event(run, "all_pbis_passed")
        else:
            run["last_check_failure"] = (
                result.get("output")
                or result.get("error")
                or result.get("summary")
                or json.dumps(result, ensure_ascii=False)
            )
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


def _build_pbis_from_plan(run: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_pbis = result.get("pbis")
    if raw_pbis is None:
        raw_pbis = [{"id": "PBI-1", "title": "Default PBI", "depends_on": []}]
    if not isinstance(raw_pbis, list) or not raw_pbis:
        raise ConductorError("planner result must include a non-empty 'pbis' list")

    cand_reasons: dict[str, str] = {}
    for entry in result.get("sensitive_candidates") or []:
        if isinstance(entry, str):
            cand_reasons[entry] = ""
        elif isinstance(entry, dict) and entry.get("id"):
            cand_reasons[entry["id"]] = entry.get("reason", "")

    tmpl = actions.active_template(run)
    built: list[dict[str, Any]] = []
    for item in raw_pbis:
        pid = item["id"]
        level = item.get("level", "L2")
        level_reason = item.get("level_reason") or item.get("reason") or "既定の難度"
        pbi = {
            "id": pid,
            "title": item.get("title", pid),
            "goal": item.get("goal", item.get("title", pid)),
            "depends_on": list(item.get("depends_on") or []),
            "level": level,
            "level_reason": level_reason,
            "reason": item.get("reason") or level_reason,
            "files": list(item.get("files") or item.get("allowed_paths") or []),
            "sensitive_candidate": bool(item.get("sensitive_candidate", pid in cand_reasons)),
            "sensitive_reason": item.get("sensitive_reason") or cand_reasons.get(pid, ""),
            "status": "pending",
            "auto_fix_count": 0,
            "route_index": 0,
        }
        choice = template.choose_route(tmpl, pbi)
        pbi["route"] = choice["route"]
        pbi["route_rule"] = choice["rule"]
        pbi["route_inputs"] = choice["inputs"]
        pbi["route_template"] = choice["template"]
        built.append(pbi)
    return built


def _handle_launch_done(run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> None:
    # Cross-cutting flags reported by a role
    for flag, event in (
        ("plan_broken", "plan_broken"),
        ("sensitive_detected", "sensitive_detected"),
        ("budget_exceeded", "budget_exceeded"),
    ):
        if result.get(flag):
            if pending.get("pbi"):
                pbi = state.get_pbi(run, pending["pbi"])
                if pbi is not None and pbi.get("status") == "building":
                    pbi["status"] = "pending"
            if flag == "plan_broken":
                run["pending_plan_change"] = {
                    "changed_pbis": list(result.get("changed_pbis") or []),
                    "reason": result.get("reason", ""),
                }
            flow.apply_event(run, event)
            return

    s = run["state"]
    if s == "surveying":
        run["facts_file"] = str(Path(pending["result"]).resolve())
        flow.apply_event(run, "survey_done")
    elif s == "interviewing":
        run["counters"]["round"] += 1
        if result.get("done"):
            flow.apply_event(run, "interview_done")
        else:
            run["subphase"] = "ask"
    elif s == "planning":
        run["pbis"] = _build_pbis_from_plan(run, result)
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


def _raise_guard_question(
    root: Path,
    run: dict[str, Any],
    pending: dict[str, Any],
    *,
    violations: list[dict[str, str]],
    unusable_reason: str | None = None,
) -> dict[str, Any]:
    parts: list[str] = []
    if unusable_reason:
        parts.append(f"runner unusable: {unusable_reason}")
    parts.extend(f"{v['rule']}: {v['detail']}" for v in violations)
    if pending.get("launch_id"):
        run.setdefault("completed_launches", []).append(pending["launch_id"])
    pbi_id = pending.get("pbi")
    if pbi_id:
        pbi = state.get_pbi(run, pbi_id)
        if pbi is not None and pbi.get("status") == "building":
            pbi["status"] = "pending"
    run["guard_issue"] = {
        "launch_id": pending.get("launch_id"),
        "role": pending.get("role"),
        "pbi": pbi_id,
        "unusable_reason": unusable_reason,
        "violations": violations,
        "detail": "; ".join(parts),
    }
    flow.apply_event(run, "guard_violation")
    return _advance_and_save(root, run)


def _record_result_usage(run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> None:
    tokens = result.get("tokens", result.get("usage", "unknown"))
    cost = result.get("cost_usd", result.get("cost", "unknown"))
    state.record_usage(
        run,
        launch_id=pending.get("launch_id", ""),
        role=pending.get("role", ""),
        runner=pending.get("runner", "agent"),
        model=pending.get("model", ""),
        pbi_id=pending.get("pbi"),
        tokens=tokens,
        cost=cost,
    )


def _finalize_completion(
    root: Path,
    run: dict[str, Any],
    pending: dict[str, Any],
    result: dict[str, Any],
    *,
    usage_recorded: bool = False,
) -> dict[str, Any]:
    if pending["action"] == "launch" and run["state"] == "surveying":
        errors = survey.validate_facts(result)
        if errors:
            raise ConductorError(" ".join(errors))

    expected_id = pending.get("launch_id")
    if expected_id:
        run.setdefault("completed_launches", []).append(expected_id)

    if pending["action"] == "check":
        _handle_check_done(run, result)
    else:
        if not usage_recorded:
            _record_result_usage(run, pending, result)
        _handle_launch_done(run, pending, result)

    return _advance_and_save(root, run)


def _validate_pending_launch(run: dict[str, Any], launch_id: str | None, *, require_launch: bool = False) -> dict[str, Any]:
    pending = run.get("pending_action")
    if not pending or pending.get("action") not in ("launch", "check"):
        raise ConductorError(f"no pending launch or check in state {run['state']}")
    if require_launch and pending.get("action") != "launch":
        raise ConductorError(f"no pending launch in state {run['state']}")
    if launch_id and launch_id in run.get("completed_launches", []):
        raise ConductorError(f"G8: launch '{launch_id}' already completed")
    expected_id = pending.get("launch_id")
    if pending["action"] == "launch":
        if not launch_id or launch_id != expected_id:
            raise ConductorError(f"launch_id mismatch: expected {expected_id}, got {launch_id}")
    elif launch_id and launch_id != expected_id:
        raise ConductorError(f"launch_id mismatch: expected {expected_id}, got {launch_id}")
    return pending


def _cmd_done(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    run = state.load_state(root)
    pending = _validate_pending_launch(run, args.launch_id)

    result_path = Path(pending["result"])
    result_obj: dict[str, Any] | None = None
    if result_path.exists():
        result_obj = _read_result(result_path)

    if pending["action"] == "launch" and pending.get("runner") == "agent":
        before = run.get("pending_snapshot")
        if before is not None:
            violations = guards.compare(root, before)
            if violations:
                return _raise_guard_question(root, run, pending, violations=violations)

    if result_obj is None:
        result_obj = _read_result(result_path)

    return _finalize_completion(root, run, pending, result_obj)


def _cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    run = state.load_state(root)
    pending = _validate_pending_launch(run, args.launch_id, require_launch=True)
    runner = pending.get("runner")
    if runner == "agent":
        raise ConductorError("runner 'agent' must be launched by the caller and completed with done")

    step = {"runner": runner, "model": pending.get("model", ""), "effort": pending.get("effort")}
    spec = runners.launch_spec(pending["role"], step, pending["prompt"], pending["result"], root)

    before = None
    if _is_git_repo(root):
        before = guards.snapshot(
            root,
            allowed_paths=actions.allowed_paths_for(run, pending["role"], pending.get("pbi")),
            report_path=pending["result"],
        )

    ext = runners.run_external(spec, args.timeout)
    state.record_usage(
        run,
        launch_id=pending["launch_id"],
        role=pending["role"],
        runner=runner,
        model=pending.get("model", ""),
        pbi_id=pending.get("pbi"),
        tokens=ext["tokens"],
        cost=ext["cost"],
    )

    violations = guards.compare(root, before) if before is not None else []
    if before is None:
        rpath = Path(pending["result"])
        try:
            data = json.loads(rpath.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                violations.append({"rule": "G14", "detail": f"missing or unfinished report: {rpath}"})
        except (OSError, ValueError):
            violations.append({"rule": "G14", "detail": f"missing or unfinished report: {rpath}"})

    if ext.get("unusable_reason") or violations:
        return _raise_guard_question(
            root,
            run,
            pending,
            violations=violations,
            unusable_reason=ext.get("unusable_reason"),
        )

    result_obj = _read_result(Path(pending["result"]))
    return _finalize_completion(root, run, pending, result_obj, usage_recorded=True)


def _record_answer_file(root: Path, run: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    # G6: conductor answer is the sole writer of answer files
    run["counters"]["answer_seq"] += 1
    seq = run["counters"]["answer_seq"]
    ans_path = state.run_dir(root) / "answers" / f"{seq:04d}-{kind}.json"
    ans_path.parent.mkdir(parents=True, exist_ok=True)
    ans_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _extract_reply(args: argparse.Namespace) -> tuple[str | None, str | None, dict[str, Any] | None]:
    if args.reply:
        reply_path = Path(args.reply)
        if not reply_path.is_file():
            raise ConductorError(f"reply file not found: {reply_path}")
        # TODO(ticket 05): parse structured HTML/markdown reply metadata
        raw = reply_path.read_text(encoding="utf-8").strip()
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj.get("choice"), obj.get("text"), obj
        except json.JSONDecodeError:
            pass
        return None, raw, None
    return args.choice, args.text, None


def _next_level(level: str) -> str:
    idx = template.LEVELS.index(level) if level in template.LEVELS else 1
    return template.LEVELS[min(idx + 1, len(template.LEVELS) - 1)]


def _cmd_answer(args: argparse.Namespace) -> dict[str, Any]:
    provided = [bool(args.text is not None), bool(args.reply is not None), bool(args.choice is not None)]
    if sum(provided) != 1:
        raise ConductorError("specify exactly one of --text, --reply, or --choice")

    root = Path(args.root)
    run = state.load_state(root)
    pending = run.get("pending_action")
    if not pending or pending.get("action") != "ask":
        raise ConductorError(f"no pending question in state {run['state']}")

    choice, text, reply_obj = _extract_reply(args)
    _record_answer_file(
        root,
        run,
        pending["kind"],
        {"kind": pending["kind"], "choice": choice, "text": text, **({"reply": reply_obj} if reply_obj else {})},
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
            if reply_obj:
                sensitive_ids = list(reply_obj.get("sensitive_pbis") or reply_obj.get("sensitive") or [])
                proceed_ids = list(reply_obj.get("proceed_pbis") or reply_obj.get("proceed_without_prereq") or [])
                if sensitive_ids:
                    state.apply_sensitive_decision(root, run, sensitive_ids, proceed_ids)
            state.append_approval_revision(run, action="approved", summary=text or "")
            flow.apply_event(run, "plan_approved")
        elif choice == "revise":
            flow.apply_event(run, "revise_plan")
        elif choice == "sensitive":
            if reply_obj and (reply_obj.get("sensitive_pbis") or reply_obj.get("sensitive")):
                run["pending_sensitive"] = {
                    "sensitive_pbis": list(reply_obj.get("sensitive_pbis") or reply_obj.get("sensitive") or []),
                    "proceed_pbis": list(reply_obj.get("proceed_pbis") or reply_obj.get("proceed_without_prereq") or []),
                }
            flow.apply_event(run, "sensitive_detected")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "asking_rebuild":
        if choice in ("retry", "retry_up"):
            tmpl = actions.active_template(run)
            for pbi in run.get("pbis", []):
                if pbi.get("status") in ("failed", "built"):
                    pbi["status"] = "pending"
                    pbi["auto_fix_count"] = 0
                    if choice == "retry_up":
                        pbi["level"] = _next_level(pbi.get("level", "L2"))
                        route_choice = template.choose_route(tmpl, pbi)
                        pbi["route"] = route_choice["route"]
                        pbi["route_rule"] = route_choice["rule"]
                        pbi["route_inputs"] = route_choice["inputs"]
                        pbi["route_template"] = route_choice["template"]
                        pbi["route_index"] = 0
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
    elif s == "asking_template_switch":
        if choice == "drop":
            run["switch"] = None
            _apply_resolved_template(run, template.load_template(run.get("template_path"), run.get("agents_path")))
            flow.apply_event(run, "resume")
        elif choice == "keep":
            sw = run.get("switch") or {}
            _apply_resolved_template(run, template.load_template(sw.get("template"), run.get("agents_path")))
            flow.apply_event(run, "resume")
        elif choice == "stop":
            flow.apply_event(run, "stop")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "asking_guard":
        if choice in ("retry", "resume"):
            run["guard_issue"] = None
            flow.apply_event(run, "resume")
        elif choice == "next_step":
            issue = run.get("guard_issue") or {}
            pbi = state.get_pbi(run, issue["pbi"]) if issue.get("pbi") else None
            if not pbi or not pbi.get("route") or (pbi.get("route_index", 0) + 1) >= len(pbi["route"]):
                raise ConductorError("no next route step available")
            pbi["route_index"] = pbi.get("route_index", 0) + 1
            run["guard_issue"] = None
            flow.apply_event(run, "resume")
        elif choice == "stop":
            run["guard_issue"] = None
            flow.apply_event(run, "stop")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s in flow.CROSS_CUTTING_STATES:
        if choice == "resume":
            if s == "asking_budget":
                run["budget_approved_next"] = True
            elif s == "asking_sensitive":
                pending_sens = run.pop("pending_sensitive", None) or {}
                sens_ids = list((reply_obj or {}).get("sensitive_pbis") or (reply_obj or {}).get("sensitive") or pending_sens.get("sensitive_pbis") or [])
                proc_ids = list((reply_obj or {}).get("proceed_pbis") or (reply_obj or {}).get("proceed_without_prereq") or pending_sens.get("proceed_pbis") or [])
                if sens_ids:
                    state.apply_sensitive_decision(root, run, sens_ids, proc_ids)
            elif s == "asking_plan_broken":
                pending_chg = run.pop("pending_plan_change", None) or {}
                changed_pbis = list((reply_obj or {}).get("changed_pbis") or pending_chg.get("changed_pbis") or [])
                summary = (reply_obj or {}).get("summary") or text or pending_chg.get("reason") or ""
                if changed_pbis:
                    state.record_plan_change(run, changed_pbis, summary=summary)
            flow.apply_event(run, "resume")
        elif choice == "stop":
            run.pop("pending_plan_change", None)
            run.pop("pending_sensitive", None)
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
        "limits": run.get("limits"),
        "usage": run.get("usage"),
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
    p_start.add_argument("--template")
    p_start.add_argument("--switch")
    p_start.add_argument("--agents")
    p_start.add_argument("--force", action="store_true")

    p_next = sub.add_parser("next")
    p_next.add_argument("--root", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--root", required=True)
    p_run.add_argument("--launch-id", required=True)
    p_run.add_argument("--timeout", type=int, default=600)

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
            "run": _cmd_run,
            "done": _cmd_done,
            "answer": _cmd_answer,
            "status": _cmd_status,
        }
        _emit(handlers[args.command](args))
        return 0
    except (
        ConductorError,
        state.StateError,
        flow.TransitionError,
        flow.InvariantError,
        template.TemplateError,
    ) as exc:
        _emit({"error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
