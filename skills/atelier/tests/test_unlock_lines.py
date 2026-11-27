"""An unlock's check against the lines of the file's other locked cases, and against the order of the testcases.

The fix worker of an unlock can change the whole of the opened acceptance test file. The check runs the file at the
lock commit, where the implementation is not, so a change to another locked case's line that keeps the case green
now cannot be seen from how it ends. The lines that name such a case must therefore stay as they were at the lock
commit. The Seam 1 tests drive the CLI on a temporary git repository with the fake runner of test_unlock, whose
locked file holds one case per line here. A testcase that ran twice ends as its worst run, and an error is worse than
an assertion, whichever comes first in the JUnit XML. Every test points ATELIER_LOCKS_HOME at a temporary folder.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import acceptance  # noqa: E402
from tests import test_acceptance_red  # noqa: E402
from tests.test_acceptance_red import drive, saved  # noqa: E402
from tests.test_unlock import R1_FILE, UnlockTestBase, r1  # noqa: E402


def by_line(spec: dict) -> str:
    """An acceptance test file of the fake runner with one case on each line."""
    return '{"cases": [\n' + ",\n".join(json.dumps(case) for case in spec["cases"]) + "\n]}\n"


def write_by_line(root: Path, specs: dict[str, dict]) -> None:
    """The fake acceptance worker, writing each requirement's file with one case on each line."""
    for rid, spec in specs.items():
        path = root / "acceptance" / f"{rid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(by_line(spec).encode("utf-8"))


class UnlockOtherCaseLinesTests(UnlockTestBase):
    def to_fix_stage_by_line(self) -> dict:
        """The fix stage of a scenario unlock of R1-2, with R1's tests locked one case on each line."""
        with patch.object(test_acceptance_red, "write_tests", write_by_line):
            launch = self.to_fix_stage("scenario")
        self.assertIn('{"name": "test_R1[R1-1]", "expect": 3},\n', (self.root / R1_FILE).read_text(encoding="utf-8"))
        return launch

    def test_a_changed_line_of_another_case_that_stays_green_fails_the_check(self) -> None:
        launch = self.to_fix_stage_by_line()
        # The worker fixes R1-2 and also writes other inputs into R1-1's line; R1-1 still passes now.
        changed = {"cases": [{"name": "test_R1[R1-1]", "a": 0, "b": 3, "expect": 3}, {"name": "test_R1[R1-2]", "expect": 5}]}
        self.write(R1_FILE, by_line(changed))
        check = self.done(launch, {"status": "ok"})
        self.assertEqual(("check", "unlock"), (check["action"], check["stage"]))
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        left = self.assert_not_locked_again(saved(self.root), check)
        self.assertIn("R1-1: 対象の外のケースの行が鍵のコミットの状態から変わった", left["what"])
        self.assertNotIn("前の wave の検査では通ったのに", left["what"])

    def test_lines_of_the_unlocked_case_alone_lock_the_tests_again(self) -> None:
        launch = self.to_fix_stage_by_line()
        self.write(R1_FILE, by_line(r1(5)))
        check = self.done(launch, {"status": "ok"})
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        run = saved(self.root)
        self.assert_no_worktree(check)
        self.assertEqual([], run["leftovers"])
        self.assertEqual(["OB-1"], [r["item"] for r in run["unlocks"]])


class CaseLinesTests(unittest.TestCase):
    def test_a_line_naming_an_unlocked_case_is_its_own_and_a_longer_id_is_not_the_shorter_one(self) -> None:
        text = "case R1-1 a\ncase R1-1 and R1-2\ncase R1-10\n  case R1-1 b  \r\n"
        self.assertEqual(
            {"R1-1": ["case R1-1 a", "  case R1-1 b"], "R1-10": ["case R1-10"]},
            acceptance.case_lines(text, ["R1-2"], ["R1-1", "R1-10"]),
        )


def unlock_run() -> dict:
    """R1, whose acceptance test file is acc/R1.json, with R1-1 and R1-2 locked."""
    return {
        "pbis": [{"id": "PBI-1", "kind": "acceptance", "requirements": ["R1"], "acceptance_files": {"R1": "acc/R1.json"}}],
        "scenarios": [{"id": "R1", "mode": "normal", "cases": [{"id": "R1-1", "pbi": "PBI-3"}, {"id": "R1-2", "pbi": "PBI-3"}]}],
    }


def testcase(cid: str, outcome: str, etype: str = "", message: str = "") -> dict:
    return {"name": f"test_R1[{cid}]", "outcome": outcome, "type": etype, "message": message}


class UnlockTestcaseOrderTests(unittest.TestCase):
    def test_an_error_in_the_work_tree_fails_an_implementation_fix_whichever_run_comes_first(self) -> None:
        unlock = {"cases": ["R1-2"], "fix": "implementation", "requirement": "R1", "file": "acc/R1.json"}
        locked = {"R1-1": "R1", "R1-2": "R1"}
        at_lock = [testcase("R1-1", "failure", "NotImplementedError"), testcase("R1-2", "failure", "NotImplementedError")]
        assertion = testcase("R1-2", "failure", "AssertionError", "assert 5 == 6")
        error = testcase("R1-2", "error", message="ImportError: cannot import name add")
        before = {"passed": ["R1-1"], "ran": ["R1-1", "R1-2"]}
        for label, now in (("assertion first", [assertion, error]), ("error first", [error, assertion])):
            with self.subTest(label):
                runs = {"lock_commit": (at_lock, None), "work_tree": ([testcase("R1-1", "passed"), *now], None)}
                verdict = acceptance.judge_unlock(unlock_run(), unlock, runs, locked, before)
                self.assertFalse(verdict["passed"])
                self.assertEqual("error", verdict["work_tree"]["R1-2"])
                self.assertIn("R1-2: 実装を直すと決めたが、今の実装での落ち方が assertion でも未実装でもない（error）", verdict["reasons"])


if __name__ == "__main__":
    unittest.main()
