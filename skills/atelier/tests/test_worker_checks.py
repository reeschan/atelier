"""A worker runs only the checks of its own work; the run runs the repository-wide checks once.

A PBI's checks are the tests related to it. The plan's checks (the whole test suite, the type check and
the like) reach the worker only as `repository_checks`, which it does not run: the run runs them once
after each wave and once after the fix stage.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from atelier import actions, plan
from tests.test_conductor import VALID_FACTS, make_finding, make_valid_plan
from tests.test_review_loops import approve, done, drive_to_plan_approval

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
WHOLE = ["npm test", "npm run type-check"]


def ticket_of(launch: dict) -> dict:
    """The ticket JSON written into a worker's prompt."""
    body = Path(launch["prompt"]).read_text(encoding="utf-8")
    start = body.index("## Ticket\n```json\n") + len("## Ticket\n```json\n")
    return json.loads(body[start:body.index("\n```", start)])


def tickets_of(act: dict) -> dict[str, dict]:
    launches = act["launches"] if act["action"] == "launches" else [act]
    return {ticket_of(launch)["id"]: ticket_of(launch) for launch in launches}


def two_pbi_plan(*, second_depends: bool = False) -> dict:
    plan_obj = make_valid_plan([
        {"id": "PBI-1", "title": "a", "checks": ["node --test test/a.test.js", "npm test"], "targets": ["a.py"]},
        {
            "id": "PBI-2",
            "title": "b",
            "checks": ["node --test test/b.test.js"],
            "targets": ["b.py"],
            "depends_on": ["PBI-1"] if second_depends else [],
        },
    ])
    plan_obj["checks"] = list(WHOLE)
    plan_obj["e2e"] = ["npm run e2e"]
    return plan_obj


class WorkerTicketTests(unittest.TestCase):
    def test_workers_get_their_related_tests_and_the_run_checks_the_whole_once(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            drive_to_plan_approval(tmp, VALID_FACTS, two_pbi_plan())
            act = approve(tmp)

            tickets = tickets_of(act)
            self.assertEqual(["node --test test/a.test.js"], tickets["PBI-1"]["checks"])
            self.assertEqual(["node --test test/b.test.js"], tickets["PBI-2"]["checks"])
            self.assertEqual(WHOLE, tickets["PBI-1"]["repository_checks"])

            code, act = done(tmp, act, {"status": "ok"})
            self.assertEqual("check", act["action"], act)
            self.assertEqual(
                ["node --test test/a.test.js", "npm test", "node --test test/b.test.js", "npm run type-check"],
                act["commands"],
            )
            code, act = done(tmp, act, {"exit_code": 0})
            code, act = done(tmp, act, {"schema": "atelier.findings/1", "findings": [make_finding("F-1", files=["b.py"])]})
            # E2E runs once, after the final review; its failure goes to the triage, not to a fix worker
            self.assertEqual(("check", ["npm run e2e"]), (act["action"], act["commands"]))
            code, act = done(tmp, act, {"exit_code": 1, "output": "e2e: spec/box.spec.ts failed"})
            self.assertEqual(("launch", "planner"), (act["action"], act["role"]), act)
            code, act = done(tmp, act, {"schema": "atelier.triage/1", "items": [
                {"id": "F-1", "decision": "fix", "reason": "bug"},
                {"id": "E2E-1", "decision": "skip", "reason": "the box spec is flaky"},
            ]})
            act = approve(tmp)

            fix = ticket_of(act)
            self.assertEqual("FIX-1", fix["id"])
            self.assertEqual(["node --test test/b.test.js"], fix["checks"])
            # The run checks the fix stage once, in the final check: the whole check, then E2E.
            self.assertEqual(["node --test test/b.test.js", *WHOLE], fix["repository_checks"])

            code, act = done(tmp, act, {"status": "ok"})
            self.assertEqual(
                ("check", ["node --test test/b.test.js", *WHOLE, "npm run e2e"]), (act["action"], act["commands"])
            )
            code, act = done(tmp, act, {"exit_code": 0})
            self.assertEqual("done", act["action"], act)

    def test_a_fix_pbi_for_a_failed_whole_check_reproduces_it_instead_of_rerunning_it(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            drive_to_plan_approval(tmp, VALID_FACTS, two_pbi_plan(second_depends=True))
            act = approve(tmp)
            code, act = done(tmp, act, {"status": "ok"})
            self.assertEqual("check", act["action"], act)
            code, act = done(tmp, act, {"exit_code": 1, "output": "FAIL test/a.test.js", "commands": [
                {"command": "node --test test/a.test.js", "exit_code": 0},
                {"command": "npm test", "exit_code": 1},
                {"command": "npm run type-check", "exit_code": 0},
            ]})
            self.assertEqual(("launch", "CO-1-FIX"), (act["action"], act["pbi"]), act)

            ticket = ticket_of(act)
            self.assertEqual([], ticket["checks"])
            self.assertEqual(["npm test"], ticket["carry_over"]["failed_commands"])
            self.assertIn("test/a.test.js", ticket["carry_over"]["output"])
            self.assertEqual(WHOLE, ticket["repository_checks"])


class PlanChecksTests(unittest.TestCase):
    def test_a_pbi_without_related_tests_needs_the_plan_checks(self) -> None:
        plan_obj = make_valid_plan()
        plan_obj["pbis"][0]["checks"] = []
        plan_obj["checks"] = list(WHOLE)
        self.assertEqual([], plan.validate_plan(plan_obj))
        plan_obj["checks"] = []
        self.assertTrue(plan.validate_plan(plan_obj))

    def test_worker_checks_leave_out_the_plan_checks(self) -> None:
        run = {"plan_checks": list(WHOLE)}
        self.assertEqual(["node --test a"], actions.worker_checks(run, ["npm test", "node --test a"]))
        self.assertEqual(["npm test"], actions.worker_checks({}, ["npm test"]))

    def test_related_commands_are_the_checks_of_the_pbis_that_own_the_files(self) -> None:
        pbis = [{"checks": ["u a"], "targets": ["a.py"]}, {"checks": ["u b", "u a"], "targets": ["src/b.py"]}]
        self.assertEqual(["u b", "u a"], plan.related_commands(pbis, ["./src/b.py"]))
        self.assertEqual([], plan.related_commands(pbis, ["spec.md"]))


class PromptTests(unittest.TestCase):
    def test_the_worker_runs_only_its_own_checks(self) -> None:
        worker = (PROMPTS / "worker.md").read_text(encoding="utf-8")
        self.assertIn("`repository_checks`", worker)
        self.assertIn("Do not run them", worker)
        self.assertIn("files you changed only", worker)
        self.assertIn("narrowest command", worker)
        self.assertIn("fix at most twice", worker)

    def test_the_planner_keeps_whole_repository_checks_out_of_a_pbi(self) -> None:
        planner = (PROMPTS / "planner.md").read_text(encoding="utf-8")
        self.assertIn("only the tests related to that PBI", planner)
        self.assertIn("Never put a repository-wide command in a PBI's `checks`", planner)
        self.assertIn("runs once after every wave and once after the fix stage", planner)

    def test_the_surveyor_says_how_to_run_one_test_file(self) -> None:
        self.assertIn("one test file", (PROMPTS / "surveyor.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
