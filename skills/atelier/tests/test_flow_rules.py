"""Seam-1 tests for Atelier v8 flow rules, template/runner/survey wiring, budget, sensitive, and plan changes."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from tests.test_conductor import init_workspace, make_valid_plan, run_cli, start_run, write_json, write_result_for


VALID_FACTS: dict = {
    "schema": "atelier.facts/1",
    "modules": [{"id": "m1", "text": "core module", "source": "src/app.py:1"}],
    "layers": [],
    "likely_files": [{"id": "f1", "text": "target file", "source": "src/app.py:10"}],
    "tests": [{"id": "t1", "text": "unit test", "source": "tests/test_app.py:1"}],
    "conventions": [],
    "rule_docs": [],
    "commands": [],
    "review_skills": [],
}


def init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)


def write_agents_and_template(
    root: Path,
    *,
    routes: dict | None = None,
    limits: dict | None = None,
    roles: dict | None = None,
) -> tuple[Path, Path]:
    agents = root / "agents.json"
    write_json(
        agents,
        {
            "runners": {
                "agent": {"enabled": True},
                "codex": {"enabled": True},
                "opencode": {"enabled": True},
                "agy": {"enabled": True},
                "dsh": {"enabled": True},
                "claude": {"enabled": True},
            }
        },
    )
    base = json.loads((SKILLS_DIR / "template.default.json").read_text(encoding="utf-8"))
    if routes is not None:
        base["routes"] = routes
    if limits is not None:
        base["limits"].update(limits)
    if roles is not None:
        base["roles"].update(roles)
    tmpl = root / "template.json"
    write_json(tmpl, base)
    return agents, tmpl


def reach_building_with(
    root: Path,
    pbis: list[dict],
    *,
    agents_path: Path | None = None,
    template_path: Path | None = None,
) -> dict:
    _, goal, spec = init_workspace(root)
    args = ["start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec)]
    if agents_path:
        args += ["--agents", str(agents_path)]
    if template_path:
        args += ["--template", str(template_path)]
    code, out = run_cli(*args)
    assert code == 0, out

    code, act = run_cli("next", "--root", str(root))
    assert code == 0 and act["role"] == "surveyor", act
    write_result_for(act, VALID_FACTS)
    code, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0 and act["kind"] == "interview_or_skip", act

    code, act = run_cli("answer", "--root", str(root), "--choice", "skip")
    assert code == 0 and act["role"] == "planner", act
    write_result_for(act, make_valid_plan(pbis))
    code, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0 and act["kind"] == "plan_approval", act

    code, act = run_cli("answer", "--root", str(root), "--choice", "approve")
    assert code == 0, act
    return act


class TestFlowRulesSeam1(unittest.TestCase):
    def test_six_never_rules_including_per_pbi_two_auto_fixes(self) -> None:
        from atelier import flow, state

        # 1. BuildWithoutPlan
        r1 = state.new_run_state(Path("."), Path("goal.md"), None)
        r1["state"] = "building"
        r1["approvals"]["plan"] = False
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r1)

        # 2. PlanWithoutSpec
        r2 = state.new_run_state(Path("."), Path("goal.md"), None)
        r2["state"] = "planning"
        r2["approvals"]["spec"] = False
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r2)

        # 3. TwoAutoFixes (per PBI!)
        r3 = state.new_run_state(Path("."), Path("goal.md"), None)
        r3["state"] = "auto_fixing"
        r3["approvals"]["spec"] = True
        r3["approvals"]["plan"] = True
        r3["pbis"] = [
            {"id": "PBI-1", "status": "failed", "auto_fix_count": 2},
            {"id": "PBI-2", "status": "pending", "auto_fix_count": 0},
        ]
        r3["auto_fix_pbi"] = "PBI-1"
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r3)

        # Having PBI-1 already auto-fixed once (and passed) does NOT block PBI-2 from auto_fixing
        r3_ok = state.new_run_state(Path("."), Path("goal.md"), None)
        r3_ok["state"] = "auto_fixing"
        r3_ok["approvals"]["spec"] = True
        r3_ok["approvals"]["plan"] = True
        r3_ok["pbis"] = [
            {"id": "PBI-1", "status": "passed", "auto_fix_count": 1},
            {"id": "PBI-2", "status": "failed", "auto_fix_count": 1},
        ]
        r3_ok["auto_fix_pbi"] = "PBI-2"
        flow.check_invariants(r3_ok)

        # 4. SecondReview
        r4 = state.new_run_state(Path("."), Path("goal.md"), None)
        r4["state"] = "reviewing"
        r4["counters"]["review_count"] = 1
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r4)

        # 5. FixWithoutList
        r5 = state.new_run_state(Path("."), Path("goal.md"), None)
        r5["state"] = "fixing"
        r5["approvals"]["fix_list"] = False
        r5["counters"]["e2e_failures"] = 0
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r5)

        # 6. FixPastLimit
        r6 = state.new_run_state(Path("."), Path("goal.md"), None)
        r6["state"] = "fixing"
        r6["counters"]["e2e_failures"] = 3
        r6["counters"]["e2e_limit"] = 2
        r6["counters"]["e2e_continuing"] = False
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r6)

    def test_per_pbi_autofix_prompt_and_same_wave_independent_pbi(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            agents, tmpl = write_agents_and_template(root)
            pbis = [
                {"id": "PBI-1", "title": "First", "depends_on": [], "level": "L1", "reason": "small"},
                {"id": "PBI-2", "title": "Second", "depends_on": [], "level": "L2", "reason": "clear"},
            ]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            self.assertEqual(act["pbi"], "PBI-1")
            self.assertEqual(act["model"], "haiku")

            # Modify tracked file so git diff is non-empty
            (root / "app.py").write_text("x = 2\n", encoding="utf-8")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(act["pbi"], "PBI-2")
            self.assertEqual(act["model"], "sonnet")

            write_result_for(act, {"status": "ok"})
            _, chk = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(chk["action"], "check")

            # Check #1: PBI-1 and PBI-2 both fail; PBI-1 gets auto-fix with ONLY failure output + diff, same model (haiku)
            write_result_for(chk, {"exit_code": 1, "failed_pbis": ["PBI-1", "PBI-2"], "output": "AssertionError: expected 1"})
            code, af1 = run_cli("done", "--root", str(root), "--launch-id", chk["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(af1["action"], "launch")
            self.assertEqual(af1["role"], "worker")
            self.assertEqual(af1["pbi"], "PBI-1")
            self.assertEqual(af1["model"], "haiku")
            self.assertIsNone(af1["effort"])
            af1_prompt = Path(af1["prompt"]).read_text(encoding="utf-8")
            self.assertIn("AssertionError: expected 1", af1_prompt)
            self.assertIn("x = 2", af1_prompt)
            self.assertNotIn("最小の変更を作る", af1_prompt)  # goal not included
            self.assertNotIn("受け入れ条件", af1_prompt)  # spec not included

            # PBI-1 auto-fix returns -> checking -> PBI-1 now passes, PBI-2 still fails for its first auto-fix
            write_result_for(af1, {"status": "ok"})
            _, chk2 = run_cli("done", "--root", str(root), "--launch-id", af1["launch_id"])
            self.assertEqual(chk2["action"], "check")

            # PBI-1 passed, PBI-2 failed for its first time -> PBI-2 MUST get its own auto-fix (sonnet, low)
            write_result_for(chk2, {"exit_code": 1, "failed_pbis": ["PBI-2"], "output": "LintError: unused"})
            code, af2 = run_cli("done", "--root", str(root), "--launch-id", chk2["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(af2["action"], "launch")
            self.assertEqual(af2["pbi"], "PBI-2")
            self.assertEqual(af2["model"], "sonnet")
            self.assertEqual(af2["effort"], "low")

            # PBI-2 fails a second time -> asking_rebuild with 3 choices
            write_result_for(af2, {"status": "ok"})
            _, chk3 = run_cli("done", "--root", str(root), "--launch-id", af2["launch_id"])
            write_result_for(chk3, {"exit_code": 1, "failed_pbis": ["PBI-2"], "output": "still failing"})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", chk3["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(ask["action"], "ask")
            self.assertEqual(ask["kind"], "rebuild")
            self.assertEqual([c["id"] for c in ask["choices"]], ["retry", "retry_up", "hold"])

            # Choose retry_up -> PBI-2 promoted from L2 to L3 (sonnet, high)
            code, rebuild = run_cli("answer", "--root", str(root), "--choice", "retry_up")
            self.assertEqual(code, 0)
            self.assertEqual(rebuild["action"], "launch")
            self.assertEqual(rebuild["pbi"], "PBI-2")
            self.assertEqual(rebuild["model"], "sonnet")
            self.assertEqual(rebuild["effort"], "high")

    def test_template_switch_expired_ask_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, base_tmpl = write_agents_and_template(root, limits={"e2e_rounds": 1})
            alt_tmpl = root / "alt.json"
            alt_data = json.loads(base_tmpl.read_text(encoding="utf-8"))
            alt_data["name"] = "alt"
            alt_data["roles"]["surveyor"] = {"runner": "agent", "model": "sonnet", "effort": "low"}
            alt_data["limits"]["e2e_rounds"] = 3
            write_json(alt_tmpl, alt_data)

            switch_file = root / "switch.json"
            write_json(
                switch_file,
                {
                    "template": str(alt_tmpl),
                    "until": "2020-01-01",
                    "on_expire": "ask",
                    "reason": "expired promo",
                },
            )
            _, goal, spec = init_workspace(root)
            code, out = run_cli(
                "start",
                "--root",
                str(root),
                "--goal-file",
                str(goal),
                "--spec",
                str(spec),
                "--agents",
                str(agents),
                "--template",
                str(base_tmpl),
                "--switch",
                str(switch_file),
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["next"]["action"], "ask")
            self.assertEqual(out["next"]["kind"], "template_switch")

            # Choose drop -> reverts to base template (surveyor=haiku, e2e_limit=1)
            code, act = run_cli("answer", "--root", str(root), "--choice", "drop")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "surveyor")
            self.assertEqual(act["model"], "haiku")

            _, st = run_cli("status", "--root", str(root))
            self.assertEqual(st["counters"]["e2e_limit"], 1)

    def test_template_choose_route_persisted_on_pbi(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, tmpl = write_agents_and_template(root)
            pbis = [{"id": "PBI-1", "title": "Build", "depends_on": [], "level": "L3", "reason": "multi-layer"}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            self.assertEqual(act["model"], "sonnet")
            self.assertEqual(act["effort"], "high")
            _, st = run_cli("status", "--root", str(root))
            pbi = st["pbis"][0]
            self.assertEqual(pbi["route_rule"], "routes.L3")
            self.assertEqual(pbi["route_inputs"], {"level": "L3", "reason": "multi-layer"})
            self.assertEqual(pbi["route_template"], "default")

    def test_external_runner_and_guard_violations_ask_user(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            routes = {
                f"L{i}": [
                    {"runner": "codex", "model": "gpt-test", "effort": "low"},
                    {"runner": "agent", "model": "sonnet", "effort": None},
                ]
                for i in range(1, 6)
            }
            agents, tmpl = write_agents_and_template(root, routes=routes)
            pbis = [{"id": "PBI-1", "title": "Ext", "depends_on": [], "level": "L1", "reason": "small", "files": ["app.py"]}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["runner"], "codex")
            self.assertIsInstance(act.get("command"), list)

            # 1. Fake CLI succeeds and writes result, but modifies a forbidden file -> G1 violation -> asking_guard!
            fake = SKILLS_DIR / "tests" / "fakes" / "cli.py"
            helper = root / "evil.py"
            helper_script = (
                "import json, os, sys; from pathlib import Path; "
                f"Path({str(root / 'forbidden.txt')!r}).write_text('bad'); "
                f"Path({act['result']!r}).write_text(json.dumps({{'status': 'ok'}})); "
                "Path(sys.argv[sys.argv.index('-o')+1]).write_text('done'); "
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':11,'output_tokens':4}}))"
            )
            helper.write_text(helper_script, encoding="utf-8")
            with patch.dict(os.environ, {"ATELIER_CODEX_CMD": json.dumps([sys.executable, str(helper)])}):
                code, ask = run_cli("run", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, ask)
            self.assertEqual(ask["action"], "ask")
            self.assertEqual(ask["kind"], "guard")

            # Clean up forbidden file before user answers next_step to launch agent runner
            (root / "forbidden.txt").unlink()

            # User chooses next_step -> switches to agent runner (NO silent fallback!)
            code, act2 = run_cli("answer", "--root", str(root), "--choice", "next_step")
            self.assertEqual(code, 0, act2)
            self.assertEqual(act2["action"], "launch")
            self.assertEqual(act2["runner"], "agent")
            self.assertEqual(act2["model"], "sonnet")

            write_result_for(act2, {"status": "ok"})
            code, chk = run_cli("done", "--root", str(root), "--launch-id", act2["launch_id"])
            self.assertEqual(code, 0, chk)
            self.assertEqual(chk["action"], "check")

            # Also verify external runner unusable_reason (exit 1) asks user and does not silently fall back
            root2 = root / "repo2"
            root2.mkdir()
            init_git_repo(root2)
            agents2, tmpl2 = write_agents_and_template(root2, routes=routes)
            act_ext = reach_building_with(root2, pbis, agents_path=agents2, template_path=tmpl2)
            fail_cli = root2 / "fail_cli.py"
            fail_cli.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
            with patch.dict(os.environ, {"ATELIER_CODEX_CMD": json.dumps([sys.executable, str(fail_cli)])}):
                code, ask2 = run_cli("run", "--root", str(root2), "--launch-id", act_ext["launch_id"])
            self.assertEqual(code, 0, ask2)
            self.assertEqual(ask2["action"], "ask")
            self.assertEqual(ask2["kind"], "guard")

    def test_agent_launch_snapshot_at_next_compare_at_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            agents, tmpl = write_agents_and_template(root)
            pbis = [{"id": "PBI-1", "title": "A", "depends_on": [], "level": "L1", "reason": "small", "files": ["app.py"]}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            self.assertEqual(act["runner"], "agent")

            # Simulate agent creating package-lock.json (G5 violation) and outside file (G1 violation)
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            write_result_for(act, {"status": "ok"})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(ask["action"], "ask")
            self.assertEqual(ask["kind"], "guard")

    def test_survey_wiring_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, tmpl = write_agents_and_template(root)
            _, goal, spec = init_workspace(root)
            run_cli(
                "start",
                "--root",
                str(root),
                "--goal-file",
                str(goal),
                "--spec",
                str(spec),
                "--agents",
                str(agents),
                "--template",
                str(tmpl),
            )
            code, act = run_cli("next", "--root", str(root))
            self.assertEqual(code, 0)
            self.assertTrue(act["prompt"].endswith("survey.md"))
            self.assertTrue(act["result"].endswith("survey.json"))

            # Invalid fact sheet refused (exit 2) and launch NOT marked complete
            write_result_for(act, {"schema": "wrong"})
            code, err = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            # Retry with valid facts succeeds using the same launch_id
            write_result_for(act, VALID_FACTS)
            code, next_act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(next_act["kind"], "interview_or_skip")

            surveyor_md = (SKILLS_DIR / "prompts" / "surveyor.md").read_text(encoding="utf-8")
            self.assertIn("omm-pr-scan", surveyor_md)
            self.assertIn("--cache", surveyor_md)

    def test_budget_exceeded_before_launch_asks_user(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, tmpl = write_agents_and_template(root, limits={"budget_tokens": 100})
            _, goal, spec = init_workspace(root)
            code, out = run_cli(
                "start",
                "--root",
                str(root),
                "--goal-file",
                str(goal),
                "--spec",
                str(spec),
                "--agents",
                str(agents),
                "--template",
                str(tmpl),
            )
            self.assertEqual(code, 0)
            self.assertEqual(out["next"]["action"], "ask")
            self.assertEqual(out["next"]["kind"], "budget")

            # Resume allows launch to proceed
            code, act = run_cli("answer", "--root", str(root), "--choice", "resume")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "surveyor")

    def test_sensitive_handoff_and_dependent_hold_with_proceed_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, tmpl = write_agents_and_template(root)
            _, goal, spec = init_workspace(root)
            run_cli(
                "start",
                "--root",
                str(root),
                "--goal-file",
                str(goal),
                "--spec",
                str(spec),
                "--agents",
                str(agents),
                "--template",
                str(tmpl),
            )
            _, act = run_cli("next", "--root", str(root))
            write_result_for(act, VALID_FACTS)
            run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--choice", "skip")

            pbis = [
                {"id": "PBI-1", "title": "Independent", "depends_on": [], "level": "L1", "reason": "ok", "files": ["a.py"]},
                {
                    "id": "PBI-2",
                    "title": "Auth Secrets",
                    "goal": "Rotate signing keys",
                    "depends_on": ["PBI-1"],
                    "level": "L2",
                    "reason": "secret",
                    "files": ["secrets.py"],
                    "sensitive_candidate": True,
                    "sensitive_reason": "Handles private keys",
                },
                {"id": "PBI-3", "title": "Blocked Dep", "depends_on": ["PBI-2"], "level": "L1", "reason": "dep", "files": ["b.py"]},
                {"id": "PBI-4", "title": "Proceed Dep", "depends_on": ["PBI-2"], "level": "L1", "reason": "mocked", "files": ["c.py"]},
            ]
            write_result_for(act, make_valid_plan(pbis))
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(act["kind"], "plan_approval")

            reply = root / "plan_reply.json"
            write_json(
                reply,
                {
                    "choice": "approve",
                    "sensitive_pbis": ["PBI-2"],
                    "proceed_pbis": ["PBI-4"],
                },
            )
            code, act = run_cli("answer", "--root", str(root), "--reply", str(reply))
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["pbi"], "PBI-1")

            handoff = root / "docs" / "pbi" / "handoff" / "PBI-2.md"
            self.assertTrue(handoff.is_file())
            handoff_text = handoff.read_text(encoding="utf-8")
            self.assertIn("Rotate signing keys", handoff_text)
            self.assertIn("secrets.py", handoff_text)
            self.assertIn("PBI-1", handoff_text)

            _, st = run_cli("status", "--root", str(root))
            by_id = {p["id"]: p for p in st["pbis"]}
            self.assertEqual(by_id["PBI-2"]["status"], "sensitive")
            self.assertEqual(by_id["PBI-3"]["status"], "held")
            self.assertEqual(by_id["PBI-4"]["status"], "pending")

            # Build PBI-1 -> PBI-4 (since PBI-4 was allowed to proceed without PBI-2) -> never PBI-2 or PBI-3
            write_result_for(act, {"status": "ok"})
            code, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["pbi"], "PBI-4")

    def test_plan_changes_appended_and_staleness_holds_only_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, tmpl = write_agents_and_template(root)
            pbis = [
                {"id": "PBI-1", "title": "Base", "depends_on": [], "level": "L1", "reason": "small"},
                {"id": "PBI-2", "title": "Depends on 1", "depends_on": ["PBI-1"], "level": "L1", "reason": "small"},
                {"id": "PBI-3", "title": "Independent", "depends_on": [], "level": "L1", "reason": "small"},
            ]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            self.assertEqual(act["pbi"], "PBI-1")

            # PBI-1 reports plan_broken with changed_pbis=["PBI-1"]
            write_result_for(act, {"plan_broken": True, "changed_pbis": ["PBI-1"], "reason": "schema changed"})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(ask["kind"], "plan_broken")

            reply = root / "change_reply.json"
            write_json(reply, {"choice": "resume", "changed_pbis": ["PBI-1"], "summary": "Updated PBI-1 interface"})
            code, act2 = run_cli("answer", "--root", str(root), "--reply", str(reply))
            self.assertEqual(code, 0)

            _, st = run_cli("status", "--root", str(root))
            revs = st["approvals"]["revisions"]
            self.assertEqual(len(revs), 2)
            self.assertEqual(revs[0]["revision"], 1)
            self.assertEqual(revs[1]["revision"], 2)
            self.assertEqual(revs[1]["changed_pbis"], ["PBI-1"])

            by_id = {p["id"]: p for p in st["pbis"]}
            self.assertEqual(by_id["PBI-2"]["status"], "held")  # dependent held
            self.assertNotEqual(by_id["PBI-1"]["status"], "held")
            self.assertNotEqual(by_id["PBI-3"]["status"], "held")  # independent not held

    def test_e2e_limit_from_template_and_no_fix_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, tmpl = write_agents_and_template(root, limits={"e2e_rounds": 1})
            pbis = [{"id": "PBI-1", "title": "A", "depends_on": [], "level": "L1", "reason": "small"}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            write_result_for(act, {"status": "ok"})
            _, chk = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(chk, {"exit_code": 0})
            _, rev = run_cli("done", "--root", str(root), "--launch-id", chk["launch_id"])
            write_result_for(rev, {"schema": "atelier.findings/1", "findings": []})
            _, e2e = run_cli("done", "--root", str(root), "--launch-id", rev["launch_id"])
            self.assertEqual(e2e["action"], "check")

            # E2E failure #1 (== limit 1) -> fixing directly without fix_approval
            write_result_for(e2e, {"exit_code": 1})
            code, fix_w = run_cli("done", "--root", str(root), "--launch-id", e2e["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(fix_w["action"], "launch")
            self.assertEqual(fix_w["role"], "worker")
            write_result_for(fix_w, {"status": "ok"})
            _, fix_c = run_cli("done", "--root", str(root), "--launch-id", fix_w["launch_id"])
            write_result_for(fix_c, {"exit_code": 0})
            _, e2e2 = run_cli("done", "--root", str(root), "--launch-id", fix_c["launch_id"])
            self.assertEqual(e2e2["action"], "check")

            # E2E failure #2 (> limit 1) -> asking_e2e_limit
            write_result_for(e2e2, {"exit_code": 1})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", e2e2["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(ask["kind"], "e2e_limit")

            # continue -> 1 round -> failure asks again
            code, fix_w2 = run_cli("answer", "--root", str(root), "--choice", "continue")
            self.assertEqual(code, 0)
            self.assertEqual(fix_w2["action"], "launch")
            write_result_for(fix_w2, {"status": "ok"})
            _, fix_c2 = run_cli("done", "--root", str(root), "--launch-id", fix_w2["launch_id"])
            write_result_for(fix_c2, {"exit_code": 0})
            _, e2e3 = run_cli("done", "--root", str(root), "--launch-id", fix_c2["launch_id"])
            write_result_for(e2e3, {"exit_code": 1})
            code, ask2 = run_cli("done", "--root", str(root), "--launch-id", e2e3["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(ask2["kind"], "e2e_limit")

    def test_template_switch_expired_keep_and_budget_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, base_tmpl = write_agents_and_template(root)
            alt_tmpl = root / "alt.json"
            alt_data = json.loads(base_tmpl.read_text(encoding="utf-8"))
            alt_data["name"] = "alt"
            alt_data["roles"]["surveyor"] = {"runner": "agent", "model": "sonnet", "effort": "low"}
            alt_data["limits"]["budget_usd"] = 0.001
            write_json(alt_tmpl, alt_data)

            switch_file = root / "switch.json"
            write_json(
                switch_file,
                {"template": str(alt_tmpl), "until": "2020-01-01", "on_expire": "ask", "reason": "test"},
            )
            _, goal, spec = init_workspace(root)
            code, out = run_cli(
                "start",
                "--root",
                str(root),
                "--goal-file",
                str(goal),
                "--spec",
                str(spec),
                "--agents",
                str(agents),
                "--template",
                str(base_tmpl),
                "--switch",
                str(switch_file),
            )
            self.assertEqual(code, 0)
            self.assertEqual(out["next"]["kind"], "template_switch")

            # Choose keep -> switches to alt template whose tight budget_usd triggers asking_budget
            code, ask_b = run_cli("answer", "--root", str(root), "--choice", "keep")
            self.assertEqual(code, 0)
            self.assertEqual(ask_b["action"], "ask")
            self.assertEqual(ask_b["kind"], "budget")

            # Stop on budget -> done
            code, done_act = run_cli("answer", "--root", str(root), "--choice", "stop")
            self.assertEqual(code, 0)
            self.assertEqual(done_act["action"], "done")


if __name__ == "__main__":
    unittest.main()
