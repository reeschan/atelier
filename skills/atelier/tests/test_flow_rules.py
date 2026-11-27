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
    def test_never_rules(self) -> None:
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

        # 3. No auto-fix: a failed check is carried over, so there is no state that relaunches a worker on it
        self.assertNotIn("auto_fixing", flow.STATES)

        # 4. SecondReview
        r4 = state.new_run_state(Path("."), Path("goal.md"), None)
        r4["state"] = "reviewing"
        r4["counters"]["review_count"] = 1
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(r4)

        # 5. FixWithoutList: only the approved fix list enters fixing (a failed E2E is not a reason)
        r5 = state.new_run_state(Path("."), Path("goal.md"), None)
        r5["state"] = "fixing"
        r5["approvals"]["fix_list"] = False
        with self.assertRaisesRegex(flow.InvariantError, "never FixWithoutList: entered fixing without an approved fix list$"):
            flow.check_invariants(r5)
        r5["approvals"]["fix_list"] = True
        flow.check_invariants(r5)

        # 6. No E2E limit: no question about it, no counters for it, no FixPastLimit, no way from E2E to fixing
        self.assertNotIn("asking_e2e_limit", flow.STATES)
        self.assertNotIn("asking_e2e_limit", flow.QUESTION_STATES)
        r6 = state.new_run_state(Path("."), Path("goal.md"), None)
        for key in ("e2e_failures", "e2e_limit", "e2e_continuing"):
            self.assertNotIn(key, r6["counters"])
        self.assertNotIn("e2e_rounds", r6["limits"])
        import inspect
        self.assertNotIn("FixPastLimit", inspect.getsource(flow.check_invariants))
        self.assertNotIn("e2e", inspect.getsource(flow.check_invariants))
        self.assertEqual([], [t for t in flow.TRANSITIONS if t.source == "running_e2e" and t.target == "fixing"])

        # 7. ReturnWithoutAsking: building and checking go back to planning only through the user's answer
        def back_to_planning(src: str) -> dict:
            run = state.new_run_state(Path("."), Path("goal.md"), None)
            run["state"] = "planning"
            run["approvals"]["spec"] = True
            run["history"].append({"from": src, "event": "replan", "to": "planning"})
            return run

        for src in ("building", "checking"):
            with self.subTest(source=src), self.assertRaisesRegex(flow.InvariantError, "never ReturnWithoutAsking"):
                flow.check_invariants(back_to_planning(src))
        for src in ("asking_plan_return", "asking_plan_broken", "awaiting_plan_approval", "asking_budget"):
            with self.subTest(source=src):
                flow.check_invariants(back_to_planning(src))
        self.assertEqual(
            {("asking_plan_return", "plan_return_replan"), ("asking_plan_broken", "replan"), ("awaiting_plan_approval", "revise_plan")},
            {(t.source, t.event) for t in flow.TRANSITIONS if t.target == "planning" and t.source not in ("asking_interview", "awaiting_spec_approval")},
        )
        for src in ("building", "checking"):
            r7 = state.new_run_state(Path("."), Path("goal.md"), None)
            r7.update(state=src)
            r7["approvals"].update(spec=True, plan=True)
            for event in ("replan", "plan_return_replan", "revise_plan"):
                with self.subTest(source=src, event=event), self.assertRaises(flow.TransitionError):
                    flow.apply_event(r7, event)

    def test_fix_pbi_prompt_level_and_route(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            agents, tmpl = write_agents_and_template(root)
            pbis = [
                {"id": "PBI-1", "title": "First", "depends_on": [], "level": "L1", "reason": "small"},
                {"id": "PBI-2", "title": "Second", "depends_on": [], "level": "L2", "reason": "clear"},
                {"id": "PBI-3", "title": "Third", "depends_on": ["PBI-1", "PBI-2"], "level": "L1", "reason": "small"},
            ]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            self.assertEqual(act["pbi"], "PBI-1")
            self.assertEqual(act["model"], "haiku")

            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(act["pbi"], "PBI-2")
            self.assertEqual(act["model"], "sonnet")

            write_result_for(act, {"status": "ok"})
            _, chk = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(chk["action"], "check")

            # Check #1: PBI-1 and PBI-2 both fail -> one fix PBI at the higher level (L2: sonnet, low)
            write_result_for(chk, {"exit_code": 1, "failed_pbis": ["PBI-1", "PBI-2"], "output": "AssertionError: expected 1"})
            code, fix1 = run_cli("done", "--root", str(root), "--launch-id", chk["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual((fix1["action"], fix1["role"], fix1["pbi"]), ("launch", "worker", "CO-1-FIX"))
            self.assertEqual((fix1["model"], fix1["effort"]), ("sonnet", "low"))
            prompt = Path(fix1["prompt"]).read_text(encoding="utf-8")
            self.assertIn("AssertionError: expected 1", prompt)
            self.assertIn('"title": "First"', prompt)
            self.assertIn('"title": "Second"', prompt)

            # The fix PBI fails -> once more one level up (L3: sonnet, high)
            write_result_for(fix1, {"status": "ok"})
            _, chk2 = run_cli("done", "--root", str(root), "--launch-id", fix1["launch_id"])
            write_result_for(chk2, {"exit_code": 1, "failed_pbis": ["CO-1-FIX"], "output": "LintError: unused"})
            code, fix2 = run_cli("done", "--root", str(root), "--launch-id", chk2["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual((fix2["pbi"], fix2["model"], fix2["effort"]), ("CO-1-FIX", "sonnet", "high"))
            self.assertIn("LintError: unused", Path(fix2["prompt"]).read_text(encoding="utf-8"))

            # It fails past its tries -> held as unresolved without a question; PBI-3 waits on it, so it is held
            # too, and with nothing left to build the final review follows
            write_result_for(fix2, {"status": "ok"})
            _, chk3 = run_cli("done", "--root", str(root), "--launch-id", fix2["launch_id"])
            write_result_for(chk3, {"exit_code": 1, "failed_pbis": ["CO-1-FIX"], "output": "still failing"})
            code, nxt = run_cli("done", "--root", str(root), "--launch-id", chk3["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual((nxt["action"], nxt["role"]), ("launch", "reviewer"))
            from atelier import state
            run = state.load_state(root)
            self.assertEqual(
                {p["id"]: (p["status"], p.get("held_reason")) for p in run["pbis"]},
                {pid: ("held", state.UNRESOLVED_HOLD) for pid in ("PBI-1", "PBI-2", "CO-1-FIX", "PBI-3")},
            )
            self.assertEqual("L3", state.get_pbi(run, "CO-1-FIX")["level"])
            self.assertIn("still failing", run["unresolved"][0]["output"])

    def test_template_switch_expired_ask_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, base_tmpl = write_agents_and_template(root, limits={"budget_tokens": 1000000})
            alt_tmpl = root / "alt.json"
            alt_data = json.loads(base_tmpl.read_text(encoding="utf-8"))
            alt_data["name"] = "alt"
            alt_data["roles"]["surveyor"] = {"runner": "agent", "model": "sonnet", "effort": "low"}
            alt_data["limits"]["budget_tokens"] = 3000000
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

            # Choose drop -> reverts to base template (surveyor=haiku, budget_tokens=1000000)
            code, act = run_cli("answer", "--root", str(root), "--choice", "drop")
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["role"], "surveyor")
            self.assertEqual(act["model"], "haiku")

            _, st = run_cli("status", "--root", str(root))
            self.assertEqual(st["limits"]["budget_tokens"], 1000000)

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
            # PBI-4 was allowed to proceed without PBI-2, and its files do not overlap PBI-1's: one launch batch.
            self.assertEqual(act["action"], "launches")
            self.assertEqual([m["pbi"] for m in act["launches"]], ["PBI-1", "PBI-4"])

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
            self.assertEqual(by_id["PBI-4"]["status"], "building")

            # Build PBI-1 and PBI-4 -> the wave check, never PBI-2 or PBI-3
            write_result_for(act, {"status": "ok"})
            code, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0)
            self.assertEqual(act["action"], "check")

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

    def test_e2e_rounds_in_the_template_is_read_but_a_failed_e2e_goes_to_the_triage(self) -> None:
        from atelier import retro, state

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # An older template with the E2E limit is still read; the limit changes nothing.
            agents, tmpl = write_agents_and_template(root, limits={"e2e_rounds": 1})
            # PBI-1 runs the E2E command, so its failure has PBI-1's files to fix.
            pbis = [{
                "id": "PBI-1", "title": "A", "depends_on": [], "level": "L1", "reason": "small",
                "checks": ["python run_tests.py", "python e2e.py"],
            }]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            write_result_for(act, {"status": "ok"})
            _, chk = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(chk, {"exit_code": 0})
            _, rev = run_cli("done", "--root", str(root), "--launch-id", chk["launch_id"])
            write_result_for(rev, {"schema": "atelier.findings/1", "findings": []})
            _, e2e = run_cli("done", "--root", str(root), "--launch-id", rev["launch_id"])
            self.assertEqual(e2e["action"], "check")

            # E2E fails: the triage sorts its failure; no fix worker, no limit question
            write_result_for(e2e, {"exit_code": 1, "output": "E2E: checkout page timed out"})
            code, tri = run_cli("done", "--root", str(root), "--launch-id", e2e["launch_id"])
            self.assertEqual(code, 0, tri)
            self.assertEqual(("launch", "planner"), (tri["action"], tri["role"]))
            run = state.load_state(root)
            self.assertEqual(1, run["limits"]["e2e_rounds"])
            self.assertNotIn("e2e_limit", run["counters"])
            [failure] = retro.load_items(run)
            self.assertEqual(("E2E-1", "e2e_failure"), (failure["id"], failure["kind"]))
            # The check did not say which command failed: the item names the plan's E2E commands.
            self.assertEqual("python e2e.py", failure["command"])
            self.assertEqual("E2E: checkout page timed out", failure["output"])
            self.assertEqual("E2E: checkout page timed out", failure["why"])

            write_result_for(tri, {"schema": "atelier.triage/1", "items": [{"id": "E2E-1", "decision": "fix", "reason": "a real timeout"}]})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", tri["launch_id"])
            self.assertEqual((code, ask["kind"]), (0, "fix_approval"))
            code, fix_w = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual((code, fix_w["role"]), (0, "worker"))
            ticket = Path(fix_w["prompt"]).read_text(encoding="utf-8")
            self.assertIn('"id": "FIX-1"', ticket)
            self.assertNotIn("FIX-E2E", ticket)
            write_result_for(fix_w, {"status": "ok"})
            _, fix_c = run_cli("done", "--root", str(root), "--launch-id", fix_w["launch_id"])
            self.assertEqual(fix_c["action"], "check")
            write_result_for(fix_c, {"exit_code": 0})
            code, done = run_cli("done", "--root", str(root), "--launch-id", fix_c["launch_id"])
            self.assertEqual((code, done["action"]), (0, "done"))

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
