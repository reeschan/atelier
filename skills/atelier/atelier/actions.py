"""Build idempotent next actions and placeholder artifacts for each state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import flow, state

# TODO(ticket 02): replace fixed route with template.py route/model resolution
DEFAULT_ROUTE: dict[str, Any] = {"runner": "agent", "model": "opus", "effort": None}

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
}


def _write_prompt(run_root: Path, launch_id: str, role: str, pbi_id: str | None) -> Path:
    # TODO(ticket 04): render role prompt from skills/atelier/prompts/<role>.md
    prompt_path = state.run_dir(run_root) / "prompts" / f"{launch_id}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    target = f" for {pbi_id}" if pbi_id else ""
    prompt_path.write_text(
        f"# Role: {role}{target}\n\nPlaceholder prompt for launch {launch_id}.\n",
        encoding="utf-8",
    )
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


def _make_launch(run_root: Path, run: dict[str, Any], role: str, pbi_id: str | None = None) -> dict[str, Any]:
    launch_id = state.next_launch_id(run, role)
    prompt_path = _write_prompt(run_root, launch_id, role, pbi_id)
    result_path = state.run_dir(run_root) / "results" / f"{launch_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "action": "launch",
        "role": role,
        "runner": DEFAULT_ROUTE["runner"],
        "model": DEFAULT_ROUTE["model"],
        "effort": DEFAULT_ROUTE["effort"],
        "prompt": str(prompt_path.resolve()),
        "result": str(result_path.resolve()),
        "pbi": pbi_id,
        "launch_id": launch_id,
    }


def _make_check(run_root: Path, run: dict[str, Any], kind: str) -> dict[str, Any]:
    # TODO(ticket 03): execute machine checks / E2E directly in conductor or runners.py
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


def ensure_pending_action(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    """Build (if absent) and return the idempotent next action for run['state']."""
    if run.get("pending_action") is not None:
        return run["pending_action"]

    s = run["state"]
    sub = run.get("subphase")

    if s == "surveying":
        action = _make_launch(run_root, run, "surveyor")
    elif s == "interviewing":
        if sub == "ask":
            action = _make_ask(
                run_root,
                "interview_round",
                [{"id": "reply", "label": "回答する"}],
                "詰める工程の質問に回答してください。",
            )
        else:
            action = _make_launch(run_root, run, "interviewer")
    elif s == "planning":
        action = _make_launch(run_root, run, "planner")
    elif s == "building":
        pbi = state.next_buildable_pbi(run)
        if pbi is None:
            # No buildable PBIs remain (e.g. remaining PBIs held/passed)
            has_passed = any(p.get("status") == "passed" for p in run.get("pbis", []))
            if has_passed and run["counters"]["review_count"] == 0:
                flow.apply_event(run, "no_buildable_pbis_review")
            else:
                flow.apply_event(run, "no_buildable_pbis_report")
            return ensure_pending_action(run_root, run)
        if pbi.get("status") == "passed":
            raise state.StateError(f"G8: refusing to launch passed PBI {pbi['id']}")
        pbi["status"] = "building"
        action = _make_launch(run_root, run, "worker", pbi_id=pbi["id"])
    elif s == "checking":
        action = _make_check(run_root, run, "check")
    elif s == "auto_fixing":
        failed = [p for p in run.get("pbis", []) if p.get("status") == "failed"]
        if not failed:
            failed = [p for p in run.get("pbis", []) if p.get("status") == "built"]
        if not failed:
            raise state.StateError("no failed PBI found for auto_fixing")
        target = failed[0]
        if target.get("status") == "passed":
            raise state.StateError(f"G8: refusing to auto-fix passed PBI {target['id']}")
        action = _make_launch(run_root, run, "worker", pbi_id=target["id"])
    elif s == "reviewing":
        action = _make_launch(run_root, run, "reviewer")
    elif s == "triaging":
        action = _make_launch(run_root, run, "planner")
    elif s == "fixing":
        if sub == "check":
            action = _make_check(run_root, run, "fixcheck")
        else:
            action = _make_launch(run_root, run, "worker")
    elif s == "running_e2e":
        action = _make_check(run_root, run, "e2e")
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
