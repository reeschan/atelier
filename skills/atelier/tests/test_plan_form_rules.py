"""The plan's form rules for acceptance-first runs: light conditions, case assignment and where each PBI kind sits.

The rules on e2e PBIs (level L3 or higher, a wave after every build PBI) hold with acceptance_first off as well.
"""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from tests.test_acceptance_toggle import ACCEPTANCE, UNIT  # noqa: E402
from tests.test_integration import make_pbi, make_plan  # noqa: E402
from tests.test_plan_scenarios import saved_run, start_to_planner, submit  # noqa: E402

GHERKIN = "Scenario Outline: add\n  Given <a> and <b>\n  Then <sum>\n  Examples:\n    | a | b | sum |\n    | 1 | 2 | 3 |"


def form_plan() -> dict:
    """A plan that satisfies every form rule: waves are acc+stub, build 3, build 4 (needs 3), e2e."""
    acc = make_pbi("PBI-1", targets=[{"path": "tests/test_app.py", "kind": "unit"}])
    acc["kind"] = "acceptance"
    stub = make_pbi("PBI-2", targets=["app.py"])
    stub["kind"] = "stub"
    core = make_pbi("PBI-3", depends_on=["PBI-1", "PBI-2"], targets=["app.py", "core.py"])
    top = make_pbi("PBI-4", depends_on=["PBI-3"], targets=["b.py"])
    e2e = make_pbi("PBI-5", depends_on=["PBI-4"], level="L3", targets=["e2e/app.spec.js"])
    e2e["kind"] = "e2e"
    obj = make_plan([acc, stub, core, top, e2e])
    obj["interfaces"] = [{"module": "app", "name": "add"}]
    obj["scenarios"] = [
        {
            "id": "R1",
            "text": "二つの数を足せる",
            "mode": "normal",
            "gherkin": GHERKIN,
            "cases": [{"id": "R1-1", "values": {"a": 1, "b": 2, "sum": 3}, "pbi": "PBI-4", "needs": ["PBI-3"]}],
            "spare_count": 1,
        },
        {
            "id": "R2",
            "text": "別の画面の文言を直す",
            "mode": "light",
            "cases": [{"id": "R2-1", "values": {"label": "OK"}, "pbi": "PBI-4", "needs": []}],
            "spare_count": 0,
        },
    ]
    return obj


def by_id(obj: dict, pid: str) -> dict:
    return next(p for p in obj["pbis"] if p["id"] == pid)


class FormRulesCliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.act = start_to_planner(self.root, [UNIT, ACCEPTANCE])

    def refused(self, mutate, *words: str) -> None:
        obj = form_plan()
        mutate(obj)
        code, out = submit(self.root, self.act, obj)
        self.assertEqual(2, code, out)
        for word in words:
            self.assertIn(word, out["error"])

    def test_good_plan_passes(self) -> None:
        code, ask = submit(self.root, self.act, form_plan())
        self.assertEqual("plan_approval", ask["kind"], ask)
        self.assertEqual(5, len(saved_run(self.root)["pbis"]))

    def test_requirement_without_an_acceptance_pbi_is_refused(self) -> None:
        def mutate(obj: dict) -> None:
            by_id(obj, "PBI-1")["requirements"] = ["R1"]
        self.refused(mutate, "R2", "acceptance")

    def test_requirements_named_by_acceptance_pbis_pass(self) -> None:
        obj = form_plan()
        by_id(obj, "PBI-1")["requirements"] = ["R1", "R2"]
        code, ask = submit(self.root, self.act, obj)
        self.assertEqual("plan_approval", ask["kind"], ask)

    def test_light_new_public_interface_is_refused(self) -> None:
        def mutate(obj: dict) -> None:
            obj["scenarios"][1]["cases"][0]["pbi"] = "PBI-3"
            obj["scenarios"][1]["cases"][0]["needs"] = []
        self.refused(mutate, "R2", "公開の口")

    def test_light_no_red_with_cases_over_two_pbis_is_refused(self) -> None:
        def mutate(obj: dict) -> None:
            obj["scenarios"][1]["mode"] = "light-no-red"
            obj["scenarios"][1]["cases"].append({"id": "R2-2", "values": {"label": "NG"}, "pbi": "PBI-3", "needs": []})
            obj["interfaces"] = []
        self.refused(mutate, "R2", "2 つの PBI")

    def test_light_with_four_implementation_files_is_refused(self) -> None:
        def mutate(obj: dict) -> None:
            by_id(obj, "PBI-4")["targets"] = ["b.py", "c.py", "d.py", "e.py", {"path": "tests/helper.py", "kind": "unit"}, "tests/test_b.py"]
        self.refused(mutate, "R2", "4 つ")

    def test_light_with_three_implementation_files_and_tests_passes(self) -> None:
        obj = form_plan()
        by_id(obj, "PBI-4")["targets"] = ["b.py", "c.py", "d.py", {"path": "tests/helper.py", "kind": "unit"}, "tests/test_b.py"]
        code, ask = submit(self.root, self.act, obj)
        self.assertEqual("plan_approval", ask["kind"], ask)

    def test_light_case_value_with_question_mark_is_refused(self) -> None:
        self.refused(lambda o: o["scenarios"][1]["cases"][0]["values"].update(label="?"), "R2", "?")

    def test_normal_mode_is_not_held_to_the_light_conditions(self) -> None:
        obj = form_plan()
        obj["scenarios"][1]["mode"] = "normal"
        obj["scenarios"][1]["gherkin"] = GHERKIN
        obj["scenarios"][1]["cases"][0]["values"]["label"] = "?"
        obj["scenarios"][1]["cases"][0]["pbi"] = "PBI-3"
        code, ask = submit(self.root, self.act, obj)
        self.assertEqual("plan_approval", ask["kind"], ask)

    def test_case_without_a_pbi_or_with_an_unknown_pbi_is_refused(self) -> None:
        self.refused(lambda o: o["scenarios"][0]["cases"][0].pop("pbi"), "R1-1", "pbi")
        self.refused(lambda o: o["scenarios"][0]["cases"][0].update(pbi="PBI-9"), "R1-1", "PBI-9")

    def test_two_latest_pbis_in_one_wave_are_refused(self) -> None:
        def mutate(obj: dict) -> None:
            by_id(obj, "PBI-4")["depends_on"] = ["PBI-1", "PBI-2"]
            obj["scenarios"][0]["cases"][0]["needs"] = ["PBI-3"]
        self.refused(mutate, "R1-1", "PBI-3", "PBI-4")

    def test_case_not_assigned_to_the_latest_pbi_is_refused(self) -> None:
        def mutate(obj: dict) -> None:
            case = obj["scenarios"][0]["cases"][0]
            case["pbi"], case["needs"] = "PBI-3", ["PBI-4"]
        self.refused(mutate, "R1-1", "PBI-4")

    def test_e2e_with_low_level_is_refused(self) -> None:
        for level in ("L1", "L2"):
            with self.subTest(level):
                self.refused(lambda o: by_id(o, "PBI-5").update(level=level), "PBI-5", "L3")

    def test_e2e_not_after_every_build_is_refused(self) -> None:
        self.refused(lambda o: by_id(o, "PBI-5").update(depends_on=["PBI-3"]), "PBI-5", "build")
        self.refused(lambda o: by_id(o, "PBI-5").update(depends_on=["PBI-1"]), "PBI-5", "build")

    def test_acceptance_and_stub_outside_the_first_wave_are_refused(self) -> None:
        self.refused(lambda o: by_id(o, "PBI-1").update(depends_on=["PBI-2"]), "PBI-1", "最初の wave")
        self.refused(lambda o: by_id(o, "PBI-2").update(depends_on=["PBI-1"]), "PBI-2", "最初の wave")

    def test_a_case_with_unhashable_pbi_or_needs_is_an_error_not_a_crash(self) -> None:
        from atelier import plan

        for mutate in (
            lambda c: c.update(pbi=["PBI-1"]),
            lambda c: c.update(needs=[{"x": 1}]),
        ):
            for index in (0, 1):
                obj = copy.deepcopy(form_plan())
                mutate(obj["scenarios"][index]["cases"][0])
                errors = plan.validate_plan(obj, acceptance_first=True)
                self.assertIsInstance(errors, list)
                self.assertTrue(errors)
                self.assertTrue(all(isinstance(e, str) for e in errors))


class FormRulesOffTests(unittest.TestCase):
    def test_rules_are_not_applied_when_the_toggle_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            act = start_to_planner(root, [UNIT])
            # The e2e rules hold with the toggle off too, so the e2e PBI is L3; the stub outside the first wave passes.
            first = make_pbi("PBI-1", level="L3")
            first["kind"] = "e2e"
            second = make_pbi("PBI-2", targets=["b.py"])
            second["kind"] = "stub"
            second["depends_on"] = ["PBI-1"]
            code, ask = submit(root, act, make_plan([first, second]))
            self.assertEqual("plan_approval", ask["kind"], ask)

    def test_validate_plan_off_ignores_a_bad_form(self) -> None:
        from atelier import plan

        obj = copy.deepcopy(form_plan())
        by_id(obj, "PBI-2")["depends_on"] = ["PBI-1"]
        self.assertEqual([], plan.validate_plan(obj))
        self.assertTrue(plan.validate_plan(obj, acceptance_first=True))
        self.assertEqual([], plan.validate_plan(form_plan(), acceptance_first=True))

    def test_validate_plan_refuses_a_bad_e2e_pbi_with_the_toggle_on_or_off(self) -> None:
        from atelier import plan

        for first in (False, True):
            for label, mutate, word in (
                ("L1", lambda o: by_id(o, "PBI-5").update(level="L1"), "L3"),
                ("L2", lambda o: by_id(o, "PBI-5").update(level="L2"), "L3"),
                ("same wave as a build PBI", lambda o: by_id(o, "PBI-5").update(depends_on=["PBI-3"]), "build"),
                ("before a build PBI", lambda o: by_id(o, "PBI-5").update(depends_on=[]), "build"),
            ):
                with self.subTest(acceptance_first=first, case=label):
                    obj = copy.deepcopy(form_plan())
                    mutate(obj)
                    errors = plan.validate_plan(obj, acceptance_first=first)
                    self.assertEqual(1, len(errors), errors)
                    self.assertIn("PBI-5", errors[0])
                    self.assertIn("e2e", errors[0])
                    self.assertIn(word, errors[0])
            with self.subTest(acceptance_first=first, case="good"):
                self.assertEqual([], plan.validate_plan(form_plan(), acceptance_first=first))

    def test_the_planner_is_refused_a_bad_e2e_pbi_with_the_toggle_off(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            act = start_to_planner(root, [UNIT])
            self.assertFalse(saved_run(root)["acceptance_first"])
            build = make_pbi("PBI-1")
            e2e = make_pbi("PBI-2", depends_on=["PBI-1"], level="L2", targets=["e2e/app.spec.js"])
            e2e["kind"] = "e2e"
            code, out = submit(root, act, make_plan([build, e2e]))
            self.assertEqual(2, code, out)
            self.assertIn("PBI-2 は kind が e2e なので、level は L3 以上", out["error"])

            e2e.update(level="L3", depends_on=[])
            code, out = submit(root, act, make_plan([build, e2e]))
            self.assertEqual(2, code, out)
            self.assertIn("PBI-2 は kind が e2e なので、すべての kind が build の PBI より後の wave", out["error"])

            e2e["depends_on"] = ["PBI-1"]
            code, ask = submit(root, act, make_plan([build, e2e]))
            self.assertEqual("plan_approval", ask["kind"], ask)


if __name__ == "__main__":
    unittest.main()
