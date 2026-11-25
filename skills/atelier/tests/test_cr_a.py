"""Regression tests for the acceptance stage's review, Red check and wave check fixes (review findings, group A)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import acceptance, plan, template  # noqa: E402

COMMAND = "python run.py {file} {junit}"


def scenario(rid: str) -> dict:
    return {"id": rid, "text": rid, "mode": "normal", "cases": [{"id": f"{rid}-1", "pbi": "PBI-9"}]}


def acceptance_pbi(pid: str, files: dict[str, str], targets: list[str]) -> dict:
    return {
        "id": pid,
        "kind": "acceptance",
        "requirements": list(files),
        "acceptance_files": dict(files),
        "targets": targets,
        "status": "passed",
        "level": "L2",
        "level_reason": "test",
    }


def make_run(*pbis: dict) -> dict:
    rids = [rid for p in pbis for rid in p["requirements"]]
    return {
        "acceptance_first": True,
        "acceptance_commands": [COMMAND],
        "scenarios": [scenario(rid) for rid in rids],
        "pbis": [*pbis, {"id": "PBI-9", "kind": "build", "targets": ["app.py"], "status": "pending"}],
        "red": {rid: {"status": "passed", "failures": 0, "reasons": [], "history": []} for rid in rids},
        "unresolved": [],
    }


def review(run: dict, verdicts: dict[str, tuple[str, str]]) -> str:
    files = [
        {"file": f, "verdict": v, **({"reason": r} if r else {})} for f, (v, r) in verdicts.items()
    ]
    return acceptance.settle_review(run, {"files": files}, template.load_template(), "rv")


def red_passes(run: dict) -> None:
    """What a passing Red check leaves: every pending requirement passed, the stage PBIs passed."""
    for entry in run["red"].values():
        if entry["status"] == "pending":
            entry["status"] = "passed"
    for pbi in acceptance.stage_pbis(run):
        pbi["status"] = "passed"


class ReviewFixCountTests(unittest.TestCase):
    def test_a_file_first_marked_fix_in_the_second_round_is_rebuilt_not_left_unresolved(self) -> None:
        run = make_run(acceptance_pbi("PBI-1", {"R1": "tests/a.py", "R2": "tests/b.py"}, ["tests/a.py", "tests/b.py"]))
        self.assertEqual("acceptance_rebuild", review(run, {"tests/a.py": ("fix", "a"), "tests/b.py": ("ok", "")}))
        red_passes(run)
        self.assertEqual(["tests/a.py", "tests/b.py"], [i["file"] for i in acceptance.review_files(run)])
        self.assertEqual("acceptance_rebuild", review(run, {"tests/a.py": ("ok", ""), "tests/b.py": ("fix", "b")}))
        self.assertEqual([], run["unresolved"])
        self.assertEqual("pending", run["red"]["R2"]["status"])
        red_passes(run)
        self.assertEqual("acceptance_reviewed", review(run, {"tests/b.py": ("fix", "b again")}))
        self.assertEqual([("R2", "acceptance_review")], [(u["requirement"], u["kind"]) for u in run["unresolved"]])
        self.assertEqual("unresolved", run["acceptance_review"]["files"]["tests/b.py"]["status"])


class ReviewVerdictPathTests(unittest.TestCase):
    def test_a_fix_verdict_spelt_another_way_still_rebuilds(self) -> None:
        run = make_run(acceptance_pbi("PBI-1", {"R1": "tests/a.py"}, ["tests/a.py"]))
        result = {"schema": plan.SCHEMA_ACCEPTANCE_REVIEW, "files": [{"file": "tests/./a.py", "verdict": "fix", "reason": "x"}]}
        self.assertEqual([], plan.validate_acceptance_review(result, ["tests/a.py"]))
        self.assertEqual("acceptance_rebuild", acceptance.settle_review(run, result, template.load_template(), "rv"))
        self.assertNotIn("tests/a.py", run["acceptance_review"]["files"])
        self.assertEqual("pending", acceptance.stage_pbis(run)[0]["status"])


class OverlappingTargetsTests(unittest.TestCase):
    def overlapping(self) -> dict:
        return make_run(
            acceptance_pbi("PBI-1", {"R1": "tests/a.py"}, ["tests/**"]),
            acceptance_pbi("PBI-2", {"R2": "tests/b.py"}, ["tests/b.py"]),
        )

    def test_a_review_rebuild_reopens_another_pbis_file_in_its_targets(self) -> None:
        run = self.overlapping()
        self.assertEqual("acceptance_rebuild", review(run, {"tests/a.py": ("fix", "a"), "tests/b.py": ("ok", "")}))
        self.assertNotIn("tests/b.py", run["acceptance_review"]["files"])
        self.assertEqual({"R1": "pending", "R2": "pending"}, {r: e["status"] for r, e in run["red"].items()})

    def test_a_red_rebuild_reopens_another_pbis_passed_requirement_in_its_targets(self) -> None:
        run = self.overlapping()
        for entry in run["red"].values():
            entry["status"] = "pending"
        verdicts = [{"requirement": "R1", "passed": False, "reasons": ["x"]}, {"requirement": "R2", "passed": True, "reasons": []}]
        self.assertEqual("red_rebuild", acceptance.settle_red(run, verdicts, "c1"))
        self.assertEqual({"R1": "failed", "R2": "pending"}, {r: e["status"] for r, e in run["red"].items()})

    def test_targets_that_do_not_cover_the_file_leave_it_settled(self) -> None:
        run = make_run(
            acceptance_pbi("PBI-1", {"R1": "tests/a.py"}, ["tests/a.py"]),
            acceptance_pbi("PBI-2", {"R2": "tests/b.py"}, ["tests/b.py"]),
        )
        review(run, {"tests/a.py": ("fix", "a"), "tests/b.py": ("ok", "")})
        self.assertEqual("ok", run["acceptance_review"]["files"]["tests/b.py"]["status"])
        self.assertEqual("passed", run["red"]["R2"]["status"])


class FailureKindTests(unittest.TestCase):
    def kind(self, message: str) -> str:
        return acceptance.failure_kind({"outcome": "failure", "type": "", "message": message})

    def test_a_word_containing_expect_is_not_an_assertion(self) -> None:
        self.assertEqual("unknown", self.kind("unexpected database error"))

    def test_assert_and_expect_words_are_assertions(self) -> None:
        for message in ("assert 1 == 2", "Expected: 2", "expect(received).toBe(expected)", "values not as expected"):
            with self.subTest(message):
                self.assertEqual("assertion", self.kind(message))


class ShellPathTests(unittest.TestCase):
    def test_a_path_with_shell_characters_is_not_put_in_a_command(self) -> None:
        run = make_run(acceptance_pbi("PBI-1", {"R1": "tests/test$draft.py", "R2": "tests/my a.py"}, ["tests/**"]))
        for entry in run["red"].values():
            entry["status"] = "pending"
        groups, unrun = acceptance.red_check_plan(run, "out")
        self.assertEqual(["R1"], [u["requirement"] for u in unrun])
        self.assertIn("$", unrun[0]["reasons"][0])
        self.assertEqual(['python run.py "tests/my a.py" out/1.xml'], [g["command"] for g in groups])
        groups, reasons = acceptance.wave_case_plan(run, ["R1", "R2"], "out")
        self.assertEqual(["R2"], [r for g in groups for r in g["requirements"]])
        self.assertEqual(1, len(reasons))
        self.assertIn("R1", reasons[0])

    def test_ordinary_paths_have_no_shell_characters(self) -> None:
        for path in ("tests/acceptance/test_add.py", "features/step_definitions/add.steps.ts", "tests/a b.py"):
            self.assertEqual("", acceptance.unsafe_path(path))


def junit(cases: list[tuple[str, str]]) -> str:
    rows = "".join(
        f'<testcase name="{name}">' + ("" if outcome == "passed" else f'<{outcome} type="AssertionError"/>') + "</testcase>"
        for name, outcome in cases
    )
    return f'<?xml version="1.0"?><testsuite>{rows}</testsuite>'


class WaveCaseScopeTests(unittest.TestCase):
    def test_a_case_id_in_another_files_results_does_not_override_its_own(self) -> None:
        run = make_run(acceptance_pbi("PBI-1", {"R1": "tests/a.py", "R2": "tests/b.py"}, ["tests/**"]))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "1.xml").write_text(junit([("test_R1-1", "passed")]), encoding="utf-8")
            (root / "2.xml").write_text(junit([("test_R2-1", "passed"), ("test_like_R1-1", "failure")]), encoding="utf-8")
            groups = [
                {"file": "tests/a.py", "junit": "1.xml", "command": "a", "requirements": ["R1"]},
                {"file": "tests/b.py", "junit": "2.xml", "command": "b", "requirements": ["R2"]},
            ]
            verdict = acceptance.judge_wave_cases(
                root, run, groups, ["R1-1", "R2-1"], {"R1-1": "PBI-9", "R2-1": "PBI-9"}, ["PBI-9"]
            )
        self.assertEqual({}, verdict["failed"])
        self.assertEqual({}, verdict["by_pbi"])
        self.assertEqual(["R1-1", "R2-1"], verdict["ran"])


class StepCheckGlobTargetTests(unittest.TestCase):
    def test_files_under_glob_and_directory_targets_are_step_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("tests/a.py", "tests/support/steps.py", "tests/support/deep/helper.ts", "tests/support/notes.txt", "lib/x.py"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("", encoding="utf-8")
            groups = [{"file": "tests/a.py", "junit": "1.xml", "command": "a", "requirements": ["R1"]}]
            for target in ("tests/support/**", "tests/support"):
                with self.subTest(target):
                    run = make_run(acceptance_pbi("PBI-1", {"R1": "tests/a.py"}, ["tests/a.py", target]))
                    files = acceptance.step_check_files(root, run, groups)
                    self.assertEqual(
                        {"tests/a.py": ["R1"], "tests/support/steps.py": ["R1"], "tests/support/deep/helper.ts": ["R1"]}, files
                    )


if __name__ == "__main__":
    unittest.main()
