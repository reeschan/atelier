"""The acceptance_first toggle: what the survey leaves in the run, and what the plan page and report show."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import state, template  # noqa: E402
from tests.test_conductor import VALID_FACTS, init_workspace, run_cli, write_json, write_result_for  # noqa: E402
from tests.test_integration import make_plan, make_pbi  # noqa: E402

ACCEPTANCE = {"id": "c1", "text": "run one test file", "source": "Makefile:3", "kind": "acceptance",
              "command": "pytest {file} --junitxml={junit}"}
UNIT = {"id": "c0", "text": "run all", "source": "Makefile:1", "kind": "unit", "command": "pytest"}
REASONS = {"template": "テンプレートで切った", "not_git": "git のリポジトリでない", "no_command": "acceptance のコマンドが無い"}


def git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)


def template_file(root: Path, **parts: bool) -> Path:
    base = json.loads((SKILLS_DIR / "template.default.json").read_text(encoding="utf-8"))
    base["parts"] = dict(parts)
    return write_json(root / "template.json", base)


def survey(root: Path, commands: list[dict], *, git: bool = True, tmpl: Path | None = None) -> dict:
    """Start a run, answer the survey with these commands, and return the saved run."""
    if git:
        git_init(root)
    _, goal, spec = init_workspace(root)
    args = ["start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec)]
    if tmpl is not None:
        args += ["--template", str(tmpl)]
    code, out = run_cli(*args)
    assert code == 0, out
    _, act = run_cli("next", "--root", str(root))
    write_result_for(act, {**VALID_FACTS, "commands": commands})
    code, out = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0, out
    return state.load_state(root)


def acceptance_plan() -> dict:
    """The smallest plan a run with acceptance_first on accepts: one existing interface and one light-no-red scenario."""
    accept = make_pbi("PBI-2", targets=["tests/test_accept.py"])
    accept["kind"] = "acceptance"
    obj = make_plan([make_pbi("PBI-1"), accept])
    obj["interfaces"] = [{"module": "lib", "name": "add"}]
    obj["scenarios"] = [
        {"id": "R1", "text": "依存を更新する", "mode": "light-no-red", "cases": [{"id": "R1-1", "values": {}, "pbi": "PBI-1"}], "spare_count": 0},
    ]
    return obj


def plan_text(root: Path, obj: dict | None = None) -> str:
    _, act = run_cli("answer", "--root", str(root), "--choice", "skip")
    write_result_for(act, obj if obj is not None else make_plan([make_pbi("PBI-1")]))
    code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0 and ask["kind"] == "plan_approval", ask
    return Path(ask["draft"]).read_text(encoding="utf-8")


class AcceptanceToggleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_on_with_command_in_git_repo(self) -> None:
        run = survey(self.root, [UNIT, ACCEPTANCE])
        self.assertIs(run["acceptance_first"], True)
        self.assertIsNone(run["acceptance_reason"])
        self.assertEqual(run["acceptance_commands"], [ACCEPTANCE["command"]])
        text = plan_text(self.root, acceptance_plan())
        self.assertIn("## 部品の入り切り", text)
        self.assertIn("| 受け入れを先に置く (acceptance_first) | 入り | なし |", text)

    def test_off_without_acceptance_command(self) -> None:
        run = survey(self.root, [UNIT])
        self.assertIs(run["acceptance_first"], False)
        self.assertEqual(run["acceptance_reason"], REASONS["no_command"])
        self.assertEqual(run["acceptance_commands"], [])
        self.assertIn("| 切り | " + REASONS["no_command"] + " |", plan_text(self.root))

    def test_off_by_template(self) -> None:
        run = survey(self.root, [ACCEPTANCE], tmpl=template_file(self.root, acceptance_first=False))
        self.assertIs(run["acceptance_first"], False)
        self.assertEqual(run["acceptance_reason"], REASONS["template"])
        self.assertEqual(run["acceptance_commands"], [])
        self.assertIn("| 切り | " + REASONS["template"] + " |", plan_text(self.root))

    def test_template_with_old_parts_shape_stays_on(self) -> None:
        run = survey(self.root, [ACCEPTANCE], tmpl=template_file(self.root, something_else=True))
        self.assertIs(run["acceptance_first"], True)

    def test_off_outside_git_repo(self) -> None:
        run = survey(self.root, [ACCEPTANCE], git=False)
        self.assertIs(run["acceptance_first"], False)
        self.assertEqual(run["acceptance_reason"], REASONS["not_git"])
        self.assertIn("| 切り | " + REASONS["not_git"] + " |", plan_text(self.root))

    def test_report_shows_the_toggle(self) -> None:
        from atelier import actions
        for commands, line in (([ACCEPTANCE], "入り"), ([UNIT], "切り（" + REASONS["no_command"] + "）")):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve()
                run = survey(root, commands)
                report = actions._write_report(root, run).read_text(encoding="utf-8")
                self.assertIn("- 部品「受け入れを先に置く」: " + line, report)

    def test_unknown_command_kind_is_still_refused(self) -> None:
        git_init(self.root)
        _, goal, spec = init_workspace(self.root)
        run_cli("start", "--root", str(self.root), "--goal-file", str(goal), "--spec", str(spec))
        _, act = run_cli("next", "--root", str(self.root))
        write_result_for(act, {**VALID_FACTS, "commands": [{**ACCEPTANCE, "kind": "bogus"}]})
        code, _ = run_cli("done", "--root", str(self.root), "--launch-id", act["launch_id"])
        self.assertEqual(code, 2)

    def test_template_part_default(self) -> None:
        self.assertTrue(template.part_enabled({"parts": {}}, "acceptance_first"))
        self.assertTrue(template.part_enabled({"parts": {"acceptance_first": True}}, "acceptance_first"))
        self.assertFalse(template.part_enabled({"parts": {"acceptance_first": False}}, "acceptance_first"))


if __name__ == "__main__":
    unittest.main()
