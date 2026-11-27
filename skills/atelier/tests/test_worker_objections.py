"""The worker's ticket after the lock, and the objections and stop a worker's result may carry, driven through the CLI.

A build or e2e PBI's ticket lists the locked files, the case ids assigned to it and the unit test files it may write;
a fix PBI's ticket lists the locked files. A run without acceptance-first has none of these. A result's objections
and stop are checked for their shape, kept on the PBI and shown in the report; an objection asks no question, and a
stop holds its PBI and asks the user one question (tests/test_plan_return.py follows it further).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import actions, locks, state  # noqa: E402
from tests.test_acceptance_red import drive, pass_review, saved  # noqa: E402
from tests.test_conductor import VALID_FACTS, run_cli, write_result_for  # noqa: E402
from tests.test_locks import LOCKED, LockTests, lock_plan  # noqa: E402
from tests.test_review_loops import approve, done, drive_to_plan_approval  # noqa: E402
from tests.test_worker_checks import ticket_of, two_pbi_plan  # noqa: E402

UNIT = {"path": "tests/test_app.py", "kind": "unit"}
OBJECTION = {
    "case": "R1-2",
    "what": "the sum should round",
    "why": "the scenario's example looks off by one",
    "state": "green",
    "interpretation": "built as written",
}
STOP = {"exception": 2, "reason": "R1-1 and R1-2 contradict each other"}


def locked_plan() -> dict:
    obj = lock_plan()
    obj["pbis"][2]["targets"] = ["app.py", dict(UNIT)]
    return obj


def message(root: Path, act: dict, payload: dict) -> tuple[int, dict]:
    write_result_for(act, payload)
    return run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])


class LockedTicketTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {locks.HOME_ENV: str(Path(home.name).resolve())})
        env.start()
        self.addCleanup(env.stop)

    def launch(self) -> dict:
        review, _ = LockTests.stage_to_review(self, locked_plan())
        return pass_review(self.root, review)

    def test_a_build_ticket_lists_the_locked_files_its_cases_and_the_unit_tests(self) -> None:
        ticket = ticket_of(self.launch())
        self.assertEqual(LOCKED, ticket["locked_files"])
        self.assertEqual(["R1-1", "R1-2"], ticket["cases"])
        self.assertEqual(["tests/test_app.py"], ticket["unit_tests"])
        self.assertEqual(["app.py", "tests/test_app.py"], ticket["targets"])

    def test_a_fix_pbi_ticket_lists_the_locked_files_only(self) -> None:
        self.launch()
        run = saved(self.root)
        fix = {"id": "CO-1-FIX", "kind": "fix", "targets": ["app.py"]}
        self.assertEqual({"locked_files": LOCKED}, actions.lock_ticket_fields(run, fix))
        self.assertEqual({}, actions.lock_ticket_fields(run, {"id": "PBI-1", "kind": "acceptance"}))
        self.assertEqual({}, actions.lock_ticket_fields(run, {"id": "PBI-2", "kind": "stub"}))
        self.assertEqual({}, actions.lock_ticket_fields({**run, "acceptance_first": False}, fix))

    def test_a_stop_asks_one_question_and_both_notes_reach_the_report(self) -> None:
        launch = self.launch()
        (self.root / "app.py").write_bytes(b"def add(a, b):\n    return a + b\n")
        code, ask = message(self.root, launch, {"status": "ok", "objections": [OBJECTION], "stop": STOP})
        self.assertEqual(0, code, ask)
        run = saved(self.root)
        # PBI-3 is the only PBI left to build, so the plan-return question comes at once.
        self.assertEqual(("asking_plan_return", "ask", "plan_return"), (run["state"], ask["action"], ask["kind"]))
        self.assertEqual(["replan", "continue"], [c["id"] for c in ask["choices"]])
        pbi = state.get_pbi(run, "PBI-3")
        self.assertEqual(("held", state.STOP_HOLD), (pbi["status"], pbi["held_reason"]))
        self.assertEqual([OBJECTION], pbi["objections"])
        self.assertEqual(STOP, pbi["stop"])
        code, nxt = run_cli("answer", "--root", str(self.root), "--choice", "continue")
        self.assertEqual(0, code, nxt)
        self.assertEqual("check", nxt["action"], nxt)
        run = saved(self.root)
        self.assertEqual("built", state.get_pbi(run, "PBI-3")["status"])
        report = actions._write_report(self.root, run).read_text(encoding="utf-8")
        for text in (
            "## 作業者の異議", "PBI-3 R1-2", "the sum should round", "built as written", "## 作業者の stop", "例外 2", STOP["reason"],
            "## 途中の戻り", "PR-1（異議として続ける）",
        ):
            self.assertIn(text, report)


class WorkerResultShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        drive_to_plan_approval(self.root, VALID_FACTS, two_pbi_plan())
        self.act = approve(self.root)

    def test_a_run_without_acceptance_first_keeps_its_ticket(self) -> None:
        ticket = ticket_of(self.act["launches"][0] if self.act["action"] == "launches" else self.act)
        for key in ("locked_files", "cases", "unit_tests"):
            self.assertNotIn(key, ticket)

    def test_wrong_shapes_are_refused_and_the_run_does_not_move(self) -> None:
        bad = [
            {"objections": "x"},
            {"objections": [OBJECTION | {"state": "blue"}]},
            {"objections": [{k: v for k, v in OBJECTION.items() if k != "why"}]},
            {"objections": ["x"]},
            {"stop": "x"},
            {"stop": {"exception": 5, "reason": "r"}},
            {"stop": {"exception": 1, "reason": ""}},
            {"stop": {"exception": True, "reason": "r"}},
        ]
        launch = self.act["launches"][0] if self.act["action"] == "launches" else self.act
        for extra in bad:
            code, out = message(self.root, self.act, {"status": "ok", **extra})
            self.assertNotEqual(0, code, extra)
            self.assertEqual("building", saved(self.root)["state"], extra)
            self.assertNotIn("objections", state.get_pbi(saved(self.root), launch["pbi"]))

    def test_a_good_result_is_kept_on_its_pbi(self) -> None:
        launch = self.act["launches"][0] if self.act["action"] == "launches" else self.act
        code, out = message(self.root, self.act, {"status": "ok", "objections": [OBJECTION]})
        self.assertEqual(0, code, out)
        self.assertEqual([OBJECTION], state.get_pbi(saved(self.root), launch["pbi"])["objections"])
        self.assertEqual([], state.worker_result_errors({"status": "ok", "stop": STOP}))


if __name__ == "__main__":
    unittest.main()
