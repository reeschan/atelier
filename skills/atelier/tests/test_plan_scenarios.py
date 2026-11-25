"""The plan's acceptance-first shape: PBI kind, unit-test marks, interfaces and scenarios, checked through the CLI."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import plan  # noqa: E402
from tests.test_acceptance_toggle import ACCEPTANCE, UNIT, git_init  # noqa: E402
from tests.test_conductor import VALID_FACTS, init_workspace, run_cli, write_result_for  # noqa: E402
from tests.test_integration import make_pbi, make_plan  # noqa: E402


def scenario_plan() -> dict:
    first = make_pbi("PBI-1", targets=["app.py", "tests/test_app.py"])
    first["targets"][1] = {"path": "tests/test_app.py", "kind": "unit"}
    first["kind"] = "stub"
    second = make_pbi("PBI-2", depends_on=["PBI-1"], targets=["b.py"])
    third = make_pbi("PBI-3", targets=["tests/test_accept.py"])
    third["kind"] = "acceptance"
    obj = make_plan([first, second, third])
    obj["interfaces"] = [{"module": "app", "name": "add"}]
    obj["scenarios"] = [
        {
            "id": "R1",
            "text": "二つの数を足せる",
            "mode": "normal",
            "gherkin": "Scenario Outline: add\n  Given <a> and <b>\n  Then <sum>\n  Examples:\n    | a | b | sum |\n    | 1 | 2 | 3 |",
            "cases": [{"id": "R1-1", "values": {"a": 1, "b": 2, "sum": 3}, "pbi": "PBI-2", "needs": ["PBI-1"]}],
            "spare_count": 2,
        },
        {"id": "R2", "text": "依存を更新する", "mode": "light-no-red", "cases": [{"id": "R2-1", "values": {}, "pbi": "PBI-1"}], "spare_count": 0},
    ]
    return obj


def start_to_planner(root: Path, commands: list[dict]) -> dict:
    """Start a run, answer the survey with these commands, and return the planner launch."""
    git_init(root)
    _, goal, spec = init_workspace(root)
    code, out = run_cli("start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec))
    assert code == 0, out
    _, act = run_cli("next", "--root", str(root))
    write_result_for(act, {**VALID_FACTS, "commands": commands})
    code, out = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0, out
    code, act = run_cli("answer", "--root", str(root), "--choice", "skip")
    assert code == 0 and act["role"] == "planner", act
    return act


def submit(root: Path, act: dict, obj: dict) -> tuple[int, dict]:
    write_result_for(act, obj)
    return run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])


def saved_run(root: Path) -> dict:
    return json.loads((root / "docs" / "pbi" / "run.json").read_text(encoding="utf-8"))


class AcceptanceFirstOnTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.act = start_to_planner(self.root, [UNIT, ACCEPTANCE])

    def test_planner_prompt_says_the_toggle_is_on(self) -> None:
        self.assertIn("Acceptance first: on", Path(self.act["prompt"]).read_text(encoding="utf-8"))

    def test_plan_without_scenarios_or_interfaces_is_refused(self) -> None:
        code, out = submit(self.root, self.act, make_plan([make_pbi("PBI-1")]))
        self.assertEqual(2, code)
        self.assertIn("scenarios", out["error"])
        self.assertIn("interfaces", out["error"])

    def test_shape_errors_are_refused(self) -> None:
        cases = {
            "kind": lambda o: o["pbis"][0].update(kind="bogus"),
            "mark": lambda o: o["pbis"][0]["targets"].append({"path": "x.py", "kind": "integration"}),
            "interfaces": lambda o: o["interfaces"].append({"module": "app"}),
            "mode": lambda o: o["scenarios"][0].update(mode="fast"),
            "gherkin": lambda o: o["scenarios"][0].pop("gherkin"),
            "owner": lambda o: o["scenarios"][0]["cases"][0].update(pbi="PBI-9"),
            "dup": lambda o: o["scenarios"][1]["cases"].append({"id": "R1-1", "values": {}, "pbi": "PBI-1"}),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                obj = scenario_plan()
                mutate(obj)
                code, out = submit(self.root, self.act, obj)
                self.assertEqual(2, code, out)
                self.assertTrue(out["error"])

    def test_good_plan_reaches_the_run(self) -> None:
        code, ask = submit(self.root, self.act, scenario_plan())
        self.assertEqual("plan_approval", ask["kind"], ask)
        run = saved_run(self.root)
        pbis = {p["id"]: p for p in run["pbis"]}
        self.assertEqual("stub", pbis["PBI-1"]["kind"])
        self.assertEqual("build", pbis["PBI-2"]["kind"])
        self.assertEqual(["app.py", "tests/test_app.py"], pbis["PBI-1"]["targets"])
        self.assertEqual(["tests/test_app.py"], pbis["PBI-1"]["unit_tests"])
        self.assertEqual([], pbis["PBI-2"]["unit_tests"])
        self.assertEqual(["R1", "R2"], [s["id"] for s in run["scenarios"]])
        self.assertEqual("PBI-2", run["scenarios"][0]["cases"][0]["pbi"])
        self.assertEqual([{"module": "app", "name": "add"}], run["interfaces"])


class AcceptanceFirstOffTests(unittest.TestCase):
    def test_current_plan_passes_and_run_has_no_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            act = start_to_planner(root, [UNIT])
            self.assertIn("Acceptance first: off", Path(act["prompt"]).read_text(encoding="utf-8"))
            code, ask = submit(root, act, make_plan([make_pbi("PBI-1")]))
            self.assertEqual("plan_approval", ask["kind"], ask)
            run = saved_run(root)
            self.assertEqual("build", run["pbis"][0]["kind"])
            self.assertEqual(["app.py"], run["pbis"][0]["targets"])
            self.assertEqual([], run["pbis"][0]["unit_tests"])
            self.assertEqual([], run["scenarios"])
            self.assertEqual([], run["interfaces"])


class PlanFunctionTests(unittest.TestCase):
    def test_validate_plan_toggle(self) -> None:
        bare = make_plan([make_pbi("PBI-1")])
        self.assertEqual([], plan.validate_plan(bare))
        self.assertTrue(plan.validate_plan(bare, acceptance_first=True))
        self.assertEqual([], plan.validate_plan(scenario_plan(), acceptance_first=True))

    def test_frozen_changes_ignores_marked_targets_with_same_content(self) -> None:
        planned = scenario_plan()
        built = {
            "id": "PBI-1", "goal": planned["pbis"][0]["goal"], "checks": planned["pbis"][0]["checks"],
            "kind": "stub", "targets": ["app.py", "tests/test_app.py"], "unit_tests": ["tests/test_app.py"],
        }
        self.assertEqual([], plan.frozen_changes([built], planned, ["PBI-1"]))
        unmarked = copy.deepcopy(planned)
        unmarked["pbis"][0]["targets"][1] = "tests/test_app.py"
        self.assertTrue(plan.frozen_changes([built], unmarked, ["PBI-1"]))


if __name__ == "__main__":
    unittest.main()
