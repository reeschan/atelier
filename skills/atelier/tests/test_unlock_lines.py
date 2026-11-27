"""An unlock's check against the lines of the file's other locked cases, and against the order of the testcases.

The fix worker of an unlock can change the whole of the opened acceptance test file. The check runs the file at the
lock commit, where the implementation is not, so a change to another locked case's content that keeps the case green
now cannot be seen from how it ends. A line diff against the lock commit therefore lets the fix change, remove or add
only lines that name one of the unlock's cases and no other locked case: a line two cases share and a line that names
no case are refused. The Seam 1 tests drive the CLI on a temporary git repository with the fake runner of
test_unlock, whose locked file holds each case on its own line unless a test locks it in another layout. A testcase
that ran twice ends as its worst run, and an error is worse than an assertion, whichever comes first in the JUnit XML.
Every test points ATELIER_LOCKS_HOME at a temporary folder.
"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from unittest.mock import patch

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import acceptance  # noqa: E402
from tests import test_unlock  # noqa: E402
from tests.test_acceptance_red import drive, saved  # noqa: E402
from tests.test_unlock import R1_FILE, UnlockTestBase, by_line, r1  # noqa: E402

LINE_REASON = "R1-1: 対象の外のケースの行が鍵のコミットの状態から変わった"
NO_ID_REASON = f"{R1_FILE}: どのケースの id も無い行が鍵のコミットの状態から変わった"


def one_line(spec: dict) -> str:
    """An acceptance test file of the fake runner with every case on one line."""
    return json.dumps(spec) + "\n"


def name_then_values(spec: dict) -> str:
    """An acceptance test file of the fake runner where R1-1's values sit on a line of their own, without its id (a
    file of one case, R2's, is written one case per line)."""
    if len(spec["cases"]) != 2:
        return by_line(spec)
    one, two = spec["cases"]
    values = json.dumps({k: v for k, v in one.items() if k != "name"})[1:]
    return '{"cases": [\n{"name": %s,\n %s,\n%s\n]}\n' % (json.dumps(one["name"]), values, json.dumps(two))


@contextmanager
def locked_as(layout: Callable[[dict], str]) -> Iterator[None]:
    """The fake acceptance worker of test_unlock writes the locked files in layout."""

    def write(root: Path, specs: dict[str, dict]) -> None:
        for rid, spec in specs.items():
            path = root / "acceptance" / f"{rid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(layout(spec).encode("utf-8"))

    with patch.object(test_unlock, "write_by_line", write):
        yield


# The worker fixes R1-2 and also gives R1-1 inputs the fake runner ignores: R1-1 still fails at the lock commit and
# passes now, so only the lines show the change.
CHANGED = {"cases": [{"name": "test_R1[R1-1]", "a": 0, "b": 3, "expect": 3}, {"name": "test_R1[R1-2]", "expect": 5}]}


class UnlockOtherCaseLinesTests(UnlockTestBase):
    def check_fails(self, launch: dict, text: str) -> str:
        """The fix worker writes R1's file as text; the check fails and the item is left over. Returns why."""
        self.write(R1_FILE, text)
        check = self.done(launch, {"status": "ok"})
        self.assertEqual(("check", "unlock"), (check["action"], check["stage"]))
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        left = self.assert_not_locked_again(saved(self.root), check)
        self.assertNotIn("前の wave の検査では通ったのに", left["what"])
        return left["what"]

    def test_a_changed_line_of_another_case_that_stays_green_fails_the_check(self) -> None:
        launch = self.to_fix_stage("scenario")
        self.assertIn('{"name": "test_R1[R1-1]", "expect": 3},\n', (self.root / R1_FILE).read_text(encoding="utf-8"))
        self.assertIn(LINE_REASON, self.check_fails(launch, by_line(CHANGED)))

    def test_a_line_another_case_shares_with_the_unlocked_case_fails_the_check(self) -> None:
        with locked_as(one_line):
            launch = self.to_fix_stage("scenario")
        self.assertEqual(one_line(r1()), (self.root / R1_FILE).read_text(encoding="utf-8"))
        what = self.check_fails(launch, one_line(CHANGED))
        self.assertIn(LINE_REASON, what)
        self.assertNotIn(NO_ID_REASON, what)

    def test_a_changed_line_that_names_no_case_fails_the_check(self) -> None:
        with locked_as(name_then_values):
            launch = self.to_fix_stage("scenario")
        self.assertIn('{"name": "test_R1[R1-1]",\n "expect": 3},\n', (self.root / R1_FILE).read_text(encoding="utf-8"))
        # R1-1's line with its id stays; the line under it, which holds R1-1's values, changes.
        what = self.check_fails(launch, name_then_values(CHANGED))
        self.assertIn(NO_ID_REASON, what)
        self.assertNotIn(LINE_REASON, what)

    def test_lines_of_the_unlocked_case_alone_lock_the_tests_again(self) -> None:
        launch = self.to_fix_stage("scenario")
        check = self.fix_lines(launch, r1(5))
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        run = saved(self.root)
        self.assert_no_worktree(check)
        self.assertEqual([], run["leftovers"])
        self.assertEqual(["OB-1"], [r["item"] for r in run["unlocks"]])


class CaseLinesTests(unittest.TestCase):
    OLD = "head\ncase R1-1 a\ncase R1-2 a\ncase R1-1 and R1-2\ncase R1-10\n"

    def lines(self, new: str) -> tuple[dict[str, list[str]], list[str]]:
        return acceptance.case_lines(self.OLD, new, ["R1-2", "R1-3"], ["R1-1", "R1-10"])

    def test_lines_that_name_only_the_unlocked_cases_may_change_and_be_added(self) -> None:
        new = "head\n\ncase R1-1 a  \r\ncase R1-2 b\ncase R1-3 new\ncase R1-1 and R1-2\n\ncase R1-10\n"
        self.assertEqual(({"R1-1": [], "R1-10": []}, []), self.lines(new))

    def test_a_shared_line_or_a_line_of_another_case_may_not_change(self) -> None:
        new = "head\ncase R1-1 a\ncase R1-2 a\ncase R1-1 and R1-2 b\ncase R1-10 b\ncase R1-3 and R1-1\n"
        self.assertEqual(
            (
                {
                    "R1-1": ["case R1-1 and R1-2", "case R1-1 and R1-2 b", "case R1-3 and R1-1"],
                    "R1-10": ["case R1-10", "case R1-10 b"],
                },
                [],
            ),
            self.lines(new),
        )

    def test_a_line_of_no_case_may_not_change_be_removed_or_be_added(self) -> None:
        for label, new, got in (
            ("changed", "head b\ncase R1-1 a\ncase R1-2 a\ncase R1-1 and R1-2\ncase R1-10\n", ["head", "head b"]),
            ("removed", "case R1-1 a\ncase R1-2 a\ncase R1-1 and R1-2\ncase R1-10\n", ["head"]),
            ("added", self.OLD + "tail\n", ["tail"]),
        ):
            with self.subTest(label):
                self.assertEqual(({"R1-1": [], "R1-10": []}, got), self.lines(new))


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
