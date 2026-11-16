"""Build idempotent next actions and role prompts for each state."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import flow, runners, state, survey, template

ASK_CHOICES: dict[str, tuple[str, list[dict[str, str]], str]] = {
    "asking_interview": (
        "interview_or_skip",
        [
            {"id": "skip", "label": "今の仕様で進める"},
            {"id": "interview", "label": "仕様を詰める"},
        ],
        "今の仕様のまま PBI 作成へ進むか、仕様を詰めるかを選んでください。",
    ),
    "awaiting_spec_approval": (
        "spec_approval",
        [
            {"id": "approve", "label": "仕様を承認する"},
            {"id": "revise", "label": "仕様を直す"},
        ],
        "作成された仕様を確認し、承認するか修正するかを選んでください。",
    ),
    "awaiting_plan_approval": (
        "plan_approval",
        [
            {"id": "approve", "label": "計画を承認する"},
            {"id": "revise", "label": "計画を直す"},
            {"id": "sensitive", "label": "sensitive がある"},
        ],
        "PBI の計画を確認し、承認・修正・sensitive の指定を選んでください。",
    ),
    "asking_rebuild": (
        "rebuild",
        [
            {"id": "retry", "label": "同じ経路でもう 1 回"},
            {"id": "retry_up", "label": "1 難度上の経路で"},
            {"id": "hold", "label": "保留にして次の wave へ"},
        ],
        "1 回自動修正しても検査に落ちました。次の対応を選んでください。",
    ),
    "awaiting_fix_approval": (
        "fix_approval",
        [
            {"id": "approve", "label": "直す一覧を承認する"},
            {"id": "nothing_to_fix", "label": "直すものがない（E2E へ）"},
        ],
        "最後の見直しの仕分け結果を確認し、修正へ進むかを選んでください。",
    ),
    "asking_e2e_limit": (
        "e2e_limit",
        [
            {"id": "continue", "label": "修正を 1 回だけ続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "E2E の失敗回数が上限を超えました。1 回だけ続けるか止めるかを選んでください。",
    ),
    "asking_budget": (
        "budget",
        [
            {"id": "resume", "label": "続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "予算の上限に達しそうです。続けるか止めるかを選んでください。",
    ),
    "asking_plan_broken": (
        "plan_broken",
        [
            {"id": "resume", "label": "続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "計画の前提が崩れました。続けるか止めるかを選んでください。",
    ),
    "asking_sensitive": (
        "sensitive",
        [
            {"id": "resume", "label": "外して続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "sensitive な対象が見つかりました。外して続けるか止めるかを選んでください。",
    ),
    "asking_template_switch": (
        "template_switch",
        [
            {"id": "drop", "label": "既定のテンプレートに戻して続ける"},
            {"id": "keep", "label": "切り替えを延長して続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "テンプレートの切り替え期限が過ぎました。対応を選んでください。",
    ),
}


def active_template(run: dict[str, Any]) -> dict[str, Any]:
    tmpl = run.get("active_template")
    if tmpl is not None:
        return tmpl
    loaded = template.load_template(run.get("template_path"), run.get("agents_path"))
    run["active_template"] = loaded
    run["limits"] = dict(loaded["limits"])
    run["counters"]["e2e_limit"] = loaded["limits"].get("e2e_rounds", 2)
    return loaded


def allowed_paths_for(run: dict[str, Any], role: str, pbi_id: str | None) -> list[str] | None:
    if pbi_id:
        pbi = state.get_pbi(run, pbi_id)
        if pbi:
            files = pbi.get("allowed_paths") or pbi.get("files")
            if files:
                return list(files) + ["docs/pbi/"]
        return None
    if role in ("surveyor", "reviewer", "planner"):
        return ["docs/pbi/"]
    return None


def _git_diff_text(run_root: Path) -> str:
    check = subprocess.run(
        ["git", "-C", str(run_root), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check.returncode != 0:
        return "(no git repository)"
    has_head = subprocess.run(
        ["git", "-C", str(run_root), "rev-parse", "--verify", "HEAD"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    cmd = ["git", "-C", str(run_root), "diff"]
    if has_head:
        cmd.append("HEAD")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    diff = res.stdout.strip()
    return diff if diff else "(empty diff)"


def _prompt_body(run_root: Path, run: dict[str, Any], role: str, pbi_id: str | None) -> str:
    s = run["state"]
    if s == "auto_fixing":
        failure_output = run.get("last_check_failure") or "(no failure output)"
        diff_text = _git_diff_text(run_root)
        return (
            f"# Auto-fix for {pbi_id or 'wave'}\n\n"
            "Fix the check failure using only the failure output and diff below.\n\n"
            f"## Failure Output\n{failure_output}\n\n"
            f"## Diff\n{diff_text}\n"
        )
    if s == "planning":
        spec_path = run.get("spec_file") or "none"
        facts_path = state.run_dir(run_root) / "results" / "survey.json"
        return (
            "# Planner\n\n"
            "Read only the approved spec and the fact sheet below to create PBIs.\n\n"
            f"- Spec: {spec_path}\n"
            f"- Facts: {facts_path.resolve()}\n"
        )
    target = f" for {pbi_id}" if pbi_id else ""
    return f"# Role: {role}{target}\n\nFollow the instructions for {role}{target}.\n"


def _write_prompt_text(run_root: Path, launch_id: str, body: str) -> Path:
    prompt_path = state.run_dir(run_root) / "prompts" / f"{launch_id}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(body, encoding="utf-8")
    return prompt_path


def _write_question(run_root: Path, kind: str, body: str) -> Path:
    # TODO(ticket 05): render HTML approval/question page via pages.py
    q_path = state.run_dir(run_root) / "questions" / f"{kind}.txt"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(body + "\n", encoding="utf-8")
    return q_path


def _write_report(run_root: Path, run: dict[str, Any]) -> Path:
    # TODO(ticket 09): generate run report aggregates via record.py
    report_path = state.run_dir(run_root) / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Atelier v8 報告 ({run['run_id']})",
        "",
        f"- 状態: {run['state']}",
        f"- PBI 数: {len(run.get('pbis', []))}",
        f"- 自動修正回数: {run['counters']['auto_fix_count']}",
        f"- 見直し回数: {run['counters']['review_count']}",
        f"- E2E 失敗回数: {run['counters']['e2e_failures']}",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _budget_would_exceed(run: dict[str, Any], prompt_text: str) -> bool:
    if run.pop("budget_approved_next", False):
        return False
    limits = run.get("limits") or active_template(run).get("limits", {})
    budget_tokens = limits.get("budget_tokens")
    budget_usd = limits.get("budget_usd")
    if budget_tokens is None and budget_usd is None:
        return False
    usage = run.get("usage") or {}
    recorded_tokens = usage.get("tokens", 0)
    recorded_cost = usage.get("cost_usd", 0.0)
    est_tokens = max(500, len(prompt_text) // 4 + 500)
    est_cost = round(est_tokens * 0.00001, 6)
    if budget_tokens is not None and (recorded_tokens + est_tokens) > budget_tokens:
        return True
    if budget_usd is not None and (recorded_cost + est_cost) > budget_usd:
        return True
    return False


def _step_for_launch(run: dict[str, Any], tmpl: dict[str, Any], role: str, pbi_id: str | None) -> dict[str, Any]:
    s = run["state"]
    if s == "building" and pbi_id:
        pbi = state.get_pbi(run, pbi_id)
        assert pbi is not None
        if not pbi.get("route"):
            choice = template.choose_route(tmpl, pbi)
            pbi["route"] = choice["route"]
            pbi["route_rule"] = choice["rule"]
            pbi["route_inputs"] = choice["inputs"]
            pbi["route_template"] = choice["template"]
            pbi["route_index"] = 0
        idx = pbi.get("route_index", 0)
        if idx >= len(pbi["route"]):
            raise state.StateError(f"route exhausted for {pbi_id}")
        step = dict(pbi["route"][idx])
        pbi["last_step"] = step
        return step
    if s == "auto_fixing" and pbi_id:
        pbi = state.get_pbi(run, pbi_id)
        if pbi is not None:
            if pbi.get("last_step"):
                return dict(pbi["last_step"])
            if pbi.get("route"):
                return dict(pbi["route"][pbi.get("route_index", 0)])
            choice = template.choose_route(tmpl, pbi)
            return dict(choice["route"][0])
    if s == "fixing":
        if "worker_fix" in tmpl.get("roles", {}):
            return template.role_for(tmpl, "worker_fix")
        choice = template.choose_route(tmpl, {"level": "L2", "level_reason": "指摘またはE2Eの修正"})
        return dict(choice["route"][0])
    return template.role_for(tmpl, role)


def _make_launch(
    run_root: Path,
    run: dict[str, Any],
    role: str,
    *,
    step: dict[str, Any],
    prompt_path: Path,
    result_path: Path,
    pbi_id: str | None = None,
    launch_id: str | None = None,
) -> dict[str, Any]:
    lid = launch_id or state.next_launch_id(run, role)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    spec = runners.launch_spec(role, step, prompt_path, result_path, run_root)
    if step["runner"] == "agent":
        return {
            "action": "launch",
            **spec,
            "pbi": pbi_id,
            "launch_id": lid,
        }
    return {
        "action": "launch",
        "role": role,
        "runner": step["runner"],
        "model": step.get("model", ""),
        "effort": step.get("effort"),
        "prompt": str(prompt_path.resolve()),
        "result": str(result_path.resolve()),
        "command": list(spec),
        "pbi": pbi_id,
        "launch_id": lid,
    }


def _make_check(run_root: Path, run: dict[str, Any], kind: str) -> dict[str, Any]:
    launch_id = state.next_launch_id(run, kind)
    result_path = state.run_dir(run_root) / "results" / f"{launch_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "action": "check",
        "commands": ["python run_tests.py"],
        "result": str(result_path.resolve()),
        "launch_id": launch_id,
    }


def _make_ask(run_root: Path, kind: str, choices: list[dict[str, str]], body: str) -> dict[str, Any]:
    q_path = _write_question(run_root, kind, body)
    return {
        "action": "ask",
        "kind": kind,
        "question": str(q_path.resolve()),
        "page": None,  # TODO(ticket 05): HTML page path from pages.py
        "choices": choices,
    }


def _prepare_survey_prompt(run_root: Path, run: dict[str, Any]) -> tuple[Path, Path]:
    cache_env = os.environ.get("OMM_CACHE_DIR")
    cache_dir = Path(cache_env) if cache_env else run_root.resolve().parent / ".omm-cache"
    omm = survey.omm_context(run_root, cache_dir)
    prompt_path = survey.survey_prompt(run_root, run["goal_file"], run.get("spec_file"), omm)
    result_path = state.run_dir(run_root) / "results" / "survey.json"
    return prompt_path, result_path


def _launch_for_state(run_root: Path, run: dict[str, Any], role: str, pbi_id: str | None = None) -> dict[str, Any]:
    tmpl = active_template(run)
    step = _step_for_launch(run, tmpl, role, pbi_id)
    if run["state"] == "surveying":
        prompt_path, result_path = _prepare_survey_prompt(run_root, run)
        prompt_text = prompt_path.read_text(encoding="utf-8")
        if _budget_would_exceed(run, prompt_text):
            flow.apply_event(run, "budget_exceeded")
            return ensure_pending_action(run_root, run)
        launch_id = state.next_launch_id(run, role)
        return _make_launch(
            run_root,
            run,
            role,
            step=step,
            prompt_path=prompt_path,
            result_path=result_path,
            pbi_id=pbi_id,
            launch_id=launch_id,
        )

    prompt_text = _prompt_body(run_root, run, role, pbi_id)
    if _budget_would_exceed(run, prompt_text):
        flow.apply_event(run, "budget_exceeded")
        return ensure_pending_action(run_root, run)
    launch_id = state.next_launch_id(run, role)
    prompt_path = _write_prompt_text(run_root, launch_id, prompt_text)
    result_path = state.run_dir(run_root) / "results" / f"{launch_id}.json"
    return _make_launch(
        run_root,
        run,
        role,
        step=step,
        prompt_path=prompt_path,
        result_path=result_path,
        pbi_id=pbi_id,
        launch_id=launch_id,
    )


def _guard_ask(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    issue = run.get("guard_issue") or {}
    choices = [{"id": "retry", "label": "同じ設定でもう 1 回試す"}]
    pbi_id = issue.get("pbi")
    pbi = state.get_pbi(run, pbi_id) if pbi_id else None
    if pbi and pbi.get("route") and (pbi.get("route_index", 0) + 1) < len(pbi["route"]):
        choices.append({"id": "next_step", "label": "経路の次の候補で試す"})
    choices.append({"id": "stop", "label": "止めて報告する"})
    detail = issue.get("detail") or "実行後の検査またはランナーで問題が見つかりました。"
    return _make_ask(run_root, "guard", choices, detail)


def ensure_pending_action(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    """Build (if absent) and return the idempotent next action for run['state']."""
    if run.get("pending_action") is not None:
        return run["pending_action"]

    s = run["state"]
    sub = run.get("subphase")

    if s == "surveying":
        action = _launch_for_state(run_root, run, "surveyor")
    elif s == "interviewing":
        if sub == "ask":
            action = _make_ask(
                run_root,
                "interview_round",
                [{"id": "reply", "label": "回答する"}],
                "詰める工程の質問に回答してください。",
            )
        else:
            action = _launch_for_state(run_root, run, "interviewer")
    elif s == "planning":
        action = _launch_for_state(run_root, run, "planner")
    elif s == "building":
        pbi = state.next_buildable_pbi(run)
        if pbi is None:
            has_passed = any(p.get("status") == "passed" for p in run.get("pbis", []))
            if has_passed and run["counters"]["review_count"] == 0:
                flow.apply_event(run, "no_buildable_pbis_review")
            else:
                flow.apply_event(run, "no_buildable_pbis_report")
            return ensure_pending_action(run_root, run)
        if pbi.get("status") == "passed":
            raise state.StateError(f"G8: refusing to launch passed PBI {pbi['id']}")
        action = _launch_for_state(run_root, run, "worker", pbi_id=pbi["id"])
        if action.get("action") == "launch":
            pbi["status"] = "building"
    elif s == "checking":
        action = _make_check(run_root, run, "check")
    elif s == "auto_fixing":
        target = None
        if run.get("auto_fix_pbi"):
            target = state.get_pbi(run, run["auto_fix_pbi"])
        if target is None:
            failed = [p for p in run.get("pbis", []) if p.get("status") == "failed"]
            if not failed:
                failed = [p for p in run.get("pbis", []) if p.get("status") == "built"]
            if not failed:
                raise state.StateError("no failed PBI found for auto_fixing")
            target = failed[0]
        if target.get("status") == "passed":
            raise state.StateError(f"G8: refusing to auto-fix passed PBI {target['id']}")
        action = _launch_for_state(run_root, run, "worker", pbi_id=target["id"])
    elif s == "reviewing":
        action = _launch_for_state(run_root, run, "reviewer")
    elif s == "triaging":
        action = _launch_for_state(run_root, run, "planner")
    elif s == "fixing":
        if sub == "check":
            action = _make_check(run_root, run, "fixcheck")
        else:
            action = _launch_for_state(run_root, run, "worker")
    elif s == "running_e2e":
        action = _make_check(run_root, run, "e2e")
    elif s == "asking_guard":
        action = _guard_ask(run_root, run)
    elif s in ASK_CHOICES:
        kind, choices, body = ASK_CHOICES[s]
        action = _make_ask(run_root, kind, choices, body)
    elif s in ("reporting", "done"):
        report_path = _write_report(run_root, run)
        if s == "reporting":
            flow.apply_event(run, "report_written")
            report_path = _write_report(run_root, run)
        action = {"action": "done", "report": str(report_path.resolve())}
    else:
        raise state.StateError(f"unhandled state: {s}")

    run["pending_action"] = action
    return action
