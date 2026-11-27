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
    """Write payload as the result of a launch, or of every launch of a launch batch."""
    for launch in action["launches"] if action.get("action") == "launches" else [action]:
        write_json(launch["result"], payload)


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


VALID_FACTS = {
    "schema": "atelier.facts/1",
    "modules": [{"id": "m1", "text": "ok", "source": "a.py:1"}],
    "layers": [],
    "likely_files": [],
    "tests": [],
    "conventions": [],
    "rule_docs": [],
    "commands": [],
    "review_skills": [],
}

TO_SPEC_BODY = (
    "# Spec\n\n"
    "## Problem Statement\nNeed feature.\n\n"
    "## Solution\nImplement minimal change.\n\n"
    "## User Stories\n1. Works as expected.\n\n"
    "## Implementation Decisions\nStandard library only.\n\n"
    "## Testing Decisions\nUnit tests.\n\n"
    "## Out of Scope\nNone.\n\n"
    "## Further Notes\nNone.\n"
)


def make_valid_plan(pbis: list[dict] | None = None) -> dict:
    raw = pbis or [{"id": "PBI-1", "title": "Single PBI", "depends_on": [], "level": "L1", "reason": "small"}]
    normalized = []
    for item in raw:
        pid = item["id"]
        lvl_reason = item.get("level_reason") or item.get("reason") or "small change"
        normalized.append({
            "id": pid,
            "title": item.get("title", pid),
            "goal": item.get("goal", item.get("title", pid)),
            "level": item.get("level", "L2"),
            "level_reason": lvl_reason,
            "sensitive_candidate": bool(item.get("sensitive_candidate", False)),
            "sensitive_reason": item.get("sensitive_reason", ""),
            "depends_on": list(item.get("depends_on") or []),
            "targets": list(item.get("targets") or item.get("files") or ["app.py"]),
            "do_not_touch": list(item.get("do_not_touch") or []),
            "checks": list(item.get("checks") or ["python run_tests.py"]),
            "facts": list(item.get("facts") or ["m1"]),
            "acceptance": list(item.get("acceptance") or [f"{pid} passes checks"]),
            "evidence": list(item.get("evidence") or ["app.py:1"]),
            "data_needs": list(item.get("data_needs") or []),
        })
    return {
        "schema": "atelier.plan/1",
        "pbis": normalized,
        "checks": ["python -m unittest"],
        "e2e": ["python e2e.py"],
        "final_review": None,
        "open_questions": [],
    }


def make_finding(
    fid: str = "F-1",
    *,
    severity: str = "major",
    confidence: int = 95,
    kind: str = "correctness",
    files: list[str] | None = None,
) -> dict:
    item: dict = {
        "id": fid,
        "file": (files[0] if files else "a.py"),
        "line": 1,
        "severity": severity,
        "confidence": confidence,
        "kind": kind,
        "why": f"why {fid}",
        "fix": f"fix {fid}",
    }
    if files is not None:
        item["files"] = files
    return item


def reach_building(tmp: Path, pbis: list[dict] | None = None) -> dict:
    """Drive straight from start to building with fake results, returning next action."""
    start_run(tmp)
    code, act = run_cli("next", "--root", str(tmp))
    assert code == 0 and act["role"] == "surveyor"
    write_result_for(act, VALID_FACTS)
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "ask" and act["kind"] == "interview_or_skip"

    code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
    assert code == 0 and act["action"] == "launch" and act["role"] == "planner"

    plan_pbis = pbis or [{"id": "PBI-1", "title": "Single PBI", "depends_on": [], "level": "L1", "reason": "small"}]
    write_result_for(act, make_valid_plan(plan_pbis))
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "ask" and act["kind"] == "plan_approval"

    code, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
    assert code == 0 and act["action"] in ("launch", "launches"), act
    assert all(m["role"] == "worker" for m in (act["launches"] if act["action"] == "launches" else [act])), act
    return act


def fail_until_unresolved(tmp: Path, act: dict, failed: list[str] | None = None) -> dict:
    """Fail a wave check, then its fix PBI twice: the carry-over is held as unresolved without a question."""
    extra = {"failed_pbis": failed} if failed else {}
    write_result_for(act, {"exit_code": 1, **extra})
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["pbi"] == "CO-1-FIX", act
    for _ in range(2):
        write_result_for(act, {"status": "ok"})
        code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
        assert code == 0 and act["action"] == "check", act
        write_result_for(act, {"exit_code": 1, "failed_pbis": ["CO-1-FIX"]})
        code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
        assert code == 0, act
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


def reach_e2e_after_review(tmp: Path, findings: list[dict] | None = None) -> dict:
    """Drive to the final review and return its findings: E2E runs next, before the triage."""
    act = reach_reviewing(tmp)
    write_result_for(act, {"schema": "atelier.findings/1", "findings": list(findings or [])})
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["action"] == "check" and act["commands"] == ["python e2e.py"], act
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
            self.assertEqual(act["model"], "haiku")
            self.assertIsNone(act["pbi"])
            self.assertTrue(Path(act["prompt"]).exists())

            write_result_for(act, VALID_FACTS)
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "interview_or_skip")
            self.assertTrue(Path(act["question"]).exists())

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "planner")

            write_result_for(act, make_valid_plan([{"id": "PBI-1", "title": "Build", "depends_on": [], "level": "L1", "reason": "small"}]))
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
            write_result_for(act, {"schema": "atelier.findings/1", "findings": []})
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

            write_result_for(a1, VALID_FACTS)
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
            write_result_for(act, VALID_FACTS)
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "interview_or_skip")

            # asking_interview -> interviewing
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "interview")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "interviewer")

            # interviewing -> interviewing (round with open questions -> ask -> answer)
            tree_path = tmp / "docs" / "pbi" / "interview" / "tree.json"
            write_json(tree_path, {
                "schema": "atelier.tree/1",
                "decided": [],
                "open": [{"id": "Q1", "question": "target scope?", "recommended": "minimal", "depends_on": []}],
            })
            write_result_for(act, {"done": False, "frontier_ids": ["Q1"]})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "ask")
            self.assertEqual(act["kind"], "interview_round")

            code, act = run_cli("answer", "--root", str(tmp), "--text", "1. はい")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "interviewer")

            # interviewing -> awaiting_spec_approval
            spec_path = tmp / "docs" / "pbi" / "interview" / "spec.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(TO_SPEC_BODY, encoding="utf-8")
            write_result_for(act, {"done": True, "spec_path": str(spec_path)})
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
            spec_path.write_text(TO_SPEC_BODY + "\nUpdated.\n", encoding="utf-8")
            write_result_for(act, {"done": True, "spec_path": str(spec_path)})
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
            write_result_for(act, VALID_FACTS)
            run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")

            write_result_for(act, make_valid_plan([{"id": "PBI-1", "title": "v1", "depends_on": []}]))
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "plan_approval")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "revise")
            self.assertEqual(code, 0)
            self.assertEqual(act["role"], "planner")

            write_result_for(act, make_valid_plan([{"id": "PBI-1", "title": "v2", "depends_on": []}]))
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "plan_approval")

    def test_multi_wave_carry_over_holds_the_failing_fix_pbi_as_unresolved(self) -> None:
        # GUARDS.md (G9) points here: the check's exit code alone fails the wave, and the fix PBI failing past its
        # tries is held as unresolved without a question.
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

            # The check fails (exit code 1 fails both PBIs), the fix PBI fails twice -> held as unresolved, no question
            act = fail_until_unresolved(tmp, act)
            self.assertNotEqual(act["action"], "ask")

            # PBI-3 needs both, so it is held too; nothing is left to build -> the final review
            self.assertEqual((act["action"], act["role"]), ("launch", "reviewer"))
            from atelier import state as run_state
            run = run_state.load_state(tmp)
            self.assertEqual(
                {p["id"]: p["status"] for p in run["pbis"]},
                {"PBI-1": "held", "PBI-2": "held", "CO-1-FIX": "held", "PBI-3": "held"},
            )
            self.assertEqual(run["carry_overs"][0]["status"], "unresolved")
            self.assertEqual([u["held_pbis"] for u in run["unresolved"]], [["PBI-1", "PBI-2", "CO-1-FIX", "PBI-3"]])

    def test_an_independent_pbi_shares_the_wave_of_a_fix_pbi(self) -> None:
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

            # The check fails on PBI-1 only -> PBI-2 passes (G8); the fix PBI goes first in the next wave
            write_result_for(act, {"exit_code": 1, "failed_pbis": ["PBI-1"]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["pbi"], "CO-1-FIX")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["pbi"], "PBI-3")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["role"], "reviewer")
            from atelier import state

            statuses = state.pbi_statuses(state.load_state(tmp))
            self.assertEqual({"PBI-1", "PBI-2", "PBI-3", "CO-1-FIX"}, {k for k, v in statuses.items() if v == "passed"})

    def test_review_e2e_failure_triage_fix_and_report(self) -> None:
        from atelier import retro, state

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_e2e_after_review(tmp, [
                make_finding("F-1", severity="major", confidence=95, files=["a.py"]),
                make_finding("F-2", severity="minor", confidence=90, kind="style", files=["a.py"]),
                make_finding("F-3", severity="major", confidence=60, files=["a.py"]),
            ])
            self.assertEqual("running_e2e", state.load_state(tmp)["state"])

            # E2E fails: no fix worker is launched; the failure joins the retrospective list and the triage sorts it
            output = "\n".join(f"line {i}" for i in range(40)) + "\nAssertionError: the page has no total"
            write_result_for(act, {
                "exit_code": 1,
                "commands": [{"command": "python e2e.py", "exit_code": 1}],
                "output": output,
            })
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, act)
            self.assertEqual(("launch", "planner"), (act["action"], act["role"]))
            run = state.load_state(tmp)
            self.assertEqual("triaging", run["state"])
            self.assertEqual({"from": "running_e2e", "event": "e2e_done", "to": "triaging"}, run["history"][-1])
            self.assertNotIn("fixing", [h["to"] for h in run["history"]])
            items = {i["id"]: i for i in retro.load_items(run)}
            self.assertEqual(["F-1", "E2E-1"], list(items))
            failure = items["E2E-1"]
            self.assertEqual("e2e_failure", failure["kind"])
            self.assertEqual("python e2e.py", failure["command"])
            self.assertTrue(failure["output"].endswith("AssertionError: the page has no total"))
            self.assertNotIn("line 0\n", failure["output"])  # only the end of the output
            self.assertIn("python e2e.py", failure["what"])
            self.assertEqual({"review": 1, "e2e_failure": 1}, run["retro"]["kinds"])

            write_result_for(act, {
                "schema": "atelier.triage/1",
                "items": [
                    {"id": "F-1", "decision": "fix", "reason": "fix bug"},
                    {"id": "E2E-1", "decision": "skip", "reason": "the E2E server was down"},
                ],
            })
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual((code, act["action"], act["kind"]), (0, "ask", "fix_approval"))

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
            self.assertEqual((code, act["action"], act["role"]), (0, "launch", "worker"))
            write_result_for(act, {"summary": "fixed F-1", "status": "ok"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual((code, act["action"]), (0, "check"))
            # The final check: the whole check and the plan's E2E, once
            self.assertEqual(["python -m unittest", "python e2e.py"], act["commands"])

            # The final check passes -> the report (the E2E state is not entered again)
            write_result_for(act, {"exit_code": 0, "summary": "final checks passed"})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual((code, act["action"]), (0, "done"))
            run = state.load_state(tmp)
            self.assertIn({"from": "fixing", "event": "fixes_built", "to": "final_checking"}, run["history"])
            self.assertIn({"from": "final_checking", "event": "final_check_done", "to": "reporting"}, run["history"])
            self.assertEqual(1, [h["from"] for h in run["history"]].count("running_e2e"))
            for key in ("e2e_failures", "e2e_limit", "e2e_continuing"):
                self.assertNotIn(key, run["counters"])
            report = Path(act["report"]).read_text(encoding="utf-8")
            self.assertIn("- E2E: 落ちた（1 件を振り返りの一覧に入れた", report)
            self.assertNotIn("E2E 失敗回数", report)

    def test_e2e_passes_then_the_triage(self) -> None:
        from atelier import state

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_e2e_after_review(tmp, [make_finding("F-1", severity="major", confidence=90)])
            write_result_for(act, {"exit_code": 0})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual((code, act["action"], act["role"]), (0, "launch", "planner"))
            run = state.load_state(tmp)
            self.assertEqual(("triaging", "e2e_done"), (run["state"], run["history"][-1]["event"]))
            self.assertEqual({"review": 1}, run["retro"]["kinds"])

    def test_triage_nothing_to_fix_goes_to_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_e2e_after_review(tmp, [make_finding("F-1", severity="major", confidence=90)])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["role"], "planner")

            write_result_for(act, {
                "schema": "atelier.triage/1",
                "items": [{"id": "F-1", "decision": "skip", "reason": "acceptable"}],
            })
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "fix_approval")

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "nothing_to_fix")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "done")
            _, st = run_cli("status", "--root", str(tmp))
            self.assertEqual(st["state"], "done")
            self.assertIn({"from": "awaiting_fix_approval", "event": "nothing_to_fix", "to": "reporting"}, st["history"])

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
            write_result_for(act, VALID_FACTS)
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
            write_result_for(act, {"schema": "atelier.findings/1", "findings": []})
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

        # 3. A failed check goes on as a carry-over; past the fix PBI's tries it is held, never asked
        r4 = state.new_run_state(Path("."), Path("goal.md"), None)
        r4["state"] = "checking"
        r4["approvals"]["spec"] = True
        r4["approvals"]["plan"] = True
        flow.apply_event(r4, "carried_next")
        self.assertEqual(r4["state"], "building")
        r4["state"] = "checking"
        with self.assertRaises(flow.TransitionError):
            flow.apply_event(r4, "check_failed")
        self.assertNotIn("asking_rebuild", flow.STATES)
        self.assertNotIn("auto_fixing", flow.STATES)

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
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r6)

        # 6. No E2E limit: a failed E2E goes on to the triage like a passed one, never to fixing
        r7 = state.new_run_state(Path("."), Path("goal.md"), None)
        r7["state"] = "running_e2e"
        for event in ("e2e_failed", "e2e_passed", "e2e_skipped", "e2e_continue"):
            with self.subTest(event=event), self.assertRaises(flow.TransitionError):
                flow.apply_event(r7, event)
        self.assertEqual("triaging", flow.apply_event(r7, "e2e_done"))
        self.assertEqual(
            [("running_e2e", "e2e_done", flow.AFTER_E2E)],
            [(t.source, t.event, t.target) for t in flow.TRANSITIONS if t.source == "running_e2e"],
        )

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

    def test_no_buildable_pbis_paths_and_final_check_failure(self) -> None:
        # 1. The only PBI fails -> the carry-over goes to the final review and the fix stage
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_building(tmp)
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual((act["action"], act["role"]), ("launch", "reviewer"))

        # 2. Wave 1 passed, Wave 2 failed -> reviewing
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
            code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "reviewer")

        # 3. A failed final check launches no fix worker again: it is left over and the run goes to the report
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_e2e_after_review(tmp, [make_finding("F-1", severity="major", confidence=95)])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            write_result_for(act, {"schema": "atelier.triage/1", "items": [{"id": "F-1", "decision": "fix", "reason": "fix"}]})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            self.assertEqual(act["action"], "check")

            write_result_for(act, {"exit_code": 1})
            code, act = run_cli("done", "--root", str(tmp))
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "done")
            _, st = run_cli("status", "--root", str(tmp))
            self.assertEqual(st["state"], "done")

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

            write_result_for(act, VALID_FACTS)
            run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
            reply = tmp / "reply.json"
            reply.write_text(json.dumps({"choice": "skip"}), encoding="utf-8")
            code, act = run_cli("answer", "--root", str(tmp), "--reply", str(reply))
            self.assertEqual(code, 0)
            self.assertEqual(act["role"], "planner")
