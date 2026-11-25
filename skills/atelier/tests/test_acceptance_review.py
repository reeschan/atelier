"""The planner's acceptance review, driven through the CLI after the Red check passed.

The planner is launched once when every requirement is Red-checked, and answers ok or fix (with a reason) for each
acceptance test file. A file marked fix has its acceptance PBI built again on the L5 route and the Red check starts
over; a file marked fix in the second review leaves its requirements unresolved, treated like light mode.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import actions, plan, state  # noqa: E402
from tests.test_acceptance_red import (  # noqa: E402
    approve_plan,
    drive,
    finish_stage,
    good,
    pass_review,
    pbis_of,
    red_plan,
    saved,
    write_tests,
)
from tests.test_conductor import run_cli, write_result_for  # noqa: E402
from tests.test_worker_checks import ticket_of  # noqa: E402

SCHEMA = plan.SCHEMA_ACCEPTANCE_REVIEW
FILES = ("acceptance/R1.json", "acceptance/R3.json")


def review_prompt(act: dict) -> str:
    return Path(act["prompt"]).read_text(encoding="utf-8")


def to_review(root: Path) -> dict:
    """Plan R1 and R3 (R2 is light-no-red), write good tests, and drive to the planner's review launch."""
    act = approve_plan(root, red_plan(["R1", "R3"]))
    check = finish_stage(root, act, {"R1": good("R1"), "R3": good("R3"), "R2": {"cases": []}})
    assert check["action"] == "check", check
    return drive(root)


def rebuild_and_recheck(root: Path, rebuild: dict) -> dict:
    """Write the acceptance PBI's tests again and drive its Red check; returns the next action (the review)."""
    write_tests(root, {"R1": good("R1"), "R3": good("R3")})
    write_result_for(rebuild, {"status": "ok", "acceptance_files": {"R1": FILES[0], "R3": FILES[1]}})
    code, check = run_cli("done", "--root", str(root), "--launch-id", rebuild["launch_id"])
    assert (code, check["action"]) == (0, "check"), check
    return drive(root)


class AcceptanceReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_all_ok_launches_the_planner_once_and_goes_on_to_building(self) -> None:
        review = to_review(self.root)
        run = saved(self.root)
        self.assertEqual(("reviewing_acceptance", "launch", "planner"), (run["state"], review["action"], review["role"]))
        prompt = review_prompt(review)
        self.assertIn("Mode: acceptance_review", prompt)
        scenarios = json.loads(next(ln for ln in prompt.splitlines() if ln.startswith("Approved scenarios: "))[len("Approved scenarios: "):])
        self.assertEqual(["R1", "R3", "R2"], [sc["id"] for sc in scenarios])
        self.assertIn("Scenario Outline", scenarios[0]["gherkin"])
        self.assertEqual({"id": "R1-1", "values": {"a": 1, "b": 2, "sum": 3}}, scenarios[0]["cases"][0])
        self.assertIn('Public interfaces: [{"module": "app", "name": "add"}]', prompt)
        files = json.loads(next(ln for ln in prompt.splitlines() if ln.startswith("Acceptance test files: "))[len("Acceptance test files: "):])
        self.assertEqual([{"file": FILES[0], "requirements": ["R1"]}, {"file": FILES[1], "requirements": ["R3"]}], files)
        for text in ("When", "Then", "Given", "## Mode: `acceptance_review`"):
            self.assertIn(text, prompt)
        self.assertNotIn("grill", prompt.lower())

        nxt = pass_review(self.root, review)
        run = saved(self.root)
        self.assertEqual("building", run["state"])
        self.assertEqual(["PBI-3", "PBI-4"], pbis_of(nxt))
        self.assertEqual([], run["unresolved"])
        self.assertEqual({FILES[0]: "ok", FILES[1]: "ok"}, {f: v["status"] for f, v in run["acceptance_review"]["files"].items()})
        entered = [h for h in run["history"] if h["to"] == "reviewing_acceptance"]
        self.assertEqual(1, len(entered))

    def test_a_result_that_misses_a_file_or_has_a_wrong_shape_is_refused(self) -> None:
        review = to_review(self.root)
        ok = {"file": FILES[0], "verdict": "ok"}
        cases = {
            "a file is missing": ({"schema": SCHEMA, "files": [ok]}, FILES[1]),
            "a verdict is not ok or fix": ({"schema": SCHEMA, "files": [ok, {"file": FILES[1], "verdict": "maybe"}]}, "verdict"),
            "fix without a reason": ({"schema": SCHEMA, "files": [ok, {"file": FILES[1], "verdict": "fix", "reason": "  "}]}, "reason"),
            "an unlisted file": ({"schema": SCHEMA, "files": [ok, {"file": FILES[1], "verdict": "ok"}, {"file": "other.json", "verdict": "ok"}]}, "other.json"),
            "wrong schema": ({"schema": "atelier.triage/1", "files": [ok, {"file": FILES[1], "verdict": "ok"}]}, "schema"),
            "files is not a list": ({"schema": SCHEMA, "files": "ok"}, "files"),
        }
        for name, (payload, needle) in cases.items():
            with self.subTest(name):
                write_result_for(review, payload)
                code, out = run_cli("done", "--root", str(self.root), "--launch-id", review["launch_id"])
                self.assertEqual(2, code, out)
                self.assertIn(needle, out["error"])
                self.assertEqual("reviewing_acceptance", saved(self.root)["state"])

    def test_fix_rebuilds_the_acceptance_pbi_on_l5_and_the_red_check_starts_over(self) -> None:
        review = to_review(self.root)
        nxt = pass_review(self.root, review, {FILES[0]: ("fix", "When が app.add を呼んでいない")})
        run = saved(self.root)
        self.assertEqual("building_acceptance", run["state"])
        self.assertEqual(["PBI-1"], pbis_of(nxt))
        pbi = state.get_pbi(run, "PBI-1")
        self.assertEqual(("L5", "routes.L5"), (pbi["level"], pbi["route_rule"]))
        ticket = ticket_of(nxt)
        self.assertEqual("L5", ticket["level"])
        (failure,) = ticket["red_failure"]
        self.assertEqual("R1", failure["requirement"])
        self.assertIn("When が app.add を呼んでいない", failure["reasons"][0])
        # Every requirement of the rebuilt acceptance PBI is checked Red again, and read again.
        self.assertEqual({"R1": "pending", "R3": "pending"}, {r: run["red"][r]["status"] for r in ("R1", "R3")})
        self.assertEqual({}, run["acceptance_review"]["files"])

        second = rebuild_and_recheck(self.root, nxt)
        run = saved(self.root)
        self.assertEqual("reviewing_acceptance", run["state"])
        self.assertEqual({"R1": "passed", "R3": "passed"}, {r: run["red"][r]["status"] for r in ("R1", "R3")})
        prompt = review_prompt(second)
        files = json.loads(next(ln for ln in prompt.splitlines() if ln.startswith("Acceptance test files: "))[len("Acceptance test files: "):])
        self.assertEqual(list(FILES), [item["file"] for item in files])

        after = pass_review(self.root, second)
        run = saved(self.root)
        self.assertEqual("building", run["state"])
        self.assertEqual(["PBI-3", "PBI-4"], pbis_of(after))
        self.assertEqual([], run["unresolved"])
        self.assertEqual(2, run["acceptance_review"]["round"])

    def test_a_second_fix_leaves_the_requirement_unresolved_without_a_lock(self) -> None:
        review = to_review(self.root)
        rebuild = pass_review(self.root, review, {FILES[0]: ("fix", "Then が合計を見ていない")})
        second = rebuild_and_recheck(self.root, rebuild)
        nxt = pass_review(self.root, second, {FILES[0]: ("fix", "まだ Then が合計を見ていない")})
        run = saved(self.root)
        self.assertEqual("building", run["state"])
        self.assertEqual(["PBI-3", "PBI-4"], pbis_of(nxt))
        (item,) = run["unresolved"]
        self.assertEqual(("acceptance_review", "R1", ["PBI-1"]), (item["kind"], item["requirement"], item["pbis"]))
        self.assertIn("まだ Then が合計を見ていない", item["reason"])
        self.assertEqual(["PBI-4"], item["as_light_pbis"])
        self.assertEqual(["R1"], state.get_pbi(run, "PBI-4")["as_light"])
        self.assertTrue(next(sc for sc in run["scenarios"] if sc["id"] == "R1")["no_lock"])
        self.assertNotIn("no_lock", next(sc for sc in run["scenarios"] if sc["id"] == "R3"))
        self.assertEqual(("unresolved", "passed"), (run["red"]["R1"]["status"], run["red"]["R3"]["status"]))
        report = actions._write_report(self.root, run).read_text(encoding="utf-8")
        self.assertIn("## 未解決", report)
        self.assertIn("UR-1（要件 R1、受け入れの PBI PBI-1）: planner の確認で 2 回「直す」と返され", report)


if __name__ == "__main__":
    unittest.main()
