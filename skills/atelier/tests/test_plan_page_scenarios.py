"""The plan approval page shows scenarios, and the mode and kind columns, when a run is driven through the CLI."""

import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from tests.test_acceptance_toggle import ACCEPTANCE, UNIT, plan_text, survey  # noqa: E402
from tests.test_plan_scenarios import scenario_plan  # noqa: E402


class PlanPageScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_on_run_shows_scenarios_mode_and_kind(self) -> None:
        survey(self.root, [UNIT, ACCEPTANCE])
        text = plan_text(self.root, scenario_plan())
        self.assertIn("## シナリオ", text)
        self.assertIn("**R1: 二つの数を足せる（normal）**", text)
        self.assertIn("Scenario Outline: add", text)
        self.assertIn("| ケース | a | b | sum |", text)
        self.assertIn("| R1-1 | 1 | 2 | 3 |", text)
        self.assertIn("**R2: 依存を更新する（light-no-red）**", text)
        self.assertIn("| ケース | テスト名 |", text)
        self.assertIn("| R2-1 | 依存を更新する |", text)
        self.assertIn("| モード | 種類 |", text)
        self.assertRegex(text, r"\| PBI-1 \|.*\| light-no-red \| stub \|")
        self.assertRegex(text, r"\| PBI-2 \|.*\| normal \| build \|")
        self.assertIn("（受け入れ 1、stub 1、build 1、E2E 0）", text)
        self.assertNotIn("???", text)
        self.assertNotIn("\ufffd", text)

    def test_off_run_has_no_scenario_section(self) -> None:
        survey(self.root, [UNIT])
        text = plan_text(self.root)
        self.assertNotIn("## シナリオ", text)
        self.assertIn("| モード | 種類 |", text)
        self.assertIn("| — | build |", text)


if __name__ == "__main__":
    unittest.main()
