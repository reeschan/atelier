"""A failed check does not stop the run: it becomes a carry-over that a fix PBI or the fix stage fixes.

The worker runs its checks and fixes them itself; a wave check that still fails puts a fix PBI before
the PBIs that are left, and the last wave's carry-overs go to the fix stage with the review's findings.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

import conductor  # noqa: E402
from atelier import actions, guards, state  # noqa: E402
from tests.test_conductor import reach_building, run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo, reach_building_with, write_agents_and_template  # noqa: E402

_ONE_AT_A_TIME = patch("atelier.template.parallel_limit", return_value=1)


def setUpModule() -> None:
    """These tests follow the carry-over step by step, one launch at a time (test_launch_batches covers batches)."""
    _ONE_AT_A_TIME.start()


def tearDownModule() -> None:
    _ONE_AT_A_TIME.stop()


TWO = [
    {"id": "PBI-1", "title": "Lower the box", "depends_on": [], "targets": ["box.py"], "checks": ["check-1"]},
    {"id": "PBI-2", "title": "Show the box", "depends_on": ["PBI-1"], "targets": ["view.py"], "checks": ["check-2"]},
    {"id": "PBI-3", "title": "Log the box", "depends_on": [], "targets": ["log.py"], "checks": ["check-3"]},
]
FAILED = {"exit_code": 1, "output": "lint: unused export lower", "commands": [
    {"command": "check-1", "exit_code": 1},
    {"command": "python -m unittest", "exit_code": 0},
]}


def done(tmp: Path, act: dict, payload: dict) -> dict:
    write_result_for(act, payload)
    code, nxt = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0, nxt
    return nxt


def answer(tmp: Path, *args: str) -> dict:
    code, nxt = run_cli("answer", "--root", str(tmp), *args)
    assert code == 0, nxt
    return nxt


def pbis_of(tmp: Path) -> dict:
    return {p["id"]: p for p in state.load_state(tmp)["pbis"]}


def fail_first_wave(tmp: Path, plan: list[dict] = TWO) -> dict:
    """Build the first wave (PBI-1, PBI-3) and fail its check on PBI-1."""
    act = reach_building(tmp, pbis=plan)
    while act["action"] in ("launch", "launches"):
        act = done(tmp, act, {"status": "ok"})
    assert act["action"] == "check", act
    return done(tmp, act, {**FAILED, "failed_pbis": ["PBI-1"]})


class FixPbiTests(unittest.TestCase):
    def test_a_failed_check_puts_a_fix_pbi_before_the_pbis_left(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = fail_first_wave(tmp)
            self.assertEqual(("launch", "worker", "CO-1-FIX"), (act["action"], act["role"], act["pbi"]))

            pbis = pbis_of(tmp)
            self.assertEqual("carried", pbis["PBI-1"]["status"])
            self.assertEqual("passed", pbis["PBI-3"]["status"])
            self.assertIn("CO-1-FIX", pbis["PBI-2"]["depends_on"])
            fix = pbis["CO-1-FIX"]
            self.assertEqual(["box.py"], fix["targets"])
            self.assertEqual(["check-1"], fix["checks"])
            self.assertEqual(pbis["PBI-1"]["level"], fix["level"])

            ticket = Path(act["prompt"]).read_text(encoding="utf-8")
            self.assertIn("lint: unused export lower", ticket)
            self.assertIn("Lower the box", ticket)

            carry = state.load_state(tmp)["carry_overs"]
            self.assertEqual([("CO-1", ["PBI-1"], "open", 1)], [(c["id"], c["pbis"], c["status"], c["tries"]) for c in carry])

    def test_the_fix_pbi_passing_passes_the_original_and_lets_dependents_go_on(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = fail_first_wave(tmp)
            act = done(tmp, act, {"status": "ok"})
            self.assertEqual("check", act["action"])
            self.assertIn("check-1", act["commands"])
            act = done(tmp, act, {"exit_code": 0})
            self.assertEqual(("worker", "PBI-2"), (act["role"], act["pbi"]))
            pbis = pbis_of(tmp)
            self.assertEqual(("passed", "passed"), (pbis["PBI-1"]["status"], pbis["CO-1-FIX"]["status"]))
            self.assertEqual("fixed_by_pbi", state.load_state(tmp)["carry_overs"][0]["status"])

    def test_the_second_fix_pbi_goes_one_level_up_and_the_third_failure_is_held_without_asking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = fail_first_wave(tmp)
            level = pbis_of(tmp)["CO-1-FIX"]["level"]
            act = done(tmp, act, {"status": "ok"})
            act = done(tmp, act, {**FAILED, "failed_pbis": ["CO-1-FIX"]})
            self.assertEqual(("worker", "CO-1-FIX"), (act["role"], act["pbi"]))
            self.assertNotEqual(level, pbis_of(tmp)["CO-1-FIX"]["level"])
            self.assertEqual(2, state.load_state(tmp)["carry_overs"][0]["tries"])

            # The third failure asks nothing: the fix PBI, PBI-1 and PBI-2 (which needs it) are held, and the
            # run goes on to the final review since no other PBI is left.
            act = done(tmp, act, {"status": "ok"})
            act = done(tmp, act, {**FAILED, "failed_pbis": ["CO-1-FIX"]})
            self.assertEqual(("launch", "reviewer"), (act["action"], act["role"]))
            pbis = pbis_of(tmp)
            self.assertEqual(
                {"PBI-1": "held", "CO-1-FIX": "held", "PBI-2": "held", "PBI-3": "passed"},
                {pid: p["status"] for pid, p in pbis.items()},
            )
            run = state.load_state(tmp)
            self.assertEqual("unresolved", run["carry_overs"][0]["status"])
            self.assertEqual(["CO-1"], [u["carry_over"] for u in run["unresolved"]])
            self.assertEqual(["check-1"], run["unresolved"][0]["commands"])

    def test_no_auto_fix_launch_after_a_failed_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = fail_first_wave(tmp)
            self.assertNotEqual("auto_fixing", state.load_state(tmp)["state"])
            self.assertNotIn("Mode: auto_fix", Path(act["prompt"]).read_text(encoding="utf-8"))

    def test_a_worker_carry_over_goes_on_to_the_check_and_reaches_the_fix_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_building(tmp, pbis=TWO)
            act = done(tmp, act, {"status": "ok", "carry_over": [{"command": "check-1", "output": "E1", "why": "two tries"}]})
            self.assertEqual(("launch", "PBI-3"), (act["action"], act.get("pbi")))
            act = done(tmp, act, {"status": "ok"})
            self.assertEqual("check", act["action"])
            act = done(tmp, act, {**FAILED, "failed_pbis": ["PBI-1"]})
            self.assertIn("two tries", Path(act["prompt"]).read_text(encoding="utf-8"))


class ReviewFindingTests(unittest.TestCase):
    PLAN = [
        {"id": "PBI-1", "title": "Flaky", "depends_on": [], "targets": ["a.py"], "do_not_touch": ["keep.py"], "checks": ["check-1"]},
        {"id": "PBI-2", "title": "Solid", "depends_on": [], "targets": ["b.py"], "checks": ["check-2"]},
        {"id": "PBI-3", "title": "Next", "depends_on": ["PBI-2"], "targets": ["c.py"], "checks": ["check-3"]},
    ]

    def first_wave_failed_by_commands(self, tmp: Path) -> dict:
        act = reach_building(tmp, pbis=self.PLAN)
        while act["action"] in ("launch", "launches"):
            act = done(tmp, act, {"status": "ok"})
        return done(tmp, act, {"exit_code": 1, "output": "E1", "commands": [
            {"command": "check-1", "exit_code": 1},
            {"command": "check-2", "exit_code": 0},
            {"command": "python -m unittest", "exit_code": 0},
        ]})

    def test_without_failed_pbis_the_failed_commands_decide_which_pbis_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = self.first_wave_failed_by_commands(tmp)
            self.assertEqual("CO-1-FIX", act["pbi"])
            pbis = pbis_of(tmp)
            self.assertEqual(("carried", "passed"), (pbis["PBI-1"]["status"], pbis["PBI-2"]["status"]))

    def test_a_fix_pbi_passes_on_its_own_checks_while_another_pbi_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = self.first_wave_failed_by_commands(tmp)
            act = done(tmp, act, {"status": "ok"})
            self.assertEqual("PBI-3", act["pbi"])
            act = done(tmp, act, {"status": "ok"})
            act = done(tmp, act, {"exit_code": 1, "output": "E3", "commands": [
                {"command": "check-1", "exit_code": 0},
                {"command": "check-3", "exit_code": 1},
            ]})
            self.assertEqual(("launch", "reviewer"), (act["action"], act["role"]))
            run = state.load_state(tmp)
            pbis = {p["id"]: p for p in run["pbis"]}
            self.assertEqual(("passed", "passed", "carried"), (pbis["CO-1-FIX"]["status"], pbis["PBI-1"]["status"], pbis["PBI-3"]["status"]))
            self.assertEqual([("CO-1", "fixed_by_pbi", 1), ("CO-2", "open", 0)], [(c["id"], c["status"], c["tries"]) for c in run["carry_overs"]])

    def test_the_fix_ticket_keeps_do_not_touch_and_says_no_test_edits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = self.first_wave_failed_by_commands(tmp)
            self.assertEqual(["keep.py"], pbis_of(tmp)["CO-1-FIX"]["do_not_touch"])
            ticket = Path(act["prompt"]).read_text(encoding="utf-8")
            self.assertIn('"no_test_edits": true', ticket)
            self.assertIn('"keep.py"', ticket)

    def test_a_fix_pbi_keeps_a_path_one_original_protects_and_another_targets(self) -> None:
        run = {"pbis": [
            {"id": "PBI-A", "targets": ["protected.py", "a.py"], "do_not_touch": [], "level": "L2"},
            {"id": "PBI-B", "targets": ["b.py"], "do_not_touch": ["protected.py"], "level": "L1"},
        ]}
        co = state.record_carry_over(run, run["pbis"], ["check"], "E")
        fix = state.fix_pbi_for(run, co)
        self.assertEqual((["a.py", "b.py"], ["protected.py"]), (fix["targets"], fix["do_not_touch"]))

    def test_check_records_use_the_derived_failed_pbis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.first_wave_failed_by_commands(tmp)
            records = [json.loads(line) for line in (tmp / "docs" / "pbi" / "record.jsonl").read_text(encoding="utf-8").splitlines()]
            checks = {r["pbi"]: r["result"] for r in records if r.get("type") == "check"}
            self.assertEqual(("fail", "pass"), (checks["PBI-1"], checks["PBI-2"]))

    def test_a_worker_carry_over_the_wave_passed_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_building(tmp, pbis=self.PLAN)
            act = done(tmp, act, {"status": "ok", "carry_over": [{"command": "lint a.py", "output": "W1", "why": "two tries"}]})
            while act["action"] in ("launch", "launches"):
                act = done(tmp, act, {"status": "ok"})
            act = done(tmp, act, {"exit_code": 0})
            run = state.load_state(tmp)
            self.assertEqual([], run["carry_overs"])
            self.assertIn("- PBI-1: lint a.py — two tries", actions._unfinished_lines(run))

    def test_replanning_closes_the_open_carry_overs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = self.first_wave_failed_by_commands(tmp)
            ask = done(tmp, act, {"status": "plan_broken", "plan_broken": True, "reason": "needs a new module", "needed_files": ["new.py"]})
            self.assertEqual("plan_broken", ask["kind"])
            act = answer(tmp, "--choice", "replan")
            self.assertEqual("planner", act["role"])
            run = state.load_state(tmp)
            self.assertEqual(["replanned"], [c["status"] for c in run["carry_overs"]])
            self.assertEqual([], state.carry_over_fix_items(run))


class LastWaveTests(unittest.TestCase):
    ONE = [{"id": "PBI-1", "title": "Lower the box", "depends_on": [], "targets": ["box.py"], "checks": ["check-1"]}]

    def reach_fix_approval(self, tmp: Path) -> dict:
        act = reach_building(tmp, pbis=self.ONE)
        act = done(tmp, act, {"status": "ok"})
        act = done(tmp, act, {**FAILED, "failed_pbis": ["PBI-1"]})
        self.assertEqual(("launch", "reviewer"), (act["action"], act["role"]))
        act = done(tmp, act, {"schema": "atelier.findings/1", "findings": []})
        self.assertEqual("check", act["action"])  # E2E runs after the final review
        ask = done(tmp, act, {"exit_code": 0})
        self.assertEqual(("ask", "fix_approval"), (ask["action"], ask["kind"]))
        return ask

    def test_the_last_wave_carry_over_reaches_the_fix_list_without_triage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ask = self.reach_fix_approval(tmp)
            run = state.load_state(tmp)
            self.assertEqual(["CO-1"], [f["id"] for f in run["fix_list"]])
            self.assertIn("CO-1", Path(ask["draft"]).read_text(encoding="utf-8"))

            act = answer(tmp, "--choice", "approve")
            self.assertEqual(("launch", "worker"), (act["action"], act["role"]))
            act = done(tmp, act, {"status": "ok"})
            self.assertEqual("check", act["action"])
            act = done(tmp, act, {"exit_code": 0})
            self.assertEqual("done", act["action"])
            self.assertTrue(act["complete"])
            self.assertEqual("fixed", state.load_state(tmp)["carry_overs"][0]["status"])

    def test_dropping_the_carry_over_reports_unfinished(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.reach_fix_approval(tmp)
            reply = tmp / "reply.json"
            reply.write_text(json.dumps({"choice": "approve", "exclude_ids": ["CO-1"]}), encoding="utf-8")
            act = answer(tmp, "--reply", str(reply))
            self.assertEqual("done", act["action"])
            self.assertFalse(act["complete"])
            self.assertEqual(["PBI-1"], act["unfinished_pbis"])
            self.assertEqual("dropped", state.load_state(tmp)["carry_overs"][0]["status"])
            report = Path(act["report"]).read_text(encoding="utf-8")
            self.assertIn("CO-1", report)
            self.assertIn("外した", report)


class CheckRunsEveryCommandTests(unittest.TestCase):
    def test_a_failed_command_does_not_stop_the_ones_after_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            py = f'"{sys.executable}"'
            act = {"commands": [f"{py} -c \"import sys; sys.exit(3)\"", f"{py} -c \"print('second ran')\""], "result": str(root / "r.json")}
            self.assertEqual(3, conductor._run_check(root, act, None, 60))
            result = json.loads((root / "r.json").read_text(encoding="utf-8"))
            self.assertEqual([3, 0], [c["exit_code"] for c in result["commands"]])
            self.assertIn("second ran", result["output"])

    def test_a_long_later_failure_keeps_the_earlier_failure_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            py = f'"{sys.executable}"'
            act = {"commands": [
                f"{py} -c \"print('FIRST-FAIL'); raise SystemExit(1)\"",
                f"{py} -c \"print('x' * 20000); raise SystemExit(2)\"",
                f"{py} -c \"print('quiet pass')\"",
            ], "result": str(root / "r.json")}
            conductor._run_check(root, act, None, 60)
            output = json.loads((root / "r.json").read_text(encoding="utf-8"))["output"]
            self.assertIn("FIRST-FAIL", output)
            self.assertIn("(ok)", output)
            self.assertLessEqual(len(output), conductor.CHECK_OUTPUT_TAIL + 1000)


def commit(root: Path, *names: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", *names], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "add"], check=True)


class OutsideEditTests(unittest.TestCase):
    def repo(self, td: str) -> Path:
        root = Path(td)
        init_git_repo(root)
        (root / "tests").mkdir()
        (root / "tests" / "test_app.py").write_text("assert 1\n", encoding="utf-8")
        (root / "lib.py").write_text("y = 1\n", encoding="utf-8")
        (root / "keep.py").write_text("k = 1\n", encoding="utf-8")
        commit(root, "tests/test_app.py", "lib.py", "keep.py")
        return root

    def g1(self, violations: list[dict]) -> list[str]:
        return [v["detail"] for v in violations if v["rule"] == "G1"]

    def test_existing_files_outside_the_targets_may_change_and_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["app.py"], outside_edits={"blocked": ["keep.py"]})
            (root / "lib.py").write_text("y = 2\n", encoding="utf-8")
            (root / "tests" / "test_app.py").write_text("assert 2\n", encoding="utf-8")
            self.assertEqual([], self.g1(guards.compare(root, before)))
            self.assertEqual(["lib.py", "tests/test_app.py"], guards.outside_edits(root, before))

    def test_do_not_touch_new_and_deleted_files_still_break_g1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["app.py"], outside_edits={"blocked": ["keep.py"]})
            (root / "keep.py").write_text("k = 2\n", encoding="utf-8")
            (root / "lib.py").unlink()
            (root / "new.py").write_text("n = 1\n", encoding="utf-8")
            details = self.g1(guards.compare(root, before))
            for name in ("keep.py", "lib.py", "new.py"):
                self.assertTrue(any(name in d for d in details), (name, details))

    def test_a_fix_pbi_may_not_change_tests_even_inside_its_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["tests/test_app.py"], outside_edits={"blocked": [], "no_tests": True})
            (root / "tests" / "test_app.py").write_text("assert 2\n", encoding="utf-8")
            (root / "lib.py").write_text("y = 2\n", encoding="utf-8")
            self.assertEqual(["test file changed by a fix PBI: tests/test_app.py"], self.g1(guards.compare(root, before)))

    def test_without_the_allowance_an_outside_edit_breaks_g1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["app.py"])
            (root / "lib.py").write_text("y = 2\n", encoding="utf-8")
            self.assertEqual(["changed outside allowed paths: lib.py"], self.g1(guards.compare(root, before)))

    def test_do_not_touch_holds_inside_a_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["tests/"], outside_edits={"blocked": ["tests/test_app.py"]})
            (root / "tests" / "test_app.py").write_text("assert 2\n", encoding="utf-8")
            self.assertEqual(["do_not_touch changed: tests/test_app.py"], self.g1(guards.compare(root, before)))

    def test_an_approved_edit_keeps_its_content_outside_the_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["app.py"], outside_edits={"blocked": []})
            (root / "lib.py").write_text("y = 2\n", encoding="utf-8")
            approved = [guards.approved_edit(root, "lib.py")]
            (root / "lib.py").write_text("y = 3\n", encoding="utf-8")
            self.assertEqual(["changed outside allowed paths: lib.py"], self.g1(guards.compare(root, before, approved)))

    def test_test_files_by_directory_and_name(self) -> None:
        for name in ("tests/test_app.py", "src/__tests__/view.tsx", "pkg/store_test.go", "src/view/Outline.test.ts",
                     "spec/models/user_spec.rb", "e2e/select.cy.ts", "src/OutlineTest.java", "src/__snapshots__/v.snap",
                     "test_status", "./tests/a.py", "Tests/Foo.cs", "conftest.py", "e2e/login.ts",
                     "spec/spec_helper.rb", "testdata/x.json"):
            with self.subTest(name=name):
                self.assertTrue(guards.is_test_file(name))
        for name in ("app.py", "docs/specs/outline.md", "src/Contest.java", "testing.md", ".tests/a.py", ".test/x.py", ""):
            with self.subTest(name=name):
                self.assertFalse(guards.is_test_file(name))


class WorkerOutsideEditTests(unittest.TestCase):
    def test_a_worker_edit_outside_targets_goes_on_and_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "tests").mkdir()
            (root / "tests" / "test_app.py").write_text("assert x == 1\n", encoding="utf-8")
            commit(root, "tests/test_app.py")
            agents, tmpl = write_agents_and_template(root)
            pbis = [{"id": "PBI-1", "title": "Lower x", "depends_on": [], "level": "L1", "reason": "small", "files": ["app.py"]}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            prompt = Path(act["prompt"]).read_text(encoding="utf-8")
            self.assertIn("outside_edits", prompt)
            self.assertIn("carry_over", prompt)

            (root / "app.py").write_text("x = 0\n", encoding="utf-8")
            (root / "tests" / "test_app.py").write_text("assert x == 0\n", encoding="utf-8")
            act = done(root, act, {"status": "ok", "outside_edits": [{"file": "tests/test_app.py", "why": "x is 0 by the spec now"}]})
            self.assertEqual("check", act["action"], act)

            run = state.load_state(root)
            self.assertEqual([{"by": "PBI-1", "file": "tests/test_app.py", "why": "x is 0 by the spec now"}], run["outside_edits"])
            self.assertIn("- PBI-1: tests/test_app.py — x is 0 by the spec now", actions._unfinished_lines(run))

    def test_a_new_file_outside_targets_still_raises_the_guard_question(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "lib.py").write_text("y = 1\n", encoding="utf-8")
            commit(root, "lib.py")
            agents, tmpl = write_agents_and_template(root)
            pbis = [{"id": "PBI-1", "title": "Lower x", "depends_on": [], "level": "L1", "reason": "small", "files": ["app.py"]}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            (root / "new.py").write_text("n = 1\n", encoding="utf-8")
            (root / "lib.py").write_text("y = 2\n", encoding="utf-8")
            ask = done(root, act, {"status": "ok"})
            self.assertEqual("guard", ask["kind"])
            # The edit outside the targets that G1 let through is still reported, though the launch was refused.
            self.assertEqual([{"by": "PBI-1", "file": "lib.py", "why": ""}], state.load_state(root)["outside_edits"])


if __name__ == "__main__":
    unittest.main()
