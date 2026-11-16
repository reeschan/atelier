"""Seam-1 tests for the Atelier v8 conductor CLI and state machine."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))


def run_cli(*args: str) -> tuple[int, dict]:
    """Invoke conductor.main in-process and return (exit_code, parsed_json)."""
    import conductor

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = conductor.main(list(args))
    raw = buf.getvalue().strip()
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected 1 JSON line on stdout, got: {raw!r}"
    return code, json.loads(lines[0])


def write_json(path: str | Path, data: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def write_result_for(action: dict, payload: dict) -> None:
    write_json(action["result"], payload)


def init_workspace(tmp: Path, with_spec: bool = True) -> tuple[Path, Path, Path | None]:
    goal = tmp / "goal.md"
    goal.write_text("# 目的\n最小の変更を作る\n", encoding="utf-8")
    spec = None
    if with_spec:
        spec = tmp / "spec.md"
        spec.write_text("# 仕様\n## 目的\nテスト\n## 範囲\nテスト\n## 受け入れ条件\n通ること\n## 範囲外\nなし\n", encoding="utf-8")
    return tmp, goal, spec


def start_run(tmp: Path, with_spec: bool = True, force: bool = False) -> dict:
    root, goal, spec = init_workspace(tmp, with_spec=with_spec)
    args = ["start", "--root", str(root), "--goal-file", str(goal)]
    if spec is not None:
        args += ["--spec", str(spec)]
    if force:
        args.append("--force")
    code, out = run_cli(*args)
    assert code == 0, out
    return out


def reach_building(tmp: Path, pbis: list[dict] | None = None) -> dict:
    """Drive straight from start to building with fake results, returning next action."""
    start_run(tmp)
    code, act = run_cli("next", "--root", str(tmp))
    assert code == 0 and act["role"] == "surveyor"
    write_result_for(act, {"summary": "surveyed", "facts": [{"fact": "ok", "source": "a.py:1"}]})
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "ask" and act["kind"] == "interview_or_skip"

    code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
    assert code == 0 and act["action"] == "launch" and act["role"] == "planner"

    plan_pbis = pbis or [{"id": "PBI-1", "title": "Single PBI", "depends_on": [], "level": "L1", "reason": "small"}]
    write_result_for(act, {"summary": "planned", "pbis": plan_pbis})
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "ask" and act["kind"] == "plan_approval"

    code, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
    assert code == 0 and act["action"] == "launch" and act["role"] == "worker"
    return act


def reach_reviewing(tmp: Path) -> dict:
    """Drive through 1-PBI build and passing check to reviewing."""
    act = reach_building(tmp)
    write_result_for(act, {"summary": "built PBI-1", "status": "ok"})
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "check"

    write_result_for(act, {"exit_code": 0, "summary": "checks passed"})
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "launch" and act["role"] == "reviewer"
    return act


def reach_running_e2e_via_fix(tmp: Path) -> dict:
    """Drive to reviewing with 1 actionable finding -> triage -> approve -> fix -> e2e."""
    act = reach_reviewing(tmp)
    write_result_for(act, {
        "summary": "1 finding",
        "findings": [
            {"id": "F-1", "severity": "major", "confidence": 95, "summary": "bug", "files": ["a.py"]},
            {"id": "F-2", "severity": "minor", "confidence": 90, "summary": "style nit", "kind": "style", "files": ["a.py"]},
            {"id": "F-3", "severity": "major", "confidence": 60, "summary": "uncertain", "files": ["a.py"]},
        ],
    })
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "launch" and act["role"] == "planner"

    write_result_for(act, {
        "summary": "triaged",
        "fix_list": [{"id": "F-1", "summary": "fix bug", "files": ["a.py"]}],
        "deferred": [],
    })
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "ask" and act["kind"] == "fix_approval"

    code, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
    assert code == 0 and act["action"] == "launch" and act["role"] == "worker"

    write_result_for(act, {"summary": "fixed F-1", "status": "ok"})
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "check"

    write_result_for(act, {"exit_code": 0, "summary": "fix checks passed"})
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "check"
    return act


class TestConductorSeam1(unittest.TestCase):
    def test_straight_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            start_out = start_run(tmp)
            self.assertEqual(start_out["state"], "surveying")

            code, act = run_cli("next", "--root", str(tmp))
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "surveyor")
            self.assertEqual(act["runner"], "agent")
            self.assertEqual(act["model"], "opus")
            self.assertIsNone(act["pbi"])
            self.assertTrue(Path(act["prompt"]).exists())

            write_result_for(act, {"summary": "facts ready", "facts": []})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "interview_or_skip")
            self.assertTrue(Path(act["question"]).exists())

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "planner")

            write_result_for(act, {"summary": "1 pbi", "pbis": [{"id": "PBI-1", "title": "Build", "depends_on": []}]})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "plan_approval")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "worker")
            self.assertEqual(act["pbi"], "PBI-1")

            write_result_for(act, {"summary": "built", "status": "ok"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "check")

            write_result_for(act, {"exit_code": 0})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "reviewer")

            # No findings -> directly reaches running_e2e
            write_result_for(act, {"summary": "clean", "findings": []})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "check")

            write_result_for(act, {"exit_code": 0})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "done")
            self.assertTrue(Path(act["report"]).exists())

            code, status = run_cli("status", "--root", str(tmp))
            self.assertEqual(code, 0)
            self.assertEqual(status["state"], "done")

    def test_next_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            start_run(tmp)
            _, a1 = run_cli("next", "--root", str(tmp))
            _, a2 = run_cli("next", "--root", str(tmp))
            self.assertEqual(a1, a2)

            write_result_for(a1, {"summary": "ok"})
            _, q1 = run_cli("done", "--root", str(tmp), "--launch-id", a1["launch_id"])
            _, q2 = run_cli("next", "--root", str(tmp))
            self.assertEqual(q1, q2)

            _, st = run_cli("status", "--root", str(tmp))
            self.assertEqual(st["state"], "asking_interview")
            self.assertEqual(st["pending_action"], q1)

    def test_interview_rounds_and_spec_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            start_run(tmp, with_spec=False)
            _, act = run_cli("next", "--root", str(tmp))
            write_result_for(act, {"summary": "surveyed"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "interview_or_skip")

            # asking_interview -> interviewing
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "interview")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "interviewer")

            # interviewing -> interviewing (round with open questions -> ask -> answer)
            write_result_for(act, {"done": False, "questions": ["Q1: target scope?"]})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "interview_round")

            code, act = run_cli("answer", "--root", str(tmp), "--text", "1. はい")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "interviewer")

            # interviewing -> awaiting_spec_approval
            write_result_for(act, {"done": True, "spec": "# 仕様\n確定"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "spec_approval")

            # awaiting_spec_approval -> interviewing (revise)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "revise")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "interviewer")

            # interviewing -> awaiting_spec_approval -> planning
            write_result_for(act, {"done": True, "spec": "# 仕様\n修正版"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "spec_approval")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "planner")

    def test_plan_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            start_run(tmp)
            _, act = run_cli("next", "--root", str(tmp))
            write_result_for(act, {"summary": "ok"})
            run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")

            write_result_for(act, {"pbis": [{"id": "PBI-1", "title": "v1", "depends_on": []}]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "plan_approval")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "revise")
            self.assertEqual(code, 0)
            self.assertEqual(act["role"], "planner")

            write_result_for(act, {"pbis": [{"id": "PBI-1", "title": "v2", "depends_on": []}]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "plan_approval")

    def test_multi_wave_autofix_and_rebuild_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pbis = [
                {"id": "PBI-1", "title": "W1-A", "depends_on": []},
                {"id": "PBI-2", "title": "W1-B", "depends_on": []},
                {"id": "PBI-3", "title": "W2", "depends_on": ["PBI-1", "PBI-2"]},
            ]
            act = reach_building(tmp, pbis=pbis)
            self.assertEqual(act["pbi"], "PBI-1")

            # PBI-1 built -> stays in building for PBI-2
            write_result_for(act, {"status": "ok"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["pbi"], "PBI-2")

            # PBI-2 built -> checking
            write_result_for(act, {"status": "ok"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "check")

            # Check fail #1 -> auto_fixing
            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "worker")

            # Auto-fix returns -> checking
            write_result_for(act, {"status": "ok"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "check")

            # Check fail #2 -> asking_rebuild (NOT auto_fixing)
            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "rebuild")

            # retry -> building (wave 1)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "retry")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "worker")
            self.assertEqual(act["pbi"], "PBI-1")

            # Build wave 1 again and pass check -> wave 2 (PBI-3)
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["pbi"], "PBI-2")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")

            write_result_for(act, {"exit_code": 0})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["pbi"], "PBI-3")

    def test_rebuild_hold_moves_to_next_wave(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pbis = [
                {"id": "PBI-1", "title": "Flaky", "depends_on": []},
                {"id": "PBI-2", "title": "Solid", "depends_on": []},
                {"id": "PBI-3", "title": "Wave2", "depends_on": ["PBI-2"]},
            ]
            act = reach_building(tmp, pbis=pbis)
            self.assertEqual(act["pbi"], "PBI-1")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["pbi"], "PBI-2")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")

            # Check fails on PBI-1 only -> PBI-2 passes (G8), PBI-1 auto-fixes
            write_result_for(act, {"exit_code": 1, "failed_pbis": ["PBI-1"]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["pbi"], "PBI-1")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")

            # Fails again -> asking_rebuild
            write_result_for(act, {"exit_code": 1, "failed_pbis": ["PBI-1"]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "rebuild")

            # Hold PBI-1 -> moves to Wave 2 (PBI-3), never relaunches PBI-1 or PBI-2
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "hold")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["pbi"], "PBI-3")

    def test_review_triage_fix_and_e2e_limit_continue_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_running_e2e_via_fix(tmp)

            # E2E failure #1 (<= limit 2) -> fixing
            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "worker")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")

            # E2E failure #2 (<= limit 2) -> fixing
            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")

            # E2E failure #3 (> limit 2) -> asking_e2e_limit
            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "e2e_limit")

            # continue -> 1 fix round -> E2E failure #4 -> asks again immediately!
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "continue")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")

            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "e2e_limit")

            # stop -> reporting -> done
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "stop")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "done")
            self.assertTrue(Path(act["report"]).exists())

    def test_triage_nothing_to_fix_goes_to_e2e(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_reviewing(tmp)
            write_result_for(act, {
                "findings": [{"id": "F-1", "severity": "major", "confidence": 90, "summary": "questionable"}]
            })
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["role"], "planner")

            write_result_for(act, {"fix_list": [], "deferred": [{"id": "F-1"}]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "fix_approval")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "nothing_to_fix")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "check")
            _, st = run_cli("status", "--root", str(tmp))
            self.assertEqual(st["state"], "running_e2e")

    def test_cross_cutting_questions_resume_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_building(tmp)
            # Worker reports plan_broken -> asking_plan_broken -> resume back to building
            write_result_for(act, {"plan_broken": True, "reason": "missing dependency"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "plan_broken")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "resume")
            self.assertEqual(code, 0)
            _, st = run_cli("status", "--root", str(tmp))
            self.assertEqual(st["state"], "building")

            # Sensitive detected -> asking_sensitive -> stop -> done
            write_result_for(act, {"sensitive_detected": True, "reason": "auth secret"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "sensitive")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "stop")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "done")

    def test_budget_exceeded_asks_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_building(tmp)
            write_result_for(act, {"budget_exceeded": True, "cost_usd": 10.0})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "budget")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "resume")
            self.assertEqual(code, 0)
            _, st = run_cli("status", "--root", str(tmp))
            self.assertEqual(st["state"], "building")

    def test_g6_answer_only_via_answer_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            start_run(tmp)
            _, act = run_cli("next", "--root", str(tmp))

            # Role result containing "answer" must be refused
            write_result_for(act, {"summary": "done", "answer": "skip"})
            code, err = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            # answer refused when not in an ask state
            code, err = run_cli("answer", "--root", str(tmp), "--choice", "skip")
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            # Valid result advances, and answer writes the answer file
            write_result_for(act, {"summary": "ok"})
            run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            code, _ = run_cli("answer", "--root", str(tmp), "--choice", "skip")
            self.assertEqual(code, 0)
            answers = list((tmp / "docs" / "pbi" / "answers").glob("*"))
            self.assertEqual(len(answers), 1)

    def test_g7_start_refuses_unfinished_run_unless_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            start_run(tmp)

            root, goal, spec = init_workspace(tmp)
            code, err = run_cli("start", "--root", str(root), "--goal-file", str(goal))
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            code, out = run_cli("start", "--root", str(root), "--goal-file", str(goal), "--force")
            self.assertEqual(code, 0)
            self.assertEqual(out["state"], "surveying")

    def test_g7_start_allows_after_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_reviewing(tmp)
            write_result_for(act, {"findings": []})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "done")

            root, goal, _ = init_workspace(tmp)
            code, out = run_cli("start", "--root", str(root), "--goal-file", str(goal))
            self.assertEqual(code, 0)
            self.assertEqual(out["state"], "surveying")

    def test_g8_passed_pbi_never_relaunched_or_double_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pbis = [
                {"id": "PBI-1", "title": "Wave1", "depends_on": []},
                {"id": "PBI-2", "title": "Wave2", "depends_on": ["PBI-1"]},
            ]
            act1 = reach_building(tmp, pbis=pbis)
            launch1 = act1["launch_id"]
            write_result_for(act1, {"status": "ok"})
            _, chk = run_cli("done", "--root", str(tmp), "--launch-id", launch1)
            write_result_for(chk, {"exit_code": 0})
            _, act2 = run_cli("done", "--root", str(tmp), "--launch-id", chk["launch_id"])
            self.assertEqual(act2["pbi"], "PBI-2")

            # Re-submitting launch1 must be refused (G8)
            code, err = run_cli("done", "--root", str(tmp), "--launch-id", launch1)
            self.assertEqual(code, 2)
            self.assertIn("error", err)

    def test_never_rules_hold_at_seam_and_in_flow(self) -> None:
        from atelier import flow, state

        # 1. BuildWithoutPlan
        r = state.new_run_state(Path("."), Path("goal.md"), None)
        r["state"] = "awaiting_plan_approval"
        with self.assertRaises(flow.TransitionError):
            flow.apply_event(r, "wave_built")
        # Direct attempt to enter building with plan=False must fail invariant
        r2_bad = state.new_run_state(Path("."), Path("goal.md"), None)
        r2_bad["state"] = "building"
        r2_bad["approvals"]["plan"] = False
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r2_bad)

        # 2. PlanWithoutSpec
        r3 = state.new_run_state(Path("."), Path("goal.md"), None)
        r3["state"] = "planning"
        r3["approvals"]["spec"] = False
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r3)

        # 3. TwoAutoFixes
        r4 = state.new_run_state(Path("."), Path("goal.md"), None)
        r4["state"] = "checking"
        r4["approvals"]["spec"] = True
        r4["approvals"]["plan"] = True
        r4["counters"]["auto_fix_count"] = 1
        flow.apply_event(r4, "check_failed")
        self.assertEqual(r4["state"], "asking_rebuild")
        r4_bad = dict(r4, state="auto_fixing")
        r4_bad["counters"] = dict(r4["counters"], auto_fix_count=2)
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r4_bad)

        # 4. SecondReview
        r5 = state.new_run_state(Path("."), Path("goal.md"), None)
        r5["state"] = "checking"
        r5["approvals"]["spec"] = True
        r5["approvals"]["plan"] = True
        r5["counters"]["review_count"] = 1
        with self.assertRaises(flow.TransitionError):
            flow.apply_event(r5, "all_pbis_passed")

        # 5. FixWithoutList
        r6 = state.new_run_state(Path("."), Path("goal.md"), None)
        r6["state"] = "fixing"
        r6["approvals"]["fix_list"] = False
        r6["counters"]["e2e_failures"] = 0
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r6)

        # 6. FixPastLimit
        r7 = state.new_run_state(Path("."), Path("goal.md"), None)
        r7["state"] = "fixing"
        r7["counters"]["e2e_failures"] = 3
        r7["counters"]["e2e_limit"] = 2
        r7["counters"]["e2e_continuing"] = False
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r7)

    def test_cli_subprocess_utf8_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            root, goal, _ = init_workspace(tmp)
            res = subprocess.run(
                [sys.executable, str(SKILLS_DIR / "conductor.py"), "start", "--root", str(root), "--goal-file", str(goal)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            out = json.loads(res.stdout.strip())
            self.assertEqual(out["state"], "surveying")

            # Refused command returns exit 2 and JSON error
            res2 = subprocess.run(
                [sys.executable, str(SKILLS_DIR / "conductor.py"), "start", "--root", str(root), "--goal-file", str(goal)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(res2.returncode, 2)
            err = json.loads(res2.stdout.strip())
            self.assertIn("error", err)

    def test_no_buildable_pbis_paths_and_fix_check_retry(self) -> None:
        # 1. All PBIs held (0 passed) -> reporting -> done
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_building(tmp)
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 1})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 1})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "rebuild")
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "hold")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "done")

        # 2. Wave 1 passed, Wave 2 held -> reviewing
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pbis = [
                {"id": "PBI-1", "title": "W1", "depends_on": []},
                {"id": "PBI-2", "title": "W2", "depends_on": ["PBI-1"]},
            ]
            act = reach_building(tmp, pbis=pbis)
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["pbi"], "PBI-2")

            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 1})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 1})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "hold")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "reviewer")

        # 3. Fix check failure in fixing re-launches worker, then passes to running_e2e
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_reviewing(tmp)
            write_result_for(act, {"findings": [{"id": "F-1", "severity": "major", "confidence": 95}]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"fix_list": [{"id": "F-1"}]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")

            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp))
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "worker")

            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            code, act = run_cli("done", "--root", str(tmp))
            self.assertEqual(code, 0)
            _, st = run_cli("status", "--root", str(tmp))
            self.assertEqual(st["state"], "running_e2e")

    def test_reply_file_and_error_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            code, err = run_cli("status", "--root", str(tmp))
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            start_run(tmp)
            _, act = run_cli("next", "--root", str(tmp))
            # Wrong launch_id refused
            code, err = run_cli("done", "--root", str(tmp), "--launch-id", "wrong-id")
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            write_result_for(act, {"summary": "ok"})
            run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            reply = tmp / "reply.json"
            reply.write_text(json.dumps({"choice": "skip"}), encoding="utf-8")
            code, act = run_cli("answer", "--root", str(tmp), "--reply", str(reply))
            self.assertEqual(code, 0)
            self.assertEqual(act["role"], "planner")
