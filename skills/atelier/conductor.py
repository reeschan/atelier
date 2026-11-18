"""Atelier v8 conductor CLI: start | next | run | done | answer | status."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from atelier import actions, flow, guards, interview, pages, plan, record, runners, state, survey, template


class ConductorError(Exception):
    """Refused command error returned as exit code 2 with JSON {"error": ...}."""


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ConductorError(message)


def _emit(payload: dict[str, Any]) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


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


def _git_head(root: Path) -> str | None:
    if not _is_git_repo(root):
        return None
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


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


def _load_json_arg(raw: str | None, label: str) -> dict[str, Any] | None:
    if not raw:
        return None
    p = Path(raw)
    text = p.read_text(encoding="utf-8-sig") if p.is_file() else raw
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConductorError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConductorError(f"{label} must be a JSON object")
    return data


def _apply_resolved_template(run: dict[str, Any], tmpl: dict[str, Any]) -> None:
    run["active_template"] = tmpl
    run["limits"] = dict(tmpl["limits"])
    run["counters"]["e2e_limit"] = tmpl["limits"].get("e2e_rounds", 2)


def _normalize_tokens(tokens: Any) -> Any:
    if isinstance(tokens, dict):
        inp = tokens.get("input", tokens.get("input_tokens", tokens.get("inputTokens", "unknown")))
        out = tokens.get("output", tokens.get("output_tokens", tokens.get("outputTokens", "unknown")))
        tot = tokens.get("total", tokens.get("total_tokens", tokens.get("totalTokens", "unknown")))
        if type(inp) is not int or inp < 0:
            inp = "unknown"
        if type(out) is not int or out < 0:
            out = "unknown"
        if type(tot) is not int or tot < 0:
            tot = inp + out if isinstance(inp, int) and isinstance(out, int) else "unknown"
        return {"input": inp, "output": out, "total": tot}
    if type(tokens) is int and tokens >= 0:
        return {"input": "unknown", "output": "unknown", "total": tokens}
    return "unknown"


def _normalize_cost(cost: Any) -> Any:
    if type(cost) in (int, float) and cost >= 0:
        return cost
    return "unknown"


def _try_append_record(root: Path, event: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan record event for secrets (G4) before appending to record.jsonl."""
    line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    hits = guards.scan_secrets(line)
    if hits:
        return hits
    record.append(state.record_path(root), event)
    return []


def _record_launch_event(
    root: Path,
    run: dict[str, Any],
    pending: dict[str, Any],
    *,
    tokens: Any,
    cost: Any,
) -> list[dict[str, Any]]:
    pbi_id = pending.get("pbi")
    pbi_obj = state.get_pbi(run, pbi_id) if pbi_id else None
    level = (pbi_obj.get("level") if pbi_obj else None) or (
        "L2" if pending.get("role") == "worker" and run.get("state") == "fixing" else None
    )
    lid = pending["launch_id"]
    read_map = (run.get("launch_reads") or {}).pop(lid, {})
    event = {
        "type": "launch",
        "launch_id": lid,
        "role": pending["role"],
        "pbi": pbi_id,
        "runner": pending.get("runner", "agent"),
        "model": pending.get("model", ""),
        "effort": pending.get("effort"),
        "level": level,
        "tokens": _normalize_tokens(tokens),
        "cost": _normalize_cost(cost),
        "read": read_map,
    }
    hits = _try_append_record(root, event)
    if not hits:
        if pbi_id:
            run.setdefault("pbi_launches", {}).setdefault(pbi_id, []).append(lid)
        if run.get("state") == "fixing":
            run.setdefault("last_fix_launch_ids", []).append(lid)
    return hits


def _record_guard_check(
    root: Path,
    run: dict[str, Any],
    pending: dict[str, Any],
    violations: list[dict[str, str]],
) -> list[dict[str, Any]]:
    pbi_id = pending.get("pbi")
    pbi_obj = state.get_pbi(run, pbi_id) if pbi_id else None
    level = (pbi_obj.get("level") if pbi_obj else None) or ("L2" if run.get("state") == "fixing" else "none")
    lid = pending.get("launch_id")
    rules = sorted({v["rule"] for v in violations if v.get("rule")})
    event = {
        "type": "check",
        "pbi": pbi_id,
        "rules": rules,
        "stage": "guard",
        "model": pending.get("model", ""),
        "level": level,
        "result": "fail" if rules else "pass",
        "read_launch_ids": [lid] if lid else [],
        "first_pass": False,
    }
    return _try_append_record(root, event)


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

    switch_obj = _load_json_arg(args.switch, "switch")
    components_obj = _load_json_arg(args.components, "components")
    resolved_agents = str(
        (Path(args.agents).expanduser() if args.agents is not None else Path.home() / ".config" / "atelier" / "agents.json").resolve()
    )
    selection = template.resolve_template(args.template, switch_obj, agents_path=resolved_agents)

    run = state.new_run_state(
        root,
        goal_file,
        spec_file,
        template_path=args.template,
        switch=switch_obj,
        agents_path=resolved_agents,
        active_template=selection["template"],
        components=components_obj,
        base_commit=_git_head(root),
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


def _append_check_records_for_wave(root: Path, run: dict[str, Any], result: dict[str, Any], exit_code: int) -> None:
    checked_pbis = [p for p in run.get("pbis", []) if p.get("status") in ("built", "failed")]
    failed_ids = set(result.get("failed_pbis") or [])
    raw_rules = [str(r) for r in (result.get("rules") or [])]
    first_checked = run.setdefault("first_pass_checked_pbis", [])
    pbi_launches = run.get("pbi_launches") or {}

    if not checked_pbis:
        rules = raw_rules or ([] if exit_code == 0 else ["G9"])
        hits = _try_append_record(root, {
            "type": "check",
            "pbi": None,
            "rules": rules,
            "stage": "build",
            "model": "",
            "level": "unknown",
            "result": "pass" if exit_code == 0 else "fail",
            "read_launch_ids": [],
            "first_pass": False,
        })
        if hits:
            raise ConductorError(f"G4: secret detected in check record ({hits[0]['kind']})")
        return

    for pbi in checked_pbis:
        pid = pbi["id"]
        passed = exit_code == 0 or (bool(failed_ids) and pid not in failed_ids)
        is_first = pid not in first_checked
        if is_first:
            first_checked.append(pid)
        rules = [] if passed else (raw_rules or ["G9"])
        model = (pbi.get("last_step") or {}).get("model") or ""
        level = pbi.get("level") or "unknown"
        hits = _try_append_record(root, {
            "type": "check",
            "pbi": pid,
            "rules": rules,
            "stage": "build",
            "model": model,
            "level": level,
            "result": "pass" if passed else "fail",
            "read_launch_ids": list(pbi_launches.get(pid, [])),
            "first_pass": is_first,
        })
        if hits:
            raise ConductorError(f"G4: secret detected in check record ({hits[0]['kind']})")


def _handle_check_done(root: Path, run: dict[str, Any], result: dict[str, Any]) -> None:
    exit_code = result.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ConductorError("G9: check result must include integer 'exit_code'")

    s = run["state"]
    if s == "checking":
        _append_check_records_for_wave(root, run, result, exit_code)
        if exit_code == 0:
            run["last_check_failure"] = ""
            run["last_failed_rules"] = []
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
            run["last_failed_rules"] = [str(r) for r in (result.get("rules") or ["G9"])]
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
        rules = [str(r) for r in (result.get("rules") or ([] if exit_code == 0 else ["G9"]))]
        hits = _try_append_record(root, {
            "type": "check",
            "pbi": None,
            "rules": rules,
            "stage": "fix",
            "model": run.get("last_fix_model", ""),
            "level": "L2",
            "result": "pass" if exit_code == 0 else "fail",
            "read_launch_ids": list(run.get("last_fix_launch_ids") or []),
            "first_pass": False,
        })
        if hits:
            raise ConductorError(f"G4: secret detected in fix check record ({hits[0]['kind']})")
        if exit_code == 0:
            run["last_fix_launch_ids"] = []
            run["fix_groups"] = []
            run["fix_group_index"] = 0
            flow.apply_event(run, "fix_check_passed")
        else:
            run["fix_group_index"] = 0
            run["subphase"] = "build"
    elif s == "running_e2e":
        rules = [str(r) for r in (result.get("rules") or ([] if exit_code == 0 else ["G9"]))]
        hits = _try_append_record(root, {
            "type": "check",
            "pbi": None,
            "rules": rules,
            "stage": "e2e",
            "model": "script",
            "level": "e2e",
            "result": "pass" if exit_code == 0 else "fail",
            "read_launch_ids": [],
            "first_pass": False,
        })
        if hits:
            raise ConductorError(f"G4: secret detected in e2e check record ({hits[0]['kind']})")
        if exit_code == 0:
            run["last_e2e_failure"] = ""
            flow.apply_event(run, "e2e_passed")
        else:
            run["last_e2e_failure"] = (
                result.get("output")
                or result.get("error")
                or result.get("summary")
                or json.dumps(result, ensure_ascii=False)
            )
            run["fix_groups"] = []
            run["fix_group_index"] = 0
            flow.apply_event(run, "e2e_failed")
    else:
        raise ConductorError(f"unexpected check completion in state {s}")


def _build_pbis_from_plan(run: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    wave_list = plan.waves(result)
    tmpl = actions.active_template(run)
    built: list[dict[str, Any]] = []
    for wave_idx, wave_items in enumerate(wave_list):
        for item in wave_items:
            pid = item["id"]
            level = item["level"]
            level_reason = item["level_reason"]
            targets = list(item.get("targets") or [])
            pbi = {
                "id": pid,
                "title": item["title"],
                "goal": item["goal"],
                "depends_on": list(item.get("depends_on") or []),
                "level": level,
                "level_reason": level_reason,
                "reason": level_reason,
                "targets": targets,
                "files": targets,
                "do_not_touch": list(item.get("do_not_touch") or []),
                "checks": list(item.get("checks") or []),
                "facts": list(item.get("facts") or []),
                "acceptance": list(item.get("acceptance") or []),
                "sensitive_candidate": bool(item.get("sensitive_candidate", False)),
                "sensitive_reason": item.get("sensitive_reason") or "",
                "wave": wave_idx,
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


def _validate_completion_before_accept(
    root: Path,
    run: dict[str, Any],
    pending: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if pending["action"] != "launch":
        return
    if any(result.get(flag) for flag in ("plan_broken", "sensitive_detected", "budget_exceeded")):
        return

    s = run["state"]
    if s == "surveying":
        errors = survey.validate_facts(result)
        if errors:
            raise ConductorError(" ".join(errors))
    elif s == "interviewing":
        after_tree = interview.load_tree(root)
        before_tree = run.get("pending_tree_snapshot") or interview.empty_tree()
        errors = interview.validate_interviewer_tree_update(before_tree, after_tree)
        if errors:
            raise ConductorError(" ".join(errors))
        if result.get("done"):
            spec_path = Path(result.get("spec_path") or (state.run_dir(root) / "interview" / "spec.md"))
            if not spec_path.is_file():
                raise ConductorError(f"interview spec file not found: {spec_path}")
            missing = interview.validate_to_spec(spec_path)
            if missing:
                raise ConductorError(f"spec missing required to-spec sections: {', '.join(missing)}")
    elif s == "planning":
        errors = plan.validate_plan(result)
        if errors:
            raise ConductorError(" ".join(errors))
        try:
            plan.waves(result)
        except ValueError as exc:
            raise ConductorError(str(exc)) from exc
    elif s == "reviewing":
        errors = plan.validate_findings(result)
        if errors:
            raise ConductorError(" ".join(errors))
    elif s == "triaging":
        expected_ids = [f["id"] for f in run.get("findings", [])]
        errors = plan.validate_triage(result, expected_ids=expected_ids)
        if errors:
            raise ConductorError(" ".join(errors))


def _handle_launch_done(root: Path, run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> None:
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
        run["pending_tree_snapshot"] = None
        run["counters"]["round"] += 1
        if result.get("done"):
            spec_path = Path(result.get("spec_path") or (state.run_dir(root) / "interview" / "spec.md"))
            run["spec_file"] = str(spec_path.resolve())
            run["pending_interview_questions"] = None
            flow.apply_event(run, "interview_done")
        else:
            after_tree = interview.load_tree(root)
            frontier_qs = interview.frontier(after_tree)
            q_file = result.get("question_file")
            if not q_file or not Path(q_file).is_file():
                q_path = state.run_dir(root) / "interview" / f"round-{run['counters']['round']}-questions.md"
                q_path.parent.mkdir(parents=True, exist_ok=True)
                q_path.write_text(
                    interview.format_questions(frontier_qs, run["counters"]["round"]),
                    encoding="utf-8",
                )
                q_file = str(q_path.resolve())
            run["pending_interview_questions"] = {
                "question_file": str(Path(q_file).resolve()),
                "frontier_ids": [q["id"] for q in frontier_qs],
            }
            run["subphase"] = "ask"
    elif s == "planning":
        run["pbis"] = _build_pbis_from_plan(run, result)
        run["open_questions"] = list(result.get("open_questions") or [])
        plan_file = state.run_dir(root) / "plan.json"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
        fixed_rules = [str(r) for r in (result.get("fixed_rules") or run.get("last_failed_rules") or ["G9"])]
        for rule_id in fixed_rules:
            hits = _try_append_record(root, {"type": "fix", "rule": rule_id, "method": "auto"})
            if hits:
                raise ConductorError(f"G4: secret detected in fix record ({hits[0]['kind']})")
        flow.apply_event(run, "auto_fix_returned")
    elif s == "reviewing":
        kept, held = plan.split_findings(list(result.get("findings") or []))
        run["findings"] = kept
        run["findings_held"] = held
        kept_path = state.run_dir(root) / "results" / "findings_kept.json"
        kept_path.parent.mkdir(parents=True, exist_ok=True)
        kept_path.write_text(
            json.dumps({"schema": plan.SCHEMA_FINDINGS, "findings": kept}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        flow.apply_event(run, "review_returned")
        if not kept:
            run["fix_list"] = []
            run["triage_ask_items"] = []
            run["triage_skip_items"] = []
            flow.apply_event(run, "triaged")
            if not held:
                flow.apply_event(run, "nothing_to_fix")
    elif s == "triaging":
        finding_by_id = {f["id"]: f for f in run.get("findings", [])}
        fix_items: list[dict[str, Any]] = []
        ask_items: list[dict[str, Any]] = []
        skip_items: list[dict[str, Any]] = []
        for item in result.get("items") or []:
            fid = item["id"]
            base = dict(finding_by_id.get(fid, {"id": fid}))
            merged = {
                **base,
                "id": fid,
                "decision": item["decision"],
                "reason": item["reason"],
                "title": base.get("fix") or base.get("why") or fid,
            }
            if item["decision"] == "fix":
                fix_items.append(merged)
            elif item["decision"] == "ask":
                ask_items.append(merged)
            else:
                skip_items.append(merged)
        run["fix_list"] = fix_items
        run["triage_ask_items"] = ask_items
        run["triage_skip_items"] = skip_items
        flow.apply_event(run, "triaged")
    elif s == "fixing":
        fixed_rules = [str(r) for r in (result.get("fixed_rules") or ["review" if run.get("approvals", {}).get("fix_list") else "e2e"])]
        for rule_id in fixed_rules:
            hits = _try_append_record(root, {"type": "fix", "rule": rule_id, "method": "asked"})
            if hits:
                raise ConductorError(f"G4: secret detected in fix record ({hits[0]['kind']})")
        groups = run.get("fix_groups") or []
        if groups:
            run["fix_group_index"] = run.get("fix_group_index", 0) + 1
            if run["fix_group_index"] < len(groups):
                run["subphase"] = "build"
                return
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
    pending_destructive: list[dict[str, str]] | None = None,
    pending_garbled: list[dict[str, str]] | None = None,
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
        "runner": pending.get("runner"),
        "unusable_reason": unusable_reason,
        "violations": violations,
        "pending_destructive": list(pending_destructive or []),
        "pending_garbled": list(pending_garbled or []),
        "detail": "; ".join(parts),
    }
    flow.apply_event(run, "guard_violation")
    return _advance_and_save(root, run)


def _record_result_usage(
    root: Path,
    run: dict[str, Any],
    pending: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
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
    return _record_launch_event(root, run, pending, tokens=tokens, cost=cost)


def _finalize_completion(
    root: Path,
    run: dict[str, Any],
    pending: dict[str, Any],
    result: dict[str, Any],
    *,
    usage_recorded: bool = False,
) -> dict[str, Any]:
    _validate_completion_before_accept(root, run, pending, result)

    if pending["action"] == "launch" and not usage_recorded:
        secret_hits = _record_result_usage(root, run, pending, result)
        if secret_hits:
            return _raise_guard_question(
                root,
                run,
                pending,
                violations=[{"rule": "G4", "detail": f"secret ({h['kind']}) in record line {h['line']}"} for h in secret_hits],
            )

    expected_id = pending.get("launch_id")
    if expected_id:
        run.setdefault("completed_launches", []).append(expected_id)

    if pending["action"] == "check":
        _handle_check_done(root, run, result)
    else:
        _handle_launch_done(root, run, pending, result)

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


def _collect_post_launch_violations(
    root: Path,
    run: dict[str, Any],
    pending: dict[str, Any],
    before: dict[str, Any] | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    violations = guards.compare(root, before, run.get("approved_edits")) if before is not None else []
    pending_destructive: list[dict[str, str]] = []
    pending_garbled: list[dict[str, str]] = []

    if before is None:
        rpath = Path(pending["result"])
        try:
            data = json.loads(rpath.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                violations.append({"rule": "G14", "detail": f"missing or unfinished report: {rpath}"})
        except (OSError, ValueError):
            violations.append({"rule": "G14", "detail": f"missing or unfinished report: {rpath}"})

    if pending.get("role") == "worker":
        if before is not None and _is_git_repo(root):
            after = guards.snapshot(root)
            existing_g5 = {v["detail"] for v in violations if v.get("rule") == "G5"}
            for dep_path in guards.install_attempts(before, after):
                detail = f"dependency file changed: {dep_path}"
                if detail not in existing_g5:
                    violations.append({"rule": "G5", "detail": detail})

        base = run.get("base_commit") or (before.get("head") if before else None)
        if before is not None and _is_git_repo(root):
            approved_g = {(pair[0], pair[1]) for pair in (run.get("approved_garbled") or []) if len(pair) == 2}
            for finding in guards.garbled_changes(root, before, base=base):
                digest = hashlib.sha256(finding["text"].encode("utf-8", "replace")).hexdigest()
                if (finding["path"], digest) not in approved_g:
                    pending_garbled.append({"path": finding["path"], "digest": digest})
                    violations.append({
                        "rule": "G14",
                        "detail": f"garbled text ({finding['reason']}) at {finding['path']}:{finding['line']}",
                    })

        if _is_git_repo(root) and base:
            try:
                destructive = guards.destructive_changes(root, base)
            except RuntimeError as exc:
                violations.append({"rule": "G12", "detail": f"git inspection failed: {exc}"})
            else:
                approved = {(pair[0], pair[1]) for pair in (run.get("approved_destructive") or []) if len(pair) == 2}
                for finding in destructive:
                    if (finding["kind"], finding["path"]) not in approved:
                        pending_destructive.append(finding)
                        violations.append({"rule": "G12", "detail": f"{finding['kind']}: {finding['path']}"})

    return violations, pending_destructive, pending_garbled


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
            violations, pending_destructive, pending_garbled = _collect_post_launch_violations(root, run, pending, before)
            if violations:
                if result_obj is not None:
                    secret_hits = _record_result_usage(root, run, pending, result_obj)
                    if secret_hits:
                        violations.extend({"rule": "G4", "detail": f"secret ({h['kind']}) in record line {h['line']}"} for h in secret_hits)
                _record_guard_check(root, run, pending, violations)
                return _raise_guard_question(
                    root,
                    run,
                    pending,
                    violations=violations,
                    pending_destructive=pending_destructive,
                    pending_garbled=pending_garbled,
                )

    if result_obj is None:
        result_obj = _read_result(result_path)

    if pending["action"] == "launch":
        _validate_completion_before_accept(root, run, pending, result_obj)
        secret_hits = _record_result_usage(root, run, pending, result_obj)
        if secret_hits:
            return _raise_guard_question(
                root,
                run,
                pending,
                violations=[{"rule": "G4", "detail": f"secret ({h['kind']}) in record line {h['line']}"} for h in secret_hits],
            )
        _record_guard_check(root, run, pending, [])
        return _finalize_completion(root, run, pending, result_obj, usage_recorded=True)

    return _finalize_completion(root, run, pending, result_obj)


def _cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    run = state.load_state(root)
    pending = _validate_pending_launch(run, args.launch_id, require_launch=True)
    runner = pending.get("runner")
    if runner == "agent":
        raise ConductorError("runner 'agent' must be launched by the caller and completed with done")

    step = {"runner": runner, "model": pending.get("model", ""), "effort": pending.get("effort")}
    pbi_obj = state.get_pbi(run, pending["pbi"]) if pending.get("pbi") else None
    approved = (
        guards.sends_approved(run, pbi_obj or pending["pbi"], step)
        if pending.get("pbi")
        else (
            guards.sends_approved(run, pending["role"], step)
            or runner in (run.get("approvals", {}).get("role_sends") or {}).get(pending["role"], [])
        )
    )
    if not approved:
        violations = [{"rule": "G3", "detail": f"external send to {runner} for {pending.get('pbi') or pending['role']} is not approved"}]
        _record_guard_check(root, run, pending, violations)
        return _raise_guard_question(root, run, pending, violations=violations)

    prompt_path = Path(pending["prompt"])
    if prompt_path.is_file():
        secret_hits = guards.scan_secrets(prompt_path.read_text(encoding="utf-8-sig"))
        if secret_hits:
            violations = [{"rule": "G4", "detail": f"secret ({h['kind']}) detected at line {h['line']}"} for h in secret_hits]
            _record_guard_check(root, run, pending, violations)
            return _raise_guard_question(root, run, pending, violations=violations)

    spec = runners.launch_spec(pending["role"], step, pending["prompt"], pending["result"], root, agents_path=run.get("agents_path"))
    blocked = actions._forbidden_args_for_launch(run, pending["role"], step, spec)
    if blocked:
        violations = [{"rule": "G11", "detail": f"forbidden args: {', '.join(blocked)}"}]
        _record_guard_check(root, run, pending, violations)
        return _raise_guard_question(root, run, pending, violations=violations)

    before = None
    if _is_git_repo(root):
        before = guards.snapshot(
            root,
            allowed_paths=actions.allowed_paths_for(run, pending["role"], pending.get("pbi")),
            report_path=pending["result"],
        )

    ext = runners.run_external(spec, args.timeout)
    # approve-edit may have been run while the external role was running.
    run["approved_edits"] = state.load_state(root).get("approved_edits") or []
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
    secret_hits = _record_launch_event(root, run, pending, tokens=ext["tokens"], cost=ext["cost"])
    violations, pending_destructive, pending_garbled = _collect_post_launch_violations(root, run, pending, before)
    if secret_hits:
        violations.extend({"rule": "G4", "detail": f"secret ({h['kind']}) in record line {h['line']}"} for h in secret_hits)
    _record_guard_check(root, run, pending, violations)

    if ext.get("unusable_reason") or violations:
        return _raise_guard_question(
            root,
            run,
            pending,
            violations=violations,
            unusable_reason=ext.get("unusable_reason"),
            pending_destructive=pending_destructive,
            pending_garbled=pending_garbled,
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


def _parse_interview_answers(
    tree: dict[str, Any],
    text: str | None,
    reply_obj: dict[str, Any] | None,
) -> dict[str, str] | list[dict[str, Any]]:
    if reply_obj and "answers" in reply_obj:
        return reply_obj["answers"]
    raw_text = (reply_obj.get("text") if reply_obj else text) or ""
    frontier_qs = interview.frontier(tree)
    if not frontier_qs:
        raise ConductorError("no open frontier questions in design tree")

    frontier_ids = [q["id"] for q in frontier_qs]
    frontier_set = set(frontier_ids)
    parsed: dict[str, str] = {}
    line_re = re.compile(r"^\s*(?:[-*]\s*)?(?:\[?([A-Za-z0-9_.-]+)\]?\s*[:：.)])\s*(.+?)\s*$")
    for raw_line in raw_text.splitlines():
        if not raw_line.strip():
            continue
        m = line_re.match(raw_line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key in frontier_set:
            parsed[key] = val
        elif key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(frontier_ids):
                parsed[frontier_ids[idx]] = val

    if not parsed and len(frontier_qs) == 1 and raw_text.strip():
        parsed[frontier_ids[0]] = raw_text.strip()
    if not parsed:
        raise ConductorError("could not map answer text to open frontier questions")
    return parsed


def _include_user_fixes(run: dict[str, Any], include_ids: list[str]) -> None:
    """Add the findings the user decided to fix, whatever triage or the confidence filter decided."""
    candidates: dict[str, dict[str, Any]] = {}
    for item in run.get("findings_held") or []:
        candidates.setdefault(item["id"], dict(item))
    for item in run.get("findings") or []:
        candidates[item["id"]] = dict(item)
    for item in (run.get("triage_skip_items") or []) + (run.get("triage_ask_items") or []):
        candidates[item["id"]] = dict(item)
    unknown = [fid for fid in include_ids if fid not in candidates]
    if unknown:
        raise ConductorError(f"unknown finding ids in include_ids: {', '.join(unknown)}")
    fix_list = run.setdefault("fix_list", [])
    existing_ids = {f.get("id") for f in fix_list}
    for fid in include_ids:
        if fid in existing_ids:
            continue
        item = candidates[fid]
        fix_list.append({
            **item,
            "decision": "fix",
            "reason": "ユーザーが直すと決めた",
            "title": item.get("title") or item.get("fix") or item.get("why") or fid,
        })
        existing_ids.add(fid)


def _apply_level_changes(run: dict[str, Any], reply_obj: dict[str, Any] | None) -> None:
    if not reply_obj:
        return
    changes = reply_obj.get("level_changes") or reply_obj.get("difficulty_changes")
    if not changes:
        return
    tmpl = actions.active_template(run)
    entries: list[tuple[str, str, str | None]] = []
    if isinstance(changes, dict):
        for pid, lvl in changes.items():
            entries.append((str(pid), str(lvl), None))
    elif isinstance(changes, list):
        for item in changes:
            if isinstance(item, dict) and item.get("id") and item.get("level"):
                entries.append((str(item["id"]), str(item["level"]), item.get("reason")))
    for pid, lvl, reason in entries:
        pbi = state.get_pbi(run, pid)
        if pbi is None:
            continue
        pbi["level"] = lvl
        if reason:
            pbi["level_reason"] = reason
            pbi["reason"] = reason
        route_choice = template.choose_route(tmpl, pbi)
        pbi["route"] = route_choice["route"]
        pbi["route_rule"] = route_choice["rule"]
        pbi["route_inputs"] = route_choice["inputs"]
        pbi["route_template"] = route_choice["template"]
        pbi["route_index"] = 0


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
            spec_file = run.get("spec_file")
            if not spec_file or not Path(spec_file).is_file():
                raise ConductorError("cannot skip interview without an existing spec file")
            flow.apply_event(run, "skip_interview")
        elif choice == "interview":
            flow.apply_event(run, "start_interview")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "interviewing":
        tree = interview.load_tree(root)
        answers = _parse_interview_answers(tree, text, reply_obj)
        updated = interview.apply_answers(tree, answers)
        interview.save_tree(root, updated)
        run["pending_interview_questions"] = None
        flow.apply_event(run, "interview_round")
    elif s == "awaiting_spec_approval":
        if choice == "approve":
            flow.apply_event(run, "spec_approved")
        elif choice == "revise":
            flow.apply_event(run, "revise_spec")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "awaiting_plan_approval":
        _apply_level_changes(run, reply_obj)
        if choice == "approve":
            if reply_obj:
                sensitive_ids = list(reply_obj.get("sensitive_pbis") or reply_obj.get("sensitive") or [])
                proceed_ids = list(reply_obj.get("proceed_pbis") or reply_obj.get("proceed_without_prereq") or [])
                if sensitive_ids:
                    state.apply_sensitive_decision(root, run, sensitive_ids, proceed_ids)
            pending_sends = run.pop("pending_plan_sends", None)
            run["approvals"]["plan_sends"] = dict(pending_sends if pending_sends is not None else actions._plan_sends(run))
            state.append_approval_revision(run, action="approved", summary=text or "")
            flow.apply_event(run, "plan_approved")
        elif choice == "revise":
            if reply_obj:
                sensitive_ids = list(reply_obj.get("sensitive_pbis") or reply_obj.get("sensitive") or [])
                proceed_ids = list(reply_obj.get("proceed_pbis") or reply_obj.get("proceed_without_prereq") or [])
                if sensitive_ids:
                    state.apply_sensitive_decision(root, run, sensitive_ids, proceed_ids)
            run["planner_mode"] = "revise"
            run["plan_revision_request"] = text or (reply_obj or {}).get("reason") or (reply_obj or {}).get("summary") or ""
            run["pending_plan_sends"] = None
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
        include_ids = [
            str(fid)
            for fid in (
                (reply_obj or {}).get("approved_ask_ids")
                or (reply_obj or {}).get("include_ids")
                or (reply_obj or {}).get("fix_ids")
                or []
            )
        ]
        if include_ids:
            if choice != "approve":
                raise ConductorError("include_ids needs choice 'approve'")
            _include_user_fixes(run, include_ids)
        if choice == "approve" and run.get("fix_list"):
            run["fix_groups"] = plan.fix_groups(run["fix_list"], max_workers=3)
            run["fix_group_index"] = 0
            flow.apply_event(run, "fix_list_approved")
        elif choice in ("nothing_to_fix", "approve"):
            run["fix_groups"] = []
            run["fix_group_index"] = 0
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
        issue = run.get("guard_issue") or {}
        if choice in ("retry", "resume"):
            if issue.get("rule") == "G3" and issue.get("runner"):
                if issue.get("pbi"):
                    sends = run["approvals"].setdefault("plan_sends", {})
                    lst = sends.setdefault(issue["pbi"], [])
                    if issue["runner"] not in lst:
                        lst.append(issue["runner"])
                elif issue.get("role"):
                    rsends = run["approvals"].setdefault("role_sends", {})
                    lst = rsends.setdefault(issue["role"], [])
                    if issue["runner"] not in lst:
                        lst.append(issue["runner"])
            if issue.get("pending_destructive"):
                approved = run.setdefault("approved_destructive", [])
                for f in issue["pending_destructive"]:
                    pair = [f["kind"], f["path"]]
                    if pair not in approved:
                        approved.append(pair)
            if issue.get("pending_garbled"):
                approved_g = run.setdefault("approved_garbled", [])
                for item in issue["pending_garbled"]:
                    pair = [item["path"], item["digest"]]
                    if pair not in approved_g:
                        approved_g.append(pair)
            run["guard_issue"] = None
            flow.apply_event(run, "resume")
        elif choice == "next_step":
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


def _cmd_approve_edit(args: argparse.Namespace) -> dict[str, Any]:
    """Record files the caller changed with the user's approval, so G1 does not stop the role running meanwhile."""
    root = Path(args.root)
    run = state.load_state(root)
    if not args.reason.strip():
        raise ConductorError("--reason must say what the user approved")
    approved = run.setdefault("approved_edits", [])
    recorded = []
    for path in args.path:
        try:
            entry = guards.approved_edit(root, path)
        except ValueError as exc:
            raise ConductorError(str(exc)) from exc
        entry["reason"] = args.reason
        approved[:] = [e for e in approved if e["path"] != entry["path"]]
        approved.append(entry)
        recorded.append(entry["path"])
    state.save_state(root, run)
    return {"approved_edits": recorded}


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
        "history": run.get("history", []),
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
    p_start.add_argument("--components")
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

    p_ae = sub.add_parser("approve-edit")
    p_ae.add_argument("--root", required=True)
    p_ae.add_argument("--path", required=True, action="append")
    p_ae.add_argument("--reason", required=True)

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
            "approve-edit": _cmd_approve_edit,
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
        pages.PageError,
        interview.InterviewError,
    ) as exc:
        _emit({"error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
