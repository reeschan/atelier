"""A test made stale by a PBI's intended behaviour change does not cost a re-plan.

The worker may update an existing test file outside its targets (G1 lets that edit through and the
report lists it), and a broken plan whose needed files are all test files can be mended by adding
them to the PBI's targets instead of re-planning.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import actions, guards, state  # noqa: E402
from tests.test_conductor import run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo, reach_building_with, write_agents_and_template  # noqa: E402
from tests.test_review_loops import approve, done, drive_to_plan_approval, make_valid_plan, run_state  # noqa: E402
from tests.test_conductor import VALID_FACTS  # noqa: E402


class TestFileTests(unittest.TestCase):
    def test_test_files_by_directory_and_name(self) -> None:
        for name in (
            "tests/test_app.py",
            "src/__tests__/view.tsx",
            "pkg/store_test.go",
            "src/view/Outline.test.ts",
            "src/view/outline.spec.tsx",
            "spec/models/user_spec.rb",
            "e2e/cypress/select.cy.ts",
            "src/test/java/OutlineTest.java",
            "src/__snapshots__/view.tsx.snap",
            "app/test/helpers.js",
        ):
            with self.subTest(name=name):
                self.assertTrue(guards.is_test_file(name))
        for name in ("app.py", "src/view/Outline.tsx", "docs/specs/outline.md", "src/Contest.java", "testing.md", ""):
            with self.subTest(name=name):
                self.assertFalse(guards.is_test_file(name))


def commit(root: Path, *names: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", *names], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "add"], check=True)


class CompareTests(unittest.TestCase):
    def repo(self, td: str) -> Path:
        root = Path(td)
        init_git_repo(root)
        (root / "tests").mkdir()
        (root / "tests" / "test_app.py").write_text("assert 1\n", encoding="utf-8")
        (root / "lib.py").write_text("y = 1\n", encoding="utf-8")
        commit(root, "tests/test_app.py", "lib.py")
        return root

    def g1(self, violations: list[dict]) -> list[str]:
        return [v["detail"] for v in violations if v["rule"] == "G1"]

    def test_an_edit_to_an_existing_test_passes_and_is_listed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["app.py"], allow_test_edits=True)
            (root / "tests" / "test_app.py").write_text("assert 2\n", encoding="utf-8")
            self.assertEqual([], self.g1(guards.compare(root, before)))
            self.assertEqual(["tests/test_app.py"], guards.outside_test_edits(root, before))

    def test_new_deleted_and_non_test_files_still_break_g1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["app.py"], allow_test_edits=True)
            (root / "tests" / "test_app.py").unlink()
            (root / "tests" / "test_new.py").write_text("assert 3\n", encoding="utf-8")
            (root / "lib.py").write_text("y = 2\n", encoding="utf-8")
            details = self.g1(guards.compare(root, before))
            for name in ("tests/test_app.py", "tests/test_new.py", "lib.py"):
                self.assertTrue(any(name in d for d in details), (name, details))
            self.assertEqual([], guards.outside_test_edits(root, before))

    def test_without_the_allowance_a_test_edit_breaks_g1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self.repo(td)
            before = guards.snapshot(root, allowed_paths=["app.py"])
            (root / "tests" / "test_app.py").write_text("assert 2\n", encoding="utf-8")
            self.assertEqual(["changed outside allowed paths: tests/test_app.py"], self.g1(guards.compare(root, before)))
            self.assertEqual([], guards.outside_test_edits(root, before))


class WorkerTestEditTests(unittest.TestCase):
    def test_worker_updates_a_stale_test_outside_targets_without_a_guard_question(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "tests").mkdir()
            (root / "tests" / "test_app.py").write_text("assert x == 1\n", encoding="utf-8")
            commit(root, "tests/test_app.py")
            agents, tmpl = write_agents_and_template(root)
            pbis = [{"id": "PBI-1", "title": "Lower x", "depends_on": [], "level": "L1", "reason": "small", "files": ["app.py"]}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            self.assertIn("test_updates", Path(act["prompt"]).read_text(encoding="utf-8"))

            (root / "app.py").write_text("x = 0\n", encoding="utf-8")
            (root / "tests" / "test_app.py").write_text("assert x == 0\n", encoding="utf-8")
            write_result_for(act, {
                "status": "ok",
                "changed_files": ["app.py", "tests/test_app.py"],
                "test_updates": [{"file": "tests/test_app.py", "why": "x is 0 by the spec now"}],
            })
            code, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(0, code, act)
            self.assertEqual("check", act["action"], act)

            run = state.load_state(root)
            self.assertEqual([{"by": "PBI-1", "file": "tests/test_app.py", "why": "x is 0 by the spec now"}], run["test_edits"])
            self.assertIn("- PBI-1: tests/test_app.py — x is 0 by the spec now", actions._unfinished_lines(run))

    def test_a_new_test_file_outside_targets_still_raises_the_guard_question(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            agents, tmpl = write_agents_and_template(root)
            pbis = [{"id": "PBI-1", "title": "Lower x", "depends_on": [], "level": "L1", "reason": "small", "files": ["app.py"]}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            (root / "tests").mkdir()
            (root / "tests" / "test_new.py").write_text("assert True\n", encoding="utf-8")
            write_result_for(act, {"status": "ok"})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(0, code, ask)
            self.assertEqual("guard", ask["kind"])


class AddTestsToTargetsTests(unittest.TestCase):
    def reach_broken(self, tmp: Path, needed: list[str], *, do_not_touch: list[str] | None = None) -> dict:
        plan_obj = make_valid_plan([
            {"id": "PBI-1", "title": "first", "targets": ["a.py"]},
            {"id": "PBI-2", "title": "second", "depends_on": ["PBI-1"], "targets": ["b.py"], "do_not_touch": do_not_touch or []},
        ])
        drive_to_plan_approval(tmp, VALID_FACTS, plan_obj)
        act = approve(tmp)
        code, act = done(tmp, act, {"status": "ok"})
        code, act = done(tmp, act, {"exit_code": 0})
        self.assertEqual("PBI-2", act["pbi"])
        code, ask = done(tmp, act, {
            "status": "plan_broken",
            "plan_broken": True,
            "reason": "the outline test expects the old height",
            "needed_files": needed,
        })
        self.assertEqual((0, "plan_broken"), (code, ask["kind"]), ask)
        return ask

    def test_test_only_break_offers_adding_the_tests_and_redoes_the_same_pbi(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ask = self.reach_broken(tmp, ["src/view/outline.test.ts"])
            self.assertEqual(["add_tests", "replan", "resume", "stop"], [c["id"] for c in ask["choices"]])
            self.assertIn("テストのファイルだけ", Path(ask["question"]).read_text(encoding="utf-8"))

            code, act = run_cli("answer", "--root", str(tmp), "--choice", "add_tests")
            self.assertEqual(0, code, act)
            self.assertEqual(("launch", "worker", "PBI-2"), (act["action"], act["role"], act["pbi"]))
            self.assertIn("src/view/outline.test.ts", Path(act["prompt"]).read_text(encoding="utf-8"))

            run = run_state(tmp)
            self.assertTrue(run["approvals"]["plan"])
            pbis = {p["id"]: p for p in run["pbis"]}
            self.assertEqual(["b.py", "src/view/outline.test.ts"], pbis["PBI-2"]["targets"])
            self.assertEqual("passed", pbis["PBI-1"]["status"])
            self.assertEqual("add_tests", run["approvals"]["revisions"][-1]["action"])
            self.assertNotIn("pending_plan_change", run)

    def test_a_break_that_needs_other_files_does_not_offer_it(self) -> None:
        for needed in (["c.py"], ["tests/test_b.py", "c.py"], ["../tests/test_b.py"]):
            with self.subTest(needed=needed), tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                ask = self.reach_broken(tmp, needed)
                self.assertEqual(["replan", "resume", "stop"], [c["id"] for c in ask["choices"]])
                code, out = run_cli("answer", "--root", str(tmp), "--choice", "add_tests")
                self.assertEqual(2, code, out)

    def test_a_test_file_the_pbi_must_not_touch_is_not_offered(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ask = self.reach_broken(tmp, ["tests/test_b.py"], do_not_touch=["tests/"])
            self.assertNotIn("add_tests", [c["id"] for c in ask["choices"]])


if __name__ == "__main__":
    unittest.main()
