"""Regressions for review findings on stage 1: survey placeholders, wave failures, G4 and G1 edges, plan shapes."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

import conductor  # noqa: E402
from atelier import acceptance, actions, guards, pages, plan, record, state, survey, template  # noqa: E402
from tests.test_plan_form_rules import by_id, form_plan  # noqa: E402


def facts_with(*commands: str) -> dict:
    return {"commands": [{"id": f"c{i}", "kind": "acceptance", "command": c} for i, c in enumerate(commands)]}


class SurveyPlaceholderTests(unittest.TestCase):
    def test_a_command_without_both_placeholders_does_not_turn_acceptance_on(self) -> None:
        for command in ("pytest", "pytest {file}", "pytest --junitxml={junit}"):
            with self.subTest(command=command):
                decision = survey.decide_acceptance(facts_with(command), None, True)
                self.assertFalse(decision["enabled"])
                self.assertEqual(survey.ACCEPTANCE_REASONS["no_command"], decision["reason"])

    def test_only_the_commands_with_both_placeholders_are_kept(self) -> None:
        good = "pytest {file} --junitxml={junit}"
        decision = survey.decide_acceptance(facts_with("pytest", good), None, True)
        self.assertEqual({"enabled": True, "reason": None, "commands": [good]}, decision)


class WaveFailedPbisTests(unittest.TestCase):
    def test_case_failures_join_the_failed_pbis_the_result_names(self) -> None:
        run = {
            "state": "checking",
            "pbis": [{"id": "PBI-1", "status": "built"}, {"id": "PBI-2", "status": "built"}],
        }
        judged = {"exit_code": 1, "failed_pbis": ["PBI-1"], "case_failed_pbis": ["PBI-2"], "commands": []}
        with mock.patch.object(conductor.actions, "judge_wave", return_value=judged), \
                mock.patch.object(conductor, "_append_check_records_for_wave"), \
                mock.patch.object(conductor, "_carry_failed_check", return_value="carried_next"), \
                mock.patch.object(conductor.flow, "apply_event"):
            conductor._handle_check_done(Path("."), run, {"exit_code": 1, "failed_pbis": ["PBI-1"]}, {"wave": {}})
        self.assertEqual({"PBI-1": "failed", "PBI-2": "failed"}, {p["id"]: p["status"] for p in run["pbis"]})


class AcceptanceReviewG4Tests(unittest.TestCase):
    def test_the_acceptance_files_the_planner_reads_are_referenced_for_the_scan(self) -> None:
        from tests.test_acceptance_review import FILES, to_review

        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            to_review(root)
            run = state.load_state(root)
            with tempfile.TemporaryDirectory() as stage:
                _, _, referenced = actions._compose_prompt_parts(
                    root, run, actions.active_template(run), "planner", None, root / "result.json", Path(stage)
                )
        resolved = {p.resolve() for p in referenced}
        for name in FILES:
            self.assertIn((root / name).resolve(), resolved)


class FixTicketLockTests(unittest.TestCase):
    def ticket(self, run: dict, root: Path) -> dict:
        captured: dict = {}

        def fake_worker_prompt(_root, *, mode, ticket, result_path, out_path, rule_docs):
            captured.update(ticket)
            Path(out_path).write_text("worker", encoding="utf-8")

        with tempfile.TemporaryDirectory() as stage, \
                mock.patch.object(actions.plan, "worker_prompt", side_effect=fake_worker_prompt):
            tmpl = template.load_template(None, None)
            actions._compose_prompt_parts(root, run, tmpl, "worker", None, root / "r.json", Path(stage), fix_group=0)
        return captured

    def test_fix_tickets_carry_the_locked_files_only_in_a_locked_run(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "goal.md").write_text("goal", encoding="utf-8")
            base = {
                "state": "fixing",
                "goal_file": str(root / "goal.md"),
                "pbis": [],
                "fix_groups": [[{"id": "F1", "file": "app.py"}]],
                "acceptance_first": True,
                "lock": {"files": ["tests/acceptance/test_r1.py"]},
            }
            self.assertEqual(["tests/acceptance/test_r1.py"], self.ticket(copy.deepcopy(base), root)["locked_files"])
            # E2E no longer sends the run back to the fix stage, so a fix stage with no group (the old FIX-E2E ticket) is refused.
            with self.assertRaises(state.StateError):
                self.ticket({**copy.deepcopy(base), "fix_groups": []}, root)
            unlocked = {**copy.deepcopy(base), "lock": None}
            self.assertNotIn("locked_files", self.ticket(unlocked, root))


class RedRecordTests(unittest.TestCase):
    def test_a_refused_event_leaves_no_event_and_no_first_pass_mark(self) -> None:
        events = [
            {"type": "check", "pbi": "PBI-1", "note": "clean"},
            {"type": "check", "pbi": "PBI-2", "note": "AKIA" + "ABCDEFGHIJKLMNOP"},
        ]

        def fake_events(run, verdicts):
            run.setdefault("first_pass_checked_pbis", []).extend(["PBI-1", "PBI-2"])
            return events

        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            run = {"first_pass_checked_pbis": ["PBI-0"]}
            with mock.patch.object(acceptance, "record_events", side_effect=fake_events), \
                    mock.patch.object(record, "append") as append:
                with self.assertRaises(state.StateError):
                    actions.append_red_records(root, run, [])
            append.assert_not_called()
            self.assertEqual(["PBI-0"], run["first_pass_checked_pbis"])


class PlanShapeTests(unittest.TestCase):
    def test_a_scenario_id_that_is_a_list_is_an_error_not_a_crash(self) -> None:
        obj = form_plan()
        obj["scenarios"][0]["id"] = []
        by_id(obj, "PBI-1")["requirements"] = ["R2"]
        errors = plan.validate_plan(obj, acceptance_first=True)
        self.assertTrue(any("id" in e for e in errors), errors)

    def test_requirements_or_interfaces_of_a_wrong_shape_are_errors_not_a_crash(self) -> None:
        for mutate, word in (
            (lambda o: by_id(o, "PBI-1").update(requirements=1), "requirements"),
            (lambda o: o.update(interfaces=5), "interfaces"),
        ):
            with self.subTest(word=word):
                obj = form_plan()
                mutate(obj)
                errors = plan.validate_plan(obj, acceptance_first=True)
                self.assertTrue(any(word in e for e in errors), errors)


class ExamplesTableTests(unittest.TestCase):
    def test_empty_and_null_values_are_shown_and_a_missing_column_is_marked(self) -> None:
        scenario = {
            "id": "R1", "text": "境界", "mode": "normal", "gherkin": "Scenario Outline: x",
            "cases": [
                {"id": "R1-1", "values": {"a": "", "b": None, "c": "OK", "d": 1}},
                {"id": "R1-2", "values": {"a": "x"}},
            ],
        }
        text = "\n".join(pages._scenario_lines([scenario]))
        self.assertIn('| R1-1 | "" | null | OK | 1 |', text)
        self.assertIn("| R1-2 | x | — | — | — |", text)
        self.assertNotIn("未確認", text)


class StopExceptionTests(unittest.TestCase):
    def test_only_integers_one_to_four_are_stop_exceptions(self) -> None:
        def errors(exc) -> list[str]:
            return [e for e in state.worker_result_errors({"stop": {"exception": exc, "reason": "r"}}) if "exception" in e]

        for bad in (1.0, True, 0, 5, "1"):
            with self.subTest(bad=bad):
                self.assertTrue(errors(bad))
        for good in (1, 2, 3, 4):
            with self.subTest(good=good):
                self.assertEqual([], errors(good))


class LockedChangesTests(unittest.TestCase):
    def test_names_go_to_git_as_arguments_in_batches(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            names = ["-dash.txt", "a.txt", "b.txt"]
            for name in names:
                (root / name).write_text(name + "\n", encoding="utf-8")
            hashed = subprocess.run(["git", "-C", str(root), "hash-object", "--", *names], check=True, capture_output=True, text=True)
            blobs = dict(zip(names, hashed.stdout.split()))
            with mock.patch.object(guards, "_BATCH_CHARS", 8):
                self.assertEqual([], guards.locked_changes(root, blobs))
                (root / "b.txt").write_text("changed\n", encoding="utf-8")
                self.assertEqual(["b.txt"], guards.locked_changes(root, blobs))

    def test_batches_keep_order_and_hold_at_least_one_name(self) -> None:
        with mock.patch.object(guards, "_BATCH_CHARS", 5):
            self.assertEqual([["abcdefgh"], ["a", "b"], ["c"]], list(guards._name_batches(["abcdefgh", "a", "b", "c"])))

    def test_a_key_that_changes_from_false_to_0_during_a_launch_is_reported(self) -> None:
        locked = {"keys": {"cfg.json": {"strict": True}}, "keys_now": {"cfg.json": {"strict": {"value": False}}}}
        with mock.patch.object(guards._signatures, "key_values", return_value={"cfg.json": {"strict": {"value": 0}}}):
            self.assertEqual(1, len(guards.locked_key_changes(Path("."), locked)))
        with mock.patch.object(guards._signatures, "key_values", return_value={"cfg.json": {"strict": {"value": False}}}):
            self.assertEqual([], guards.locked_key_changes(Path("."), locked))


if __name__ == "__main__":
    unittest.main()
