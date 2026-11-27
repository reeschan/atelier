"""Atelier v8 conductor CLI: start | next | run | done | answer | status | drive."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from atelier import acceptance, actions, backlog, eol, flow, guards, interview, locks, pages, plan, record, retro, runners, state, survey, template


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


def _lock_guard(root: Path, run: dict[str, Any]) -> dict[str, Any] | None:
    """The locked files' blobs, signatures and keys every launch after the lock is compared against (G1)."""
    return locks.guard_entry(root, run) if run.get("lock") else None


def _launch_snapshot(root: Path, run: dict[str, Any], act: dict[str, Any]) -> dict[str, Any]:
    """The work root before a launch. A worker may also change existing files outside its paths.

    The fix worker of an unlock may change the acceptance test file the unlock opens (G1 does not guard it for this
    launch); every other locked file stays guarded.
    """
    return guards.snapshot(
        root,
        allowed_paths=actions.allowed_paths_for(run, act["role"], act.get("pbi"), act.get("fix_group")),
        report_path=act["result"],
        outside_edits=actions.outside_edit_rule(run, act["role"], act.get("pbi"), act.get("fix_group")),
        locked=locks.without(_lock_guard(root, run), actions.opened_files(run, act.get("fix_group"))),
    )


def _restore_locked(root: Path, run: dict[str, Any], launch_id: str | None, violations: list[dict[str, str]]) -> dict[str, list[str]]:
    """Put the locked files and keys a launch changed back to the lock commit's content, and keep a record of it.

    A changed signature only stops the launch: the source file stays as the worker wrote it.
    """
    names = locks.restore(root, run, locks.violated(violations))
    keys, failed = locks.restore_keys(root, run, violations)
    if names or keys:
        entry: dict[str, Any] = {"launch_id": launch_id, "files": names}
        if keys:
            entry["keys"] = keys
        run.setdefault("lock_restored", []).append(entry)
    return {"files": names, "keys": keys, "keys_failed": failed, "signatures": locks.changed_signatures(violations)}


def _lock_notes(restored: dict[str, list[str]]) -> list[str]:
    """What the lock did about a launch's violations, for the guard question."""
    notes = []
    if restored["files"]:
        notes.append("鍵のファイルを鍵のコミットの中身に戻した: " + ", ".join(restored["files"]))
    if restored["keys"]:
        notes.append("鍵のキーを鍵のコミットの値に戻した: " + ", ".join(restored["keys"]))
    if restored["keys_failed"]:
        notes.append("鍵のキーを戻せなかった: " + ", ".join(restored["keys_failed"]))
    if restored["signatures"]:
        notes.append(
            "公開の口のシグネチャが鍵と違うので止めた（ソースのファイルは戻していない）: " + ", ".join(restored["signatures"])
        )
    return notes


def _lock_issue_fields(restored: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        "lock_restored": restored["files"],
        "keys_restored": restored["keys"],
        "keys_not_restored": restored["keys_failed"],
        "signatures_changed": restored["signatures"],
    }


def _ensure_snapshot(root: Path, run: dict[str, Any], act: dict[str, Any]) -> None:
    if act.get("action") == "launches" and act.get("runner") == "agent":
        existing = run.get("pending_snapshot")
        if existing is not None and existing.get("launch_id") == act.get("launch_id"):
            return
        # One work root before every launch of the batch; each launch's own paths come in at done.
        run["pending_snapshot"] = (
            {**guards.snapshot(root, locked=_lock_guard(root, run)), "launch_id": act["launch_id"]} if _is_git_repo(root) else None
        )
    elif act.get("action") == "launch" and act.get("runner") == "agent":
        existing = run.get("pending_snapshot")
        if existing is not None and existing.get("launch_id") == act.get("launch_id"):
            return
        if _is_git_repo(root):
            snap = _launch_snapshot(root, run, act)
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
    if pending.get("batch"):
        event["batch"] = pending["batch"]
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
        omm_cache_dir=str(Path(args.omm_cache_dir).expanduser().resolve()) if args.omm_cache_dir else None,
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


def _append_check_records_for_wave(
    root: Path,
    run: dict[str, Any],
    result: dict[str, Any],
    exit_code: int,
) -> None:
    checked_pbis = [p for p in run.get("pbis", []) if p.get("status") in ("built", "failed")]
    failed_ids = set(result.get("failed_pbis") or [])
    # Known (the key is there) even when empty: a failure laid on no PBI is left unresolved and the PBIs pass.
    known = "failed_pbis" in result
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
        passed = exit_code == 0 or (known and pid not in failed_ids)
        # A fix PBI's check is a second try at its carried PBIs' check, never a first pass.
        is_first = pid not in first_checked and pbi.get("kind") != "fix"
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


def _command_outputs(result: dict[str, Any], commands: list[str]) -> dict[str, str]:
    """Each failed command's own output in a check's result.

    The check keeps each failed command's output with its exit code (_run_check). A result without it (written
    another way) is read from the combined output, where each failed command's part follows a "$ <command>" line and
    ends at the next whole line that is another command's header (_check_output); a line that only starts like one
    (for example "$ lint --help" for the command lint) stays in the part. A command whose part cannot be found gets
    the whole output.
    """
    entries = [c for c in result.get("commands") or [] if isinstance(c, dict)]
    own = {str(c.get("command")): c["output"] for c in entries if isinstance(c.get("output"), str)}
    for c in entries:
        # The whole output, kept beside the result (_run_check), names test files that the capped end may not hold.
        whole = _read_output_file(c.get("output_file"))
        if whole is not None:
            own[str(c.get("command"))] = whole
    output = str(result.get("output") or "")
    lines = output.split("\n")
    heads = {head for c in entries for head in (f"$ {c.get('command')}", f"$ {c.get('command')} (ok)")}
    out: dict[str, str] = {}
    for cmd in commands:
        if cmd in own:
            out[cmd] = own[cmd]
            continue
        if f"$ {cmd}" not in lines:
            out[cmd] = output
            continue
        start = lines.index(f"$ {cmd}") + 1
        end = next((i for i in range(start, len(lines)) if lines[i] in heads), len(lines))
        out[cmd] = "\n".join(lines[start:end])
    return out


def _read_output_file(path: Any) -> str | None:
    """A failed command's whole output from the file _run_check kept it in, or None when there is none to read."""
    if not isinstance(path, str) or not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _lay_failed_commands(
    run: dict[str, Any], result: dict[str, Any], check_id: str | None
) -> tuple[set[str], list[str]] | None:
    """Whose failure each failed command of a wave check is, by the failure owners' steps (state.failure_owners).

    (1) the PBIs of the wave with the command in their own checks, else (2) the PBIs of the wave with a test file in
    their targets that the command's output names, else (3) none: the command goes on the run's unresolved list with
    the end of its output and the check's id, and no PBI fails for it. Fix PBIs go by the same steps. Returns (the
    failed PBIs, the failed commands laid on them), or None when the result has no per-command exit codes.
    """
    failing = [str(c.get("command")) for c in result.get("commands") or [] if isinstance(c, dict) and c.get("exit_code")]
    if not failing:
        return None
    checked = [p for p in run.get("pbis", []) if p.get("status") in ("built", "failed")]
    outputs = _command_outputs(result, failing)
    ids: set[str] = set()
    laid: list[str] = []
    for cmd in failing:
        owners, _step = state.failure_owners(checked, cmd, outputs[cmd])
        if owners:
            ids |= set(owners)
            laid.append(cmd)
            continue
        acceptance.add_unresolved(
            run,
            acceptance.CHECK_UNRESOLVED,
            "落ちたコマンドを checks に持つ PBI も、出力に名前が出るテストのファイルを targets に持つ PBI も wave に無い",
            command=cmd,
            output=state.output_tail(outputs[cmd]),
            check=check_id or "",
        )
    return ids, laid


def _carry_failed_check(run: dict[str, Any], failed_commands: list[str]) -> str:
    """Carry a failed wave check over instead of stopping, and name the event that follows.

    The failed PBIs become a carry-over. While PBIs are left, a fix PBI goes before them (a failed fix PBI
    is tried once more one level up); after the last wave the carry-over waits for the fix stage. A fix PBI
    that failed past its tries is left unresolved without asking the user: it, its carry-over's PBIs and the
    PBIs that depend on them are held, and the run goes on with the PBIs that do not.
    """
    output = run.get("last_check_failure") or ""
    failed = [p for p in run.get("pbis", []) if p.get("status") == "failed"]
    tmpl = actions.active_template(run)
    retried: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for fix in [p for p in failed if p.get("kind") == "fix"]:
        co = state.carry_over_of(run, fix["carry_over"])
        if co is None:
            continue
        co["output"] = output
        co["commands"] = failed_commands or co["commands"]
        if co["tries"] >= state.FIX_PBI_TRIES:
            _leave_carry_over_unresolved(run, co, fix)
        else:
            retried.append((fix, co))
    # The PBIs held above are no longer left; what is left is what does not depend on them.
    left = state.any_pending_pbis(run)
    for fix, co in retried:
        if not left:
            fix["status"] = "carried"
        else:
            co["tries"] += 1
            fix["status"] = "pending"
            fix["checks"] = list(co["commands"]) or fix["checks"]
            fix["level"] = _next_level(fix.get("level", "L2"))
            _reroute(tmpl, fix)
    # A failed PBI held above as unresolved stays held; it is not carried over again.
    originals = [p for p in failed if p.get("kind") != "fix" and p.get("status") == "failed"]
    if originals:
        co = state.record_carry_over(run, originals, failed_commands, output)
        if left:
            fix = state.fix_pbi_for(run, co)
            _reroute(tmpl, fix)
            state.insert_fix_pbi(run, co, fix)
            co["tries"] = 1
    return "carried_next" if state.any_pending_pbis(run) else "carried_last"


def _leave_carry_over_unresolved(run: dict[str, Any], co: dict[str, Any], fix: dict[str, Any]) -> dict[str, Any]:
    """Hold a carry-over whose fix PBI failed past its tries and put it on the run's unresolved list."""
    held = state.hold_unresolved(run, co)
    return acceptance.add_unresolved(
        run,
        acceptance.CARRY_OVER_UNRESOLVED,
        f"修正の PBI {fix['id']} が {co['tries']} 回作っても検査に落ちた（2 回目は 1 難度上）",
        carry_over=co["id"],
        fix_pbi=fix["id"],
        pbis=list(co["pbis"]),
        commands=list(co["commands"]),
        output=state.output_tail(co.get("output") or ""),
        held_pbis=held,
    )


def _reroute(tmpl: dict[str, Any], pbi: dict[str, Any]) -> None:
    route_choice = template.choose_route(tmpl, pbi)
    pbi["route"] = route_choice["route"]
    pbi["route_rule"] = route_choice["rule"]
    pbi["route_inputs"] = route_choice["inputs"]
    pbi["route_template"] = route_choice["template"]
    pbi["route_index"] = 0


def _lock_passed_e2e(root: Path, run: dict[str, Any]) -> None:
    """In a locked run, add the E2E files of the e2e PBIs that passed the wave check to the lock: the list is written
    again, the lock commit moves on, and the copy outside the work tree, the commit id and the hash are written again.
    From the next launch on, G1 and the wave check guard those files. A run without acceptance_first has no lock."""
    if not actions.locked_wave(run) or not locks.passed_e2e(run):
        return
    try:
        locks.extend(root, run)
    except state.StateError as exc:
        raise ConductorError(str(exc)) from exc


def _settle_unlock_check(root: Path, run: dict[str, Any], pending: dict[str, Any]) -> None:
    """An unlock's check is done: judge its fixed and added lines from the JUnit XML (not the exit codes; the lock
    commit's run is meant to fail), take the temporary worktree away, and lock the tests again or put the file back
    (actions.settle_unlock_check). The fix stage then goes on: the next unlock's check, the groups left, or the
    final check once."""
    try:
        verdict = actions.settle_unlock_check(root, run, pending)
    except state.StateError as exc:
        raise ConductorError(str(exc)) from exc
    info = pending.get("unlock") or {}
    hits = _try_append_record(root, {
        "type": "check",
        "pbi": None,
        "rules": [] if verdict["passed"] else [acceptance.UNLOCK_RULE],
        "stage": "unlock",
        "model": "script",
        "level": "unlock",
        "result": "pass" if verdict["passed"] else "fail",
        "read_launch_ids": [str(info["launch_id"])] if info.get("launch_id") else [],
        "first_pass": False,
    })
    if hits:
        raise ConductorError(f"G4: secret detected in unlock check record ({hits[0]['kind']})")
    if state.fix_groups_left(run) or run.get("unlock_checks"):
        run["subphase"] = "build"
        return
    flow.apply_event(run, "fixes_built")


def _handle_check_done(
    root: Path, run: dict[str, Any], result: dict[str, Any], pending: dict[str, Any] | None = None
) -> None:
    exit_code = result.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ConductorError("G9: check result must include integer 'exit_code'")

    s = run["state"]
    if s == "checking_red":
        # The Red check passes on the JUnit XML its commands wrote, not on their exit codes: they are meant to fail.
        verdicts = acceptance.judge_red(root, run, pending or {}, result)
        try:
            actions.append_red_records(root, run, verdicts)
        except state.StateError as exc:
            raise ConductorError(str(exc)) from exc
        flow.apply_event(run, acceptance.settle_red(run, verdicts, (pending or {}).get("launch_id")))
    elif s == "checking":
        laid: set[str] = set()
        if (pending or {}).get("wave") is not None:
            # A locked run: the cases and the lock fail the PBIs they are laid on; the other commands as below.
            result = actions.judge_wave(root, run, pending or {}, result)
            exit_code = result["exit_code"]
            laid = set(result.get("case_failed_pbis") or [])
        failed_commands = [
            str(c["command"]) for c in result.get("commands") or [] if isinstance(c, dict) and c.get("exit_code")
        ]
        if exit_code != 0:
            # The PBIs the cases and the lock failed always join the ones the result names, or the ones the failure
            # owners' steps lay the failed commands on. A failure laid on no PBI is left unresolved, and the PBIs
            # pass unless something else fails them. With neither, every PBI of the wave fails (nothing tells which).
            given = {str(p) for p in result.get("failed_pbis") or []}
            owned = None if given else _lay_failed_commands(run, result, (pending or {}).get("launch_id"))
            result = {k: v for k, v in result.items() if k != "failed_pbis"}
            if given:
                result["failed_pbis"] = sorted(given | laid)
            elif owned is not None:
                result["failed_pbis"] = sorted(owned[0] | laid)
                failed_commands = owned[1]
            elif laid:
                result["failed_pbis"] = sorted(laid)
            if "failed_pbis" in result and not result["failed_pbis"]:
                # Only unresolved failures: the wave passes and the run goes on.
                exit_code = 0
        _append_check_records_for_wave(root, run, result, exit_code)
        if exit_code == 0:
            run["last_check_failure"] = ""
            run["last_failed_rules"] = []
            for pbi in run.get("pbis", []):
                if pbi.get("status") in ("built", "failed"):
                    pbi["status"] = "passed"
            state.settle_passed_fix_pbis(run)
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
            # A fix PBI that passed in a failed wave still settles its carry-over.
            state.settle_passed_fix_pbis(run)
            flow.apply_event(run, _carry_failed_check(run, failed_commands))
        _lock_passed_e2e(root, run)
    elif s == "fixing" and (pending or {}).get("stage") == "unlock":
        _settle_unlock_check(root, run, pending or {})
    elif s in ("final_checking", "fixing"):
        # The final check runs once after the fix stage. Passed or failed, the run goes to the report: what failed is
        # left over on the run, and no fix worker is launched again. A run saved while it waited for the old fix
        # check takes that check as its final check.
        if s == "fixing":
            flow.apply_event(run, "fixes_built")
        if (pending or {}).get("final") is not None:
            # A locked run: the locked cases it had to see pass go by their JUnit XML, not the acceptance exit codes.
            result = actions.judge_final(root, run, pending or {}, result)
            exit_code = result["exit_code"]
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
            raise ConductorError(f"G4: secret detected in final check record ({hits[0]['kind']})")
        actions.finish_final_check(run, {**result, "exit_code": exit_code}, list((pending or {}).get("commands") or []) or None)
        run["last_fix_launch_ids"] = []
        state.reset_fix_groups(run, [])
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
        # Passed or failed, E2E goes on one way: a failure joins the retrospective list, never the fix stage.
        try:
            actions.finish_e2e(root, run, {**result, "exit_code": exit_code})
        except retro.RetroError as exc:
            raise ConductorError(str(exc)) from exc
    else:
        raise ConductorError(f"unexpected check completion in state {s}")


KEPT_STATUSES = ("passed", "sensitive")


def _unchanged_built_pbis(run: dict[str, Any], result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The PBIs built and not checked yet that a re-plan leaves as they were: the fields a passed PBI cannot change
    (plan.frozen_changes) and the dependencies are the same in the new plan."""
    new_by_id = {p.get("id"): p for p in result.get("pbis") or [] if isinstance(p, dict)}
    return {
        p["id"]: p
        for p in run.get("pbis", [])
        if p.get("status") == "built"
        and p["id"] in new_by_id
        and not plan.frozen_changes([p], result, [p["id"]])
        and list(new_by_id[p["id"]].get("depends_on") or []) == list(p.get("depends_on") or [])
    }


def _build_pbis_from_plan(run: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """PBIs of a new plan; a re-plan keeps the PBIs that already passed (or left the run) as they were.

    A PBI built and not checked yet that the re-plan leaves as it was keeps its build and goes to the wave's check;
    a changed one, and one that a stop holds or that failed its check, is built again.
    """
    wave_list = plan.waves(result)
    tmpl = actions.active_template(run)
    kept = {p["id"]: p for p in run.get("pbis", []) if p.get("status") in KEPT_STATUSES}
    kept.update(_unchanged_built_pbis(run, result))
    built: list[dict[str, Any]] = []
    for wave_idx, wave_items in enumerate(wave_list):
        for item in wave_items:
            pid = item["id"]
            if pid in kept:
                pbi = {**kept[pid], "wave": wave_idx}
                if pbi.get("kind") == "acceptance" and pbi.get("status") == "passed":
                    # A passed acceptance PBI takes the requirements the new plan gives it, so that it is built again
                    # for the ones whose scenario the plan adds or changes (acceptance.requirement_changes refuses any
                    # other change).
                    pbi.pop("requirements", None)
                    if item.get("requirements"):
                        pbi["requirements"] = list(item["requirements"])
                built.append(pbi)
                continue
            level = item["level"]
            level_reason = item["level_reason"]
            targets, unit_tests = plan.split_targets(item.get("targets"))
            pbi = {
                "id": pid,
                "title": item["title"],
                "goal": item["goal"],
                "depends_on": list(item.get("depends_on") or []),
                "level": level,
                "level_reason": level_reason,
                "reason": level_reason,
                "kind": item.get("kind") or plan.DEFAULT_KIND,
                **({"requirements": list(item["requirements"])} if item.get("requirements") else {}),
                # The backlog entries this PBI takes; they are marked as taken by the run when the plan is approved.
                **({"backlog": list(dict.fromkeys(item["backlog"]))} if item.get("backlog") else {}),
                "targets": targets,
                "files": targets,
                "unit_tests": unit_tests,
                "do_not_touch": list(item.get("do_not_touch") or []),
                "checks": list(item.get("checks") or []),
                "facts": list(item.get("facts") or []),
                "acceptance": list(item.get("acceptance") or []),
                "evidence": list(item.get("evidence") or []),
                "data_needs": [dict(need) for need in item.get("data_needs") or []],
                "sensitive_candidate": bool(item.get("sensitive_candidate", False)),
                "sensitive_reason": item.get("sensitive_reason") or "",
                "wave": wave_idx,
                "status": "pending",
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
    if pending.get("role") == "worker":
        errors = state.worker_result_errors(result)
        if errors:
            raise ConductorError(" ".join(errors))
    if any(result.get(flag) for flag in ("plan_broken", "sensitive_detected", "budget_exceeded")):
        return

    s = run["state"]
    if s == "surveying":
        errors = survey.validate_facts(result, root)
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
        errors = plan.validate_plan(
            result, acceptance_first=bool(run.get("acceptance_first")), backlog_ids=backlog.candidate_ids(run)
        )
        review = result.get("final_review")
        if not errors and review:
            names = survey.review_skill_names(actions.load_facts(root, run))
            if review["skill"] not in names:
                errors.append(f"final_review のスキル {review['skill']!r} が事実の束の review_skills にありません。")
        passed = [p["id"] for p in run.get("pbis", []) if p.get("status") == "passed"]
        if not errors and passed:
            errors = plan.frozen_changes(run.get("pbis", []), result, passed)
        if not errors and passed:
            # Compared with the run as it was when the re-plan started, for a plan revised again before its approval.
            relock = run.get("plan_return_relock") or {}
            errors = acceptance.requirement_changes(run, result, relock.get("owners"), relock.get("scenarios"))
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
    elif s == "reviewing_acceptance":
        errors = plan.validate_acceptance_review(result, [item["file"] for item in acceptance.review_files(run)])
        if errors:
            raise ConductorError(" ".join(errors))
    elif s == "triaging":
        # Every item of the retrospective list, once; an unlock names cases of the lock (or lines added to one of its
        # requirements), and an unlock request's own unlock is checked the same way. An item with no files to fix is
        # not fixed, so a fix worker always has a bounded set of files.
        try:
            listed = retro.load_items(run)
        except state.StateError as exc:
            raise ConductorError(str(exc)) from exc
        errors = plan.validate_triage(
            _with_requested_unlocks(listed, result),
            expected_ids=[item["id"] for item in listed],
            locked_cases=locks.locked_cases(run),
            plan_cases=list(acceptance.case_requirements(run)),
            unscoped_ids=plan.unscoped_ids(listed),
        )
        if errors:
            raise ConductorError(" ".join(errors))


def _with_requested_unlocks(listed: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    """The triage result, with an unlock request's own unlock on its item when the decision fixes or asks it and
    names no unlock of its own (an unlock request asks for the lock to come off those cases)."""
    requested = {item["id"]: retro.requested_unlock(item) for item in listed}
    items = result.get("items")
    if not isinstance(items, list):
        return result
    out: list[Any] = []
    for item in items:
        if (
            isinstance(item, dict)
            and "unlock" not in item
            and isinstance(item.get("id"), str)
            and requested.get(item["id"]) is not None
            and item.get("decision") in plan.UNLOCK_DECISIONS
        ):
            item = {**item, "unlock": requested[item["id"]]}
        out.append(item)
    return {**result, "items": out}


def _record_outside_edits(run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> None:
    """Keep the files a worker changed outside its paths, with the reason it gave, for the report."""
    why_by_file: dict[str, str] = {}
    for item in result.get("outside_edits") or []:
        if isinstance(item, dict) and isinstance(item.get("file"), str):
            why_by_file[item["file"].strip().replace("\\", "/").removeprefix("./")] = str(item.get("why") or "")
    if pending.get("pbi"):
        owner = pending["pbi"]
    elif run.get("fix_groups"):
        group = pending.get("fix_group")
        if group is None:
            group = (state.fix_groups_left(run) or [0])[0]
        owner = f"FIX-{group + 1}"
    else:
        owner = "FIX"
    edits = run.setdefault("outside_edits", [])
    for name in pending.get("outside_edits") or []:
        entry = next((e for e in edits if (e["by"], e["file"]) == (owner, name)), None)
        if entry is None:
            entry = {"by": owner, "file": name, "why": ""}
            edits.append(entry)
        entry["why"] = why_by_file.get(name) or entry["why"]


def _handle_launch_done(root: Path, run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> None:
    if pending.get("role") == "worker":
        _record_outside_edits(run, pending, result)
        worker_pbi = state.get_pbi(run, pending["pbi"]) if pending.get("pbi") else None
        if worker_pbi is not None:
            state.keep_worker_notes(worker_pbi, result)
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
                    "pbi": pending.get("pbi"),
                    "changed_pbis": list(result.get("changed_pbis") or []),
                    "reason": result.get("reason", ""),
                    "needed_files": [str(f) for f in result.get("needed_files") or []],
                }
            flow.apply_event(run, event)
            return

    s = run["state"]
    if s == "surveying":
        run["facts_file"] = str(Path(pending["result"]).resolve())
        decision = survey.decide_acceptance(result, run.get("active_template"), _is_git_repo(root))
        run["acceptance_first"] = decision["enabled"]
        run["acceptance_reason"] = decision["reason"]
        run["acceptance_commands"] = decision["commands"]
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
        before = acceptance.scenarios(run)
        run["pbis"] = _build_pbis_from_plan(run, result)
        run["open_questions"] = list(result.get("open_questions") or [])
        run["scenarios"] = [dict(sc) for sc in result.get("scenarios") or []]
        # A re-plan writes the scenarios anew; a requirement it left as it was keeps being left out of the lock.
        acceptance.keep_unchanged_marks(run, before)
        run["interfaces"] = [dict(i) for i in result.get("interfaces") or []]
        run["plan_checks"] = list(result.get("checks") or [])
        run["plan_e2e"] = list(result.get("e2e") or [])
        run["final_review"] = result.get("final_review")
        plan_file = state.run_dir(root) / "plan.json"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        flow.apply_event(run, "plan_done")
    elif s == "building":
        pbi_id = pending.get("pbi")
        if pbi_id:
            state.mark_pbi_status(run, pbi_id, "built")
            notes = [n for n in result.get("carry_over") or [] if isinstance(n, dict)]
            pbi = state.get_pbi(run, pbi_id)
            if notes and pbi is not None:
                pbi["worker_carry_over"] = notes
            if pbi is not None and pbi.get("kind") == "fix":
                fixed_rules = [str(r) for r in (result.get("fixed_rules") or run.get("last_failed_rules") or ["G9"])]
                for rule_id in fixed_rules:
                    hits = _try_append_record(root, {"type": "fix", "rule": rule_id, "method": "auto"})
                    if hits:
                        raise ConductorError(f"G4: secret detected in fix record ({hits[0]['kind']})")
            if pbi is not None and isinstance(result.get("stop"), dict):
                # A stop (one of the four exceptions) holds this PBI and the PBIs that depend on it; the other PBIs
                # of the wave go on, and the user is asked once nothing is left to launch.
                state.hold_for_stop(run, pbi_id)
        if state.next_buildable_pbi(run) is not None:
            flow.apply_event(run, "worker_returned")
        elif state.stopped_pbis(run):
            flow.apply_event(run, "worker_stopped")
        else:
            flow.apply_event(run, "wave_built")
    elif s == "building_acceptance":
        pbi = state.get_pbi(run, pending.get("pbi") or "")
        if pbi is not None:
            pbi["status"] = "built"
            acceptance.keep_acceptance_files(pbi, result)
            notes = [n for n in result.get("carry_over") or [] if isinstance(n, dict)]
            if notes:
                pbi["worker_carry_over"] = notes
        if acceptance.stage_buildable(run):
            flow.apply_event(run, "worker_returned")
        else:
            flow.apply_event(run, "acceptance_built")
    elif s == "reviewing":
        # The review's findings, the workers' objections, the unresolved items and the plan returns become one
        # retrospective list, written outside the work tree. Only review items are left out for confidence or style.
        # E2E runs next; a failed E2E joins the list before the triage sorts it.
        findings = [f for f in result.get("findings") or [] if isinstance(f, dict)]
        run["findings"] = plan.split_findings(findings)[0]
        kept, dropped = retro.collect(run, findings)
        run["findings_dropped"] = dropped
        try:
            retro.write(root, run, kept, dropped)
        except retro.RetroError as exc:
            raise ConductorError(str(exc)) from exc
        flow.apply_event(run, "review_returned")
    elif s == "reviewing_acceptance":
        flow.apply_event(run, acceptance.settle_review(run, result, actions.active_template(run), pending.get("launch_id")))
    elif s == "triaging":
        try:
            listed = retro.load_items(run)
        except state.StateError as exc:
            raise ConductorError(str(exc)) from exc
        finding_by_id = {item["id"]: item for item in listed}
        locked = locks.locked_cases(run)
        fix_items: list[dict[str, Any]] = []
        ask_items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in _with_requested_unlocks(listed, result).get("items") or []:
            fid = item["id"]
            base = dict(finding_by_id.get(fid, {"id": fid}))
            # An unlock request's own unlock is the decision's only when the decision takes it (above).
            base.pop("unlock", None)
            merged = {
                **base,
                "id": fid,
                "decision": item["decision"],
                "reason": item["reason"],
                "title": base.get("title") or base.get("what") or base.get("fix") or base.get("why") or fid,
            }
            if isinstance(item.get("unlock"), dict) and item["decision"] in plan.UNLOCK_DECISIONS:
                # The lock comes off these cases in the fix stage, once the fix list with this item is approved.
                merged["unlock"] = acceptance.resolve_unlock(run, item["unlock"], locked)
            if item["decision"] == "fix":
                fix_items.append(merged)
            elif item["decision"] == "ask":
                ask_items.append(merged)
            else:
                skipped.append(merged)
        run["fix_list"] = state.carry_over_fix_items(run) + fix_items
        run["triage_ask_items"] = ask_items
        run["triage_skipped"] = skipped
        flow.apply_event(run, "triaged")
    elif s == "fixing":
        fixed_rules = [str(r) for r in (result.get("fixed_rules") or ["review"])]
        for rule_id in fixed_rules:
            hits = _try_append_record(root, {"type": "fix", "rule": rule_id, "method": "asked"})
            if hits:
                raise ConductorError(f"G4: secret detected in fix record ({hits[0]['kind']})")
        group = None
        if run.get("fix_groups"):
            group = pending.get("fix_group")
            if group is None:
                left = state.fix_groups_left(run)
                group = left[0] if left else None
        # What the worker could not fix is left over; the fix stage does not launch it again.
        actions.fix_open_leftovers(run, group, result)
        if run.get("fix_groups"):
            if group is not None:
                state.mark_fix_group_done(run, group)
                # An unlock's fixed lines are checked next, before any other launch (actions.make_unlock_check).
                actions.queue_unlock_check(run, group, pending.get("launch_id"))
            if state.fix_groups_left(run) or run.get("unlock_checks"):
                run["subphase"] = "build"
                return
        # Every fix group is built: the final check runs once, and the fix stage is not entered again.
        flow.apply_event(run, "fixes_built")
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
    restored = _restore_locked(root, run, pending.get("launch_id"), violations)
    parts.extend(_lock_notes(restored))
    if pending.get("role") == "worker":
        # The edits stay in the work tree; a retry's snapshot would no longer see them as changes.
        _record_outside_edits(run, pending, {})
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
        **_lock_issue_fields(restored),
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
        _handle_check_done(root, run, result, pending)
    else:
        if pending.get("role") == "worker":
            _restore_lf(root, run, pending, result)
        _handle_launch_done(root, run, pending, result)

    return _advance_and_save(root, run)


def _restore_lf(root: Path, run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> None:
    """Put back LF in the worker's files that it saved with CRLF, before the checks run on them."""
    allowed = actions.allowed_paths_for(run, "worker", pending.get("pbi"), pending.get("fix_group")) or []
    changed = [f for f in result.get("changed_files") or [] if isinstance(f, str)]
    paths = [p for p in [*allowed, *changed] if p != "docs/pbi/"]
    restored = eol.restore_lf(root, paths)
    if restored:
        done = run.setdefault("eol_restored", [])
        done.extend(p for p in restored if p not in done)


CALLER_ACTIONS = ("omm_scan", "prepare", "review_skill")


def _done_caller_action(root: Path, run: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    """Finish work the caller did itself: an OMM scan, copying approved data, or a review skill."""
    kind = pending["action"]
    if kind == "omm_scan":
        run["omm"] = None
        flow.apply_event(run, "omm_scanned")
    elif kind == "prepare":
        missing = actions.missing_data(root, run)
        if missing:
            raise ConductorError(f"approved data is not in place yet: {', '.join(missing)}")
        flow.apply_event(run, "data_ready")
    else:
        result = _read_result(Path(pending["result"]))
        errors = plan.validate_findings(result)
        if errors:
            raise ConductorError(" ".join(errors))
        hits = _record_result_usage(root, run, {**pending, "runner": "caller", "model": f"skill:{pending['skill']}", "effort": None}, result)
        if hits:
            raise ConductorError(f"G4: secret detected in review record ({hits[0]['kind']})")
        _handle_launch_done(root, run, {**pending, "action": "launch"}, result)
    run.setdefault("completed_launches", []).append(pending["launch_id"])
    return _advance_and_save(root, run)


def _validate_pending_launch(run: dict[str, Any], launch_id: str | None, *, require_launch: bool = False) -> dict[str, Any]:
    pending = run.get("pending_action")
    if pending and pending.get("action") in CALLER_ACTIONS and not require_launch:
        if launch_id and launch_id != pending.get("launch_id"):
            raise ConductorError(f"launch_id mismatch: expected {pending.get('launch_id')}, got {launch_id}")
        return pending
    if not pending or pending.get("action") not in ("launch", "launches", "check"):
        raise ConductorError(f"no pending launch or check in state {run['state']}")
    if require_launch and pending.get("action") not in ("launch", "launches"):
        raise ConductorError(f"no pending launch in state {run['state']}")
    if launch_id and launch_id in run.get("completed_launches", []):
        raise ConductorError(f"G8: launch '{launch_id}' already completed")
    expected_id = pending.get("launch_id")
    if pending["action"] in ("launch", "launches"):
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
    violations = guards.compare(root, before, _load_approved_edits(root), pending.get("launch_id")) if before is not None else []
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
            pending["outside_edits"] = guards.outside_edits(root, before)
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
    if pending["action"] in CALLER_ACTIONS:
        return _done_caller_action(root, run, pending)
    if pending["action"] == "launches":
        if pending.get("runner") != "agent":
            raise ConductorError("an external launch batch is run by run or drive, not completed with done")
        return _finish_batch(root, run, pending, run.get("pending_snapshot"), None, {})

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


STOP_FLAGS = ("plan_broken", "sensitive_detected", "budget_exceeded")


def _read_result_or_none(path: Path) -> dict[str, Any] | None:
    try:
        return _read_result(path) if path.exists() else None
    except ConductorError:
        return None


def _member_label(member: dict[str, Any]) -> str:
    if member.get("pbi"):
        return member["pbi"]
    group = member.get("fix_group")
    return f"FIX-{group + 1}" if group is not None else "FIX"


def _batch_violations(
    root: Path,
    run: dict[str, Any],
    batch: dict[str, Any],
    before: dict[str, Any] | None,
    ran: list[dict[str, Any]],
    results: dict[str, dict[str, Any] | None],
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    """Guard violations of each launch of a batch, compared with the one work root taken before the batch.

    Each launch is compared without the files the other launches own: their targets (or fix group files),
    and the files outside every target that only one other launch reported in outside_edits. A file outside
    every target that two launches reported is a clash for both. The checks of the whole work root run once:
    G12 and G14 findings go to the launch that owns the file, or to every launch when no one does.
    """
    out: dict[str, list[dict[str, str]]] = {m["launch_id"]: [] for m in ran}
    destructive: dict[str, list[dict[str, str]]] = {m["launch_id"]: [] for m in ran}
    garbled: dict[str, list[dict[str, str]]] = {m["launch_id"]: [] for m in ran}
    if before is None:
        for m in ran:
            data = results.get(m["launch_id"])
            if not isinstance(data, dict) or not data:
                out[m["launch_id"]].append({"rule": "G14", "detail": f"missing or unfinished report: {m['result']}"})
        return out, destructive, garbled
    after = guards.snapshot(root)
    approved = _load_approved_edits(root)
    owned = {
        m["launch_id"]: [
            p for p in (actions.allowed_paths_for(run, m["role"], m.get("pbi"), m.get("fix_group")) or []) if p != "docs/pbi/"
        ]
        for m in ran
    }
    changed = {n for n in before["files"].keys() | after["files"].keys() if before["files"].get(n) != after["files"].get(n)}

    def owners_by_target(name: str) -> list[str]:
        return [lid for lid, paths in owned.items() if guards.covered(name, paths)]

    claimers: dict[str, list[str]] = {}
    for m in ran:
        for item in (results.get(m["launch_id"]) or {}).get("outside_edits") or []:
            name = item.get("file") if isinstance(item, dict) else None
            if not isinstance(name, str):
                continue
            name = name.strip().replace("\\", "/").removeprefix("./")
            if name in changed and not owners_by_target(name) and m["launch_id"] not in claimers.get(name, []):
                claimers.setdefault(name, []).append(m["launch_id"])

    for m in ran:
        lid = m["launch_id"]
        mine = {
            **before,
            "allowed_paths": actions.allowed_paths_for(run, m["role"], m.get("pbi"), m.get("fix_group")),
            "outside_edits": actions.outside_edit_rule(run, m["role"], m.get("pbi"), m.get("fix_group")),
            "report_path": m["result"],
            # The file an unlock opens is opened to its own launch only.
            "locked": locks.without(before.get("locked"), actions.opened_files(run, m.get("fix_group"))),
        }
        ignore = [p for other, paths in owned.items() if other != lid for p in paths]
        ignore += [name for name, ids in claimers.items() if len(ids) == 1 and ids[0] != lid]
        violations = guards.compare(root, mine, approved, batch["launch_id"], after=after, ignore=ignore)
        violations += [
            {"rule": "G1", "detail": f"changed by two launches running together: {name}"}
            for name, ids in sorted(claimers.items())
            if len(ids) > 1 and lid in ids
        ]
        if m.get("role") == "worker":
            m["outside_edits"] = guards.outside_edits(root, mine, after=after, ignore=ignore)
            existing_g5 = {v["detail"] for v in violations if v.get("rule") == "G5"}
            for dep_path in guards.install_attempts(before, after):
                detail = f"dependency file changed: {dep_path}"
                if detail not in existing_g5 and not guards.covered(dep_path, ignore):
                    violations.append({"rule": "G5", "detail": detail})
        out[lid] = violations

    def owners(name: str) -> list[str]:
        return owners_by_target(name) or claimers.get(name) or list(out)

    if not any(m.get("role") == "worker" for m in ran):
        return out, destructive, garbled
    base = run.get("base_commit") or before.get("head")
    approved_g = {(pair[0], pair[1]) for pair in (run.get("approved_garbled") or []) if len(pair) == 2}
    for finding in guards.garbled_changes(root, before, base=base):
        digest = hashlib.sha256(finding["text"].encode("utf-8", "replace")).hexdigest()
        if (finding["path"], digest) in approved_g:
            continue
        for lid in owners(finding["path"]):
            garbled[lid].append({"path": finding["path"], "digest": digest})
            out[lid].append({"rule": "G14", "detail": f"garbled text ({finding['reason']}) at {finding['path']}:{finding['line']}"})
    if base:
        try:
            found = guards.destructive_changes(root, base)
        except RuntimeError as exc:
            for lid in out:
                out[lid].append({"rule": "G12", "detail": f"git inspection failed: {exc}"})
        else:
            approved_d = {(pair[0], pair[1]) for pair in (run.get("approved_destructive") or []) if len(pair) == 2}
            for finding in found:
                if (finding["kind"], finding["path"]) in approved_d:
                    continue
                for lid in owners(finding["path"]):
                    destructive[lid].append(finding)
                    out[lid].append({"rule": "G12", "detail": f"{finding['kind']}: {finding['path']}"})
    return out, destructive, garbled


def _run_batch(root: Path, run: dict[str, Any], batch: dict[str, Any], timeout: int | None) -> dict[str, Any]:
    """Start every external launch of a batch, wait for them all, then take their results together."""
    prechecked: dict[str, list[dict[str, str]]] = {}
    specs: dict[str, Any] = {}
    for member in batch["launches"]:
        violations, spec = _recheck_before_run(root, run, member)
        if violations:
            prechecked[member["launch_id"]] = violations
        else:
            specs[member["launch_id"]] = spec
    before = guards.snapshot(root, locked=_lock_guard(root, run)) if _is_git_repo(root) else None
    started = {lid: runners.start_external(spec) for lid, spec in specs.items()}
    outcomes = {lid: runners.finish_external(handle, timeout) for lid, handle in started.items()}
    return _finish_batch(root, run, batch, before, outcomes, prechecked)


def _finish_batch(
    root: Path,
    run: dict[str, Any],
    batch: dict[str, Any],
    before: dict[str, Any] | None,
    outcomes: dict[str, dict[str, Any]] | None,
    prechecked: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    """Take the results of every launch of a batch (see the spec's 一斉起動の結果の受け入れ).

    outcomes holds what each external launch returned; it is None for a batch of the caller's subagents,
    whose usage comes from their result files. Launches in prechecked failed a check before they ran.
    """
    members = list(batch["launches"])
    results = {m["launch_id"]: _read_result_or_none(Path(m["result"])) for m in members}
    violations = {m["launch_id"]: list(prechecked.get(m["launch_id"], [])) for m in members}
    unusable: dict[str, str | None] = {}
    for m in members:
        lid = m["launch_id"]
        if lid in prechecked:
            continue
        if outcomes is not None:
            outcome = outcomes[lid]
            tokens, cost = outcome["tokens"], outcome["cost"]
            unusable[lid] = outcome.get("unusable_reason")
        elif results[lid] is not None:
            tokens = results[lid].get("tokens", results[lid].get("usage", "unknown"))
            cost = results[lid].get("cost_usd", results[lid].get("cost", "unknown"))
        else:
            continue
        state.record_usage(
            run, launch_id=lid, role=m["role"], runner=m.get("runner", "agent"), model=m.get("model", ""),
            pbi_id=m.get("pbi"), tokens=tokens, cost=cost,
        )
        hits = _record_launch_event(root, run, m, tokens=tokens, cost=cost)
        violations[lid] += [{"rule": "G4", "detail": f"secret ({h['kind']}) in record line {h['line']}"} for h in hits]
    ran = [m for m in members if m["launch_id"] not in prechecked]
    found, destructive, garbled = _batch_violations(root, run, batch, before, ran, results)
    for lid, items in found.items():
        violations[lid] += items
    for m in members:
        _record_guard_check(root, run, m, violations[m["launch_id"]])

    bad = [m for m in members if violations[m["launch_id"]] or unusable.get(m["launch_id"])]
    good = [m for m in members if m not in bad]
    for m in good:
        _validate_completion_before_accept(root, run, {**m, "action": "launch"}, results[m["launch_id"]] or {})
    stopped = [m for m in good if any((results[m["launch_id"]] or {}).get(flag) for flag in STOP_FLAGS)]
    accepted = [m for m in good if m not in stopped]

    run.setdefault("batches", []).append({"id": batch["launch_id"], "members": [_member_label(m) for m in members]})
    run.setdefault("completed_launches", []).append(batch["launch_id"])
    for m in accepted:
        run.setdefault("completed_launches", []).append(m["launch_id"])
        if m.get("role") == "worker":
            _restore_lf(root, run, m, results[m["launch_id"]] or {})
        _handle_launch_done(root, run, m, results[m["launch_id"]] or {})
    if bad:
        return _raise_batch_guard(root, run, batch, bad, violations, unusable, destructive, garbled)
    if stopped:
        # One question at a time: the first stop is asked; the PBIs of later stops are built again after it.
        for m in stopped[1:]:
            run.setdefault("completed_launches", []).append(m["launch_id"])
            if m.get("role") == "worker":
                _record_outside_edits(run, m, results[m["launch_id"]] or {})
            pbi = state.get_pbi(run, m["pbi"]) if m.get("pbi") else None
            if pbi is not None and pbi.get("status") == "building":
                pbi["status"] = "pending"
        first = stopped[0]
        run.setdefault("completed_launches", []).append(first["launch_id"])
        _handle_launch_done(root, run, first, results[first["launch_id"]] or {})
    return _advance_and_save(root, run)


def _raise_batch_guard(
    root: Path,
    run: dict[str, Any],
    batch: dict[str, Any],
    bad: list[dict[str, Any]],
    violations: dict[str, list[dict[str, str]]],
    unusable: dict[str, str | None],
    destructive: dict[str, list[dict[str, str]]],
    garbled: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    """One guard question for every launch of a batch that broke a guard rule or could not run."""
    entries: list[dict[str, Any]] = []
    parts: list[str] = []
    for m in bad:
        lid = m["launch_id"]
        if m.get("role") == "worker":
            _record_outside_edits(run, m, {})
        run.setdefault("completed_launches", []).append(lid)
        pbi = state.get_pbi(run, m["pbi"]) if m.get("pbi") else None
        if pbi is not None and pbi.get("status") == "building":
            pbi["status"] = "pending"
        rules = [v.get("rule") for v in violations[lid]]
        entries.append({
            "launch_id": lid,
            "role": m.get("role"),
            "pbi": m.get("pbi"),
            "fix_group": m.get("fix_group"),
            "runner": m.get("runner"),
            "rule": "G3" if "G3" in rules else None,
            "unusable_reason": unusable.get(lid),
            "violations": violations[lid],
        })
        details = ([f"runner unusable: {unusable[lid]}"] if unusable.get(lid) else []) + [
            f"{v['rule']}: {v['detail']}" for v in violations[lid]
        ]
        parts.append(f"{_member_label(m)}: " + "; ".join(details))
    restored = _restore_locked(root, run, batch["launch_id"], [v for e in entries for v in e["violations"]])
    parts.extend(_lock_notes(restored))
    single = entries[0] if len(entries) == 1 else None
    run["guard_issue"] = {
        "launch_id": batch["launch_id"],
        "role": bad[0].get("role"),
        "pbi": single["pbi"] if single else None,
        "runner": single["runner"] if single else None,
        "rule": single["rule"] if single else None,
        "unusable_reason": single["unusable_reason"] if single else None,
        "violations": [v for e in entries for v in e["violations"]],
        "pending_destructive": [f for m in bad for f in destructive.get(m["launch_id"], [])],
        "pending_garbled": [g for m in bad for g in garbled.get(m["launch_id"], [])],
        "launches": entries,
        **_lock_issue_fields(restored),
        "detail": " / ".join(parts),
    }
    flow.apply_event(run, "guard_violation")
    return _advance_and_save(root, run)


def _recheck_before_run(root: Path, run: dict[str, Any], pending: dict[str, Any]) -> tuple[list[dict[str, str]], Any]:
    """G3, G4 and G11 again right before an external launch runs, and its command when they pass."""
    runner = pending.get("runner")
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
        return [{"rule": "G3", "detail": f"external send to {runner} for {pending.get('pbi') or pending['role']} is not approved"}], None
    prompt_path = Path(pending["prompt"])
    if prompt_path.is_file():
        secret_hits = guards.scan_secrets(prompt_path.read_text(encoding="utf-8-sig"))
        if secret_hits:
            return [{"rule": "G4", "detail": f"secret ({h['kind']}) detected at line {h['line']}"} for h in secret_hits], None
    spec = runners.launch_spec(pending["role"], step, pending["prompt"], pending["result"], root, agents_path=run.get("agents_path"))
    blocked = actions._forbidden_args_for_launch(run, pending["role"], step, spec)
    if blocked:
        return [{"rule": "G11", "detail": f"forbidden args: {', '.join(blocked)}"}], None
    return [], spec


def _cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    run = state.load_state(root)
    pending = _validate_pending_launch(run, args.launch_id, require_launch=True)
    runner = pending.get("runner")
    if runner == "agent":
        raise ConductorError("runner 'agent' must be launched by the caller and completed with done")
    if pending["action"] == "launches":
        return _run_batch(root, run, pending, args.timeout)

    violations, spec = _recheck_before_run(root, run, pending)
    if violations:
        _record_guard_check(root, run, pending, violations)
        return _raise_guard_question(root, run, pending, violations=violations)

    before = None
    if _is_git_repo(root):
        before = _launch_snapshot(root, run, pending)

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


def _start_planner_revision(
    run: dict[str, Any],
    *,
    request: str,
    action: str,
    changed: list[str],
    summary: str,
    plan_return: str | None,
) -> None:
    """Send a run back to the planner's revise on the user's answer (the broken-plan or the plan-return question).

    The passed PBIs stay as they are (_validate_completion_before_accept refuses a plan that changes them), the open
    carry-overs go into the re-plan with the PBIs they hold, and the revised plan is approved again. In an acceptance-first run the
    scenarios as they are now are kept, so that the approval can tell which requirements the new plan changed and
    lock the tests again (_reopen_after_replan).
    """
    for co in state.open_carry_overs(run):
        co["status"] = "replanned"
    if run.get("acceptance_first") and run.get("lock"):
        run["plan_return_relock"] = {
            "plan_return": plan_return,
            "scenarios": copy.deepcopy(acceptance.scenarios(run)),
            "owners": acceptance.requirement_owners(run),
        }
    run["planner_mode"] = "revise"
    run["plan_revision_request"] = request
    run["pending_plan_sends"] = None
    state.append_approval_revision(run, action=action, changed_pbis=changed, summary=summary)


def _plan_return_request(run: dict[str, Any], record: dict[str, Any], text: str | None) -> str:
    """What the planner is asked to change when the user fixes the plan after a worker's stop."""
    lines = [
        f"The worker on {stop['pbi']} ({stop['title']}) returned a stop: exception {stop['exception']} "
        f"({stop['label']}). Reason: {stop['reason']}"
        for stop in record["stops"]
    ]
    lines += [
        f"Objection of {item.get('pbi')} on {item.get('case')}: {item.get('what')} ({item.get('why')})"
        for item in record.get("objections") or []
    ]
    lines.append(f"The run held these PBIs while it asked the user: {', '.join(record['held_pbis']) or 'none'}.")
    if text:
        lines.append(f"User note: {text}")
    lines.append(
        "The user chose to fix the plan. Change only the PBIs that the public interface and the scenarios behind "
        "this stop concern, and keep every other PBI as it is. The passed PBIs cannot change."
    )
    if run.get("acceptance_first"):
        lines.append(
            "Fix the scenarios and the interfaces the stop concerns. The acceptance PBI of each requirement whose "
            "scenario you change is built again, checked Red and read by you again, and the acceptance tests are "
            "then locked again. A passed acceptance PBI keeps every requirement it has; it can take a requirement "
            "whose scenario you add or change, and is built again for it. A passed stub PBI cannot change: to change "
            "a public interface, add a stub PBI for it."
        )
    return "\n".join(lines)


def _reopen_after_replan(run: dict[str, Any]) -> None:
    """When a plan the planner revised in the middle of an acceptance-first run is approved, send the requirements it
    changed back to the acceptance stage and take the lock off.

    The acceptance PBIs of the changed (or added) requirements are built again, checked Red and read by the planner
    again; the next entry into building locks the tests again, with a new lock list, lock commit, copy outside the
    work tree and hash. Nothing happens for an approval that follows no re-plan.
    """
    pending = run.pop("plan_return_relock", None)
    if not pending or not run.get("acceptance_first"):
        return
    reopened = acceptance.reopen_revised(run, pending.get("scenarios") or [])
    before = locks.lift(run, pending.get("plan_return"))
    record = next((r for r in run.get("plan_returns") or [] if r.get("id") == pending.get("plan_return")), None)
    if record is not None:
        record["relock"] = {**reopened, "lock_before": (before or {}).get("commit")}


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
    if s == "asking_omm":
        if choice == "scan":
            flow.apply_event(run, "omm_scan")
        elif choice == "skip":
            flow.apply_event(run, "omm_skip")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "asking_plan_broken" and choice == "replan":
        change = run.pop("pending_plan_change", None) or {}
        lines = [f"The worker on {change.get('pbi') or 'a PBI'} reported that the plan's assumptions failed."]
        if change.get("reason"):
            lines.append(f"Reason: {change['reason']}")
        if change.get("needed_files"):
            lines.append(f"Files the PBI must change outside its targets: {', '.join(change['needed_files'])}")
        if text:
            lines.append(f"User note: {text}")
        lines.append("Rebuild the PBIs that have not passed so that their targets cover what the code needs.")
        _start_planner_revision(
            run,
            request="\n".join(lines),
            action="replan",
            changed=[change["pbi"]] if change.get("pbi") else [],
            summary=change.get("reason", ""),
            plan_return=None,
        )
        flow.apply_event(run, "replan")
    elif s == "asking_plan_return":
        if choice == "continue":
            # The stop goes on as an objection: kept as a plan return for the retrospective, the stopped PBI to the
            # wave's check as its worker built it, and the PBIs that depend on it back to waiting for it.
            record = state.record_plan_return(run, "continue", text or "")
            state.release_stop_hold(run, record["id"])
            flow.apply_event(run, "plan_return_continue")
        elif choice == "replan":
            record = state.record_plan_return(run, "replan", text or "")
            _start_planner_revision(
                run,
                request=_plan_return_request(run, record, text),
                action="plan_return",
                changed=[stop["pbi"] for stop in record["stops"]],
                summary="; ".join(f"{stop['pbi']}: {stop['reason']}" for stop in record["stops"]),
                plan_return=record["id"],
            )
            flow.apply_event(run, "plan_return_replan")
        else:
            raise ConductorError(f"invalid choice '{choice}' for {s}")
    elif s == "asking_interview":
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
            _reopen_after_replan(run)
            try:
                # The backlog entries the plan's PBIs take get this run's id, so later runs do not get them again.
                backlog.take(root, run)
            except (OSError, ValueError) as exc:
                # ValueError covers a backlog file that is not UTF-8 (UnicodeDecodeError); the approval still holds.
                run.setdefault("backlog", {})["take_error"] = str(exc)
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
    elif s == "awaiting_fix_approval":
        if reply_obj:
            approved_ask_ids = set(
                reply_obj.get("approved_ask_ids")
                or reply_obj.get("include_ids")
                or reply_obj.get("fix_ids")
                or []
            )
            if approved_ask_ids and choice != "approve":
                raise ConductorError("include_ids needs choice 'approve'")
            if approved_ask_ids:
                existing_ids = {f.get("id") for f in run.get("fix_list", [])}
                pool = [
                    *(run.get("triage_ask_items") or []),
                    *(run.get("triage_skipped") or []),
                    *(run.get("findings_dropped") or []),
                ]
                known = {item.get("id") for item in pool} | existing_ids
                unknown = sorted(str(i) for i in approved_ask_ids - known)
                if unknown:
                    raise ConductorError(f"unknown finding ids: {', '.join(unknown)}")
                # An item with no files to fix stays off the fix list, so a fix worker always has a bounded scope.
                unscoped = sorted(set(plan.unscoped_ids(pool)) & approved_ask_ids)
                if unscoped:
                    raise ConductorError(f"no files to fix for: {', '.join(unscoped)}")
                for item in pool:
                    if item.get("id") in approved_ask_ids and item.get("id") not in existing_ids:
                        run.setdefault("fix_list", []).append(item)
                        existing_ids.add(item.get("id"))
        excluded = set((reply_obj or {}).get("exclude_ids") or [])
        if excluded:
            unknown = sorted(str(i) for i in excluded - {f.get("id") for f in run.get("fix_list") or []})
            if unknown:
                raise ConductorError(f"unknown fix list ids: {', '.join(unknown)}")
            run["fix_list"] = [f for f in run.get("fix_list") or [] if f.get("id") not in excluded]
            for co in state.open_carry_overs(run):
                if co["id"] in excluded:
                    co["status"] = "dropped"
        if choice == "approve" and run.get("fix_list"):
            if flow.fix_stage_entries(run) or flow.final_check_started(run):
                # The fix stage runs once (for example a run an earlier version saved after its fix stage and E2E).
                raise ConductorError(
                    "this run has already been through its fix stage, and it is not entered again: "
                    "answer nothing_to_fix to go to the report"
                )
            state.reset_fix_groups(run, plan.fix_groups(run["fix_list"], max_workers=template.parallel_limit(run)))
            flow.apply_event(run, "fix_list_approved")
        elif choice in ("nothing_to_fix", "approve"):
            state.reset_fix_groups(run, [])
            flow.apply_event(run, "nothing_to_fix")
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
            for entry in issue.get("launches") or [issue]:
                if entry.get("rule") != "G3" or not entry.get("runner"):
                    continue
                if entry.get("pbi"):
                    sends = run["approvals"].setdefault("plan_sends", {})
                    lst = sends.setdefault(entry["pbi"], [])
                    if entry["runner"] not in lst:
                        lst.append(entry["runner"])
                elif entry.get("role"):
                    rsends = run["approvals"].setdefault("role_sends", {})
                    lst = rsends.setdefault(entry["role"], [])
                    if entry["runner"] not in lst:
                        lst.append(entry["runner"])
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


def _approved_edits_path(root: Path) -> Path:
    return state.run_dir(root) / "approved_edits.jsonl"


def _load_approved_edits(root: Path) -> list[dict[str, Any]]:
    """The approvals approve-edit appended, the latest one per path."""
    path = _approved_edits_path(root)
    if not path.is_file():
        return []
    latest: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            latest[entry["path"]] = entry
    return list(latest.values())


def _cmd_approve_edit(args: argparse.Namespace) -> dict[str, Any]:
    """Record files the caller changed with the user's approval, so G1 does not stop the role running meanwhile.

    Approvals are appended to their own file, not to run.json, because they are recorded while other
    commands may be saving the run state.
    """
    root = Path(args.root)
    run = state.load_state(root)
    if not args.reason.strip():
        raise ConductorError("--reason must say what the user approved")
    pending = run.get("pending_action") or {}
    launch_id = pending.get("launch_id") if pending.get("action") in ("launch", "launches") else None
    entries = []
    for path in args.path:
        try:
            entry = guards.approved_edit(root, path)
        except ValueError as exc:
            raise ConductorError(str(exc)) from exc
        entries.append({**entry, "reason": args.reason, "launch_id": launch_id})
    target = _approved_edits_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
    return {"approved_edits": [e["path"] for e in entries]}


CHECK_OUTPUT_TAIL = 8000


def _tail(text: str, limit: int = CHECK_OUTPUT_TAIL) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def _check_output(outputs: list[tuple[str, int, str]]) -> str:
    """The check's output: the end of each failed command's output, sharing the limit; a passing command's name only.

    With no failure, the end of every command's output, as before.
    """
    failed = [(cmd, out) for cmd, code, out in outputs if code != 0]
    if not failed:
        return _tail("\n".join(f"$ {cmd}\n{_tail(out)}" for cmd, _, out in outputs))
    share = max(CHECK_OUTPUT_TAIL // len(failed), 200)
    lines = [f"$ {cmd}\n{_tail(out, share)}" for cmd, out in failed]
    lines += [f"$ {cmd} (ok)" for cmd, code, _ in outputs if code == 0]
    return "\n".join(lines)


def _run_check(root: Path, act: dict[str, Any], shell: str | None, timeout: int | None) -> int:
    """Run every command of a check, even after a failure, and write the result file.

    The exit code is the first failing command's; each command's own exit code is in "commands", so a
    carry-over names every check that failed, not only the first. A failed command's entry also keeps the end of its
    own output, and its whole output goes to a file beside the result ("output_file"), which the failure owners' steps
    read (_command_outputs), since a test file may be named before the end. A command named in the action's cwds runs in
    that folder instead of the work root (an unlock's check runs the lock commit's state in a temporary worktree).
    """
    exit_code = 0
    outputs: list[tuple[str, int, str]] = []
    codes: list[dict[str, Any]] = []
    cwds = act.get("cwds") or {}
    result_path = Path(act["result"])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    for index, cmd in enumerate(act["commands"], start=1):
        argv: str | list[str] = [shell, "-c", cmd] if shell else cmd
        try:
            proc = subprocess.run(
                argv,
                shell=shell is None,
                cwd=str(cwds.get(cmd) or root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            code, out = 124, f"timed out after {timeout}s"
        except OSError as exc:
            code, out = 127, str(exc)
        outputs.append((cmd, code, out))
        entry: dict[str, Any] = {"command": cmd, "exit_code": code}
        if code != 0:
            entry["output"] = _tail(out)
            whole = result_path.with_name(f"{result_path.stem}.{index}.out")
            whole.write_text(out, encoding="utf-8")
            entry["output_file"] = str(whole)
        codes.append(entry)
        if code != 0 and exit_code == 0:
            exit_code = code
    result_path.write_text(
        json.dumps({"exit_code": exit_code, "output": _check_output(outputs), "commands": codes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return exit_code


def _copy_approved_data(act: dict[str, Any]) -> None:
    for copy in act.get("copies") or []:
        source, dest = Path(copy["source"]).expanduser(), Path(copy["dest"])
        if not source.exists():
            raise ConductorError(f"approved data source does not exist: {source}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest)


def _cmd_drive(args: argparse.Namespace) -> dict[str, Any]:
    """Do every action that needs no model, and stop at the first one that needs the caller."""
    root = Path(args.root)
    steps: list[dict[str, Any]] = []
    if args.done:
        act = _cmd_done(argparse.Namespace(root=args.root, launch_id=args.done))
        steps.append({"action": "done", "launch_id": args.done})
    else:
        act = _cmd_next(argparse.Namespace(root=args.root))
    try:
        for _ in range(args.max_steps):
            kind = act.get("action")
            if kind == "check":
                code = _run_check(root, act, args.shell, args.check_timeout)
                steps.append({"action": "check", "launch_id": act["launch_id"], "exit_code": code})
                act = _cmd_done(argparse.Namespace(root=args.root, launch_id=act["launch_id"]))
            elif kind == "launch" and act.get("runner") != "agent":
                steps.append({k: act.get(k) for k in ("action", "launch_id", "role", "pbi", "runner", "model")})
                act = _cmd_run(argparse.Namespace(root=args.root, launch_id=act["launch_id"], timeout=args.timeout))
            elif kind == "launches" and act.get("runner") != "agent":
                steps.append({
                    "action": "launches",
                    "launch_id": act["launch_id"],
                    "launches": [{k: m.get(k) for k in ("launch_id", "role", "pbi", "runner", "model")} for m in act["launches"]],
                })
                act = _cmd_run(argparse.Namespace(root=args.root, launch_id=act["launch_id"], timeout=args.timeout))
            elif kind == "prepare":
                _copy_approved_data(act)
                steps.append({"action": "prepare", "launch_id": act["launch_id"]})
                act = _cmd_done(argparse.Namespace(root=args.root, launch_id=act["launch_id"]))
            else:
                return {"drove": steps, "next": act}
    except ConductorError as exc:
        raise ConductorError(f"drive stopped after {len(steps)} steps: {exc}") from exc
    raise ConductorError(f"drive stopped after {args.max_steps} steps without needing the caller")


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
    p_start.add_argument("--omm-cache-dir")
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

    p_drive = sub.add_parser("drive")
    p_drive.add_argument("--root", required=True)
    p_drive.add_argument("--done", help="launch id of the agent launch to complete before driving")
    p_drive.add_argument("--shell", help="run each check command as <shell> -c <command> instead of the system shell")
    p_drive.add_argument("--timeout", type=int, default=600, help="seconds per external launch")
    p_drive.add_argument("--check-timeout", type=int, default=None, help="seconds per check command")
    p_drive.add_argument("--max-steps", type=int, default=200)

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
            "drive": _cmd_drive,
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
