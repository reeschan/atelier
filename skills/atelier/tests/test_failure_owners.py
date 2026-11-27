"""Whose failure a failed wave check is, in three steps, driven through the CLI (Seam 1).

(1) The PBIs with the failed command in their own checks; else (2) the PBIs of the wave with a test file in their
targets that the command's output names (a path or a unittest module name); else (3) none: the failure goes on the
run's unresolved list and the wave's PBIs pass. A wave is never failed as a whole. Fix PBIs go by the same steps.
In a locked run, a case no PBI of the wave holds goes by the same steps: when it did not run, and when it passed in
an earlier wave check and is red now.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

import conductor  # noqa: E402
from atelier import acceptance, state  # noqa: E402
from tests import test_wave_locks  # noqa: E402
from tests.test_acceptance_red import pbis_of, saved  # noqa: E402
from tests.test_conductor import reach_building, run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo  # noqa: E402
# The test case class is not imported by name, so its tests do not run again from this module.
from tests.test_wave_locks import TEST_FILE, name, outcomes  # noqa: E402

PLAN_COMMAND = "python -m unittest"
# PBI-1 and PBI-2 are the first wave; PBI-3 needs PBI-1, so it is the next one.
WAVES = [
    {"id": "PBI-1", "title": "Lower the box", "depends_on": [], "targets": ["box.py", "tests/test_box.py"], "checks": ["check-1"]},
    {"id": "PBI-2", "title": "Show the box", "depends_on": [], "targets": ["view.py", "tests/test_view.py"], "checks": ["check-2"]},
    {"id": "PBI-3", "title": "Log the box", "depends_on": ["PBI-1"], "targets": ["log.py"], "checks": ["check-3"]},
]
ONE_WAVE = WAVES[:2]


def done(tmp: Path, act: dict, payload: dict) -> dict:
    write_result_for(act, payload)
    code, nxt = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0, nxt
    return nxt


def build_wave(tmp: Path, act: dict) -> dict:
    while act["action"] in ("launch", "launches"):
        act = done(tmp, act, {"status": "ok"})
    assert act["action"] == "check", act
    return act


def checked(commands: dict[str, str]) -> dict:
    """A wave check result as the conductor writes it: each failed command's output after its "$ <command>" line."""
    failed = {cmd: out for cmd, out in commands.items() if out is not None}
    output = "\n".join([*(f"$ {cmd}\n{out}" for cmd, out in failed.items()), *(f"$ {cmd} (ok)" for cmd in commands if cmd not in failed)])
    return {
        "exit_code": 1 if failed else 0,
        "output": output,
        "commands": [{"command": cmd, "exit_code": 1 if cmd in failed else 0} for cmd in commands],
    }


def statuses(tmp: Path) -> dict[str, str]:
    return {p["id"]: p["status"] for p in state.load_state(tmp)["pbis"]}


class FailureOwnerSteps(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        init_git_repo(self.tmp)

    def first_check(self, plan: list[dict] = WAVES) -> dict:
        return build_wave(self.tmp, reach_building(self.tmp, pbis=plan))

    def test_1_a_failed_check_of_a_pbi_carries_only_that_pbi_over(self) -> None:
        act = self.first_check()
        nxt = done(self.tmp, act, checked({"check-1": "AssertionError: 1 != 2", "check-2": None, PLAN_COMMAND: None}))
        self.assertEqual(("launch", "CO-1-FIX"), (nxt["action"], nxt["pbi"]))
        self.assertEqual({"PBI-1": "carried", "PBI-2": "passed"}, {k: v for k, v in statuses(self.tmp).items() if k in ("PBI-1", "PBI-2")})
        run = state.load_state(self.tmp)
        self.assertEqual([(["PBI-1"], ["check-1"])], [(c["pbis"], c["commands"]) for c in run["carry_overs"]])
        self.assertFalse(run.get("unresolved"))

    def test_2_a_plan_check_naming_a_test_file_of_a_pbi_carries_only_that_pbi_over(self) -> None:
        for label, out in (
            ("unittest module", "FAIL: test_lower (tests.test_box.BoxTests.test_lower)\n  File \"view.py\", line 3\nAssertionError"),
            ("path", 'Traceback:\n  File "C:\\work\\tests\\test_box.py", line 12, in test_lower\n  File "view.py", line 3\nAssertionError'),
        ):
            with self.subTest(label):
                self.setUp()
                act = self.first_check()
                nxt = done(self.tmp, act, checked({"check-1": None, "check-2": None, PLAN_COMMAND: out}))
                self.assertEqual(("launch", "CO-1-FIX"), (nxt["action"], nxt["pbi"]))
                pbis = statuses(self.tmp)
                # view.py is PBI-2's target but not a test file: PBI-2 passes.
                self.assertEqual(("carried", "passed"), (pbis["PBI-1"], pbis["PBI-2"]))
                run = state.load_state(self.tmp)
                self.assertEqual([(["PBI-1"], [PLAN_COMMAND])], [(c["pbis"], c["commands"]) for c in run["carry_overs"]])
                self.assertFalse(run.get("unresolved"))

    def test_2_reads_each_failed_command_s_whole_output(self) -> None:
        named = "FAIL: test_lower (tests.test_box.BoxTests.test_lower)"
        # A line that only starts like another command's header stays in the output read from the combined text.
        combined = checked({"check-1": None, "check-2": None, PLAN_COMMAND: f"$ check-2 --help\nusage: check-2\n{named}"})
        # The output kept with the command's exit code is read before the combined text, which may be cut short.
        own = checked({"check-1": None, "check-2": None, PLAN_COMMAND: "(the end of a long output)"})
        own["commands"][-1]["output"] = f"{named}\n(the end of a long output)"
        for label, result in (("combined output", combined), ("per-command output", own)):
            with self.subTest(label):
                self.setUp()
                nxt = done(self.tmp, self.first_check(), result)
                self.assertEqual(("launch", "CO-1-FIX"), (nxt["action"], nxt["pbi"]))
                self.assertEqual(("carried", "passed"), (statuses(self.tmp)["PBI-1"], statuses(self.tmp)["PBI-2"]))
                self.assertFalse(state.load_state(self.tmp).get("unresolved"))

    def test_the_check_keeps_each_failed_command_s_output_with_its_exit_code(self) -> None:
        py = f'"{sys.executable}"'
        act = {
            "commands": [f"{py} -c \"print('$ lint --help'); print('tests/test_box.py'); raise SystemExit(1)\"", f"{py} -c \"print('ok')\""],
            "result": str(self.tmp / "r.json"),
        }
        self.assertEqual(1, conductor._run_check(self.tmp, act, None, 60))
        failed, passed = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))["commands"]
        self.assertEqual(["$ lint --help", "tests/test_box.py"], failed["output"].strip().splitlines())
        self.assertNotIn("output", passed)

    def test_3_a_failure_no_pbi_owns_is_left_unresolved_and_the_run_goes_on(self) -> None:
        tail = "\n".join(f"line {n}" for n in range(30)) + "\nImportError: cannot import name 'x' from 'helpers'"
        for label, plan, after in (("next wave", WAVES, ("launch", "worker", "PBI-3")), ("last wave", ONE_WAVE, ("launch", "reviewer", None))):
            with self.subTest(label):
                self.setUp()
                act = self.first_check(plan)
                nxt = done(self.tmp, act, checked({"check-1": None, "check-2": None, PLAN_COMMAND: tail}))
                self.assertEqual(after, (nxt["action"], nxt["role"], nxt.get("pbi")))
                run = state.load_state(self.tmp)
                self.assertEqual(("passed", "passed"), (statuses(self.tmp)["PBI-1"], statuses(self.tmp)["PBI-2"]))
                self.assertEqual([], run.get("carry_overs") or [])
                (item,) = run["unresolved"]
                self.assertEqual(
                    (acceptance.CHECK_UNRESOLVED, PLAN_COMMAND, act["launch_id"]), (item["kind"], item["command"], item["check"])
                )
                self.assertEqual(state.OUTPUT_TAIL_LINES, len(item["output"].splitlines()))
                self.assertTrue(item["output"].endswith("from 'helpers'"))
                self.assertNotIn("line 0\n", item["output"])
                records = [json.loads(ln) for ln in (self.tmp / "docs" / "pbi" / "record.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual({"PBI-1": "pass", "PBI-2": "pass"}, {r["pbi"]: r["result"] for r in records if r.get("type") == "check" and r.get("pbi")})
                line = next(ln for ln in acceptance.unresolved_lines(run) if ln.startswith(f"- {item['id']}"))
                self.assertIn(PLAN_COMMAND, line)
                self.assertIn("from 'helpers'", line)

    def test_a_failure_laid_by_step_1_or_2_leaves_the_other_commands_unresolved(self) -> None:
        act = self.first_check()
        nxt = done(self.tmp, act, checked({"check-1": None, "check-2": "E2", PLAN_COMMAND: "SyntaxError in setup.cfg"}))
        # PBI-3 needs only PBI-1, which passed, so it goes with the fix PBI.
        self.assertEqual(["CO-1-FIX", "PBI-3"], sorted(pbis_of(nxt)))
        run = state.load_state(self.tmp)
        self.assertEqual(("passed", "carried"), (statuses(self.tmp)["PBI-1"], statuses(self.tmp)["PBI-2"]))
        self.assertEqual([(["PBI-2"], ["check-2"])], [(c["pbis"], c["commands"]) for c in run["carry_overs"]])
        self.assertEqual([(acceptance.CHECK_UNRESOLVED, PLAN_COMMAND)], [(u["kind"], u["command"]) for u in run["unresolved"]])

    def test_a_fix_pbi_goes_by_the_same_steps(self) -> None:
        for label, out, fix_status in (
            ("names nothing", "ImportError: helpers", "passed"),
            ("names a test file of a PBI not in the wave", "FAIL: test_show (tests.test_view.ViewTests.test_show)", "passed"),
            ("names a test file of the fix PBI's targets", "FAIL: test_lower (tests.test_box.BoxTests.test_lower)", "failed"),
        ):
            with self.subTest(label):
                self.setUp()
                act = self.first_check()
                act = done(self.tmp, act, checked({"check-1": "E1", "check-2": None, PLAN_COMMAND: None}))
                self.assertEqual("CO-1-FIX", act["pbi"])
                act = build_wave(self.tmp, act)
                self.assertEqual(["check-1", PLAN_COMMAND], act["commands"])
                nxt = done(self.tmp, act, checked({"check-1": None, PLAN_COMMAND: out}))
                run = state.load_state(self.tmp)
                co = run["carry_overs"][0]
                if fix_status == "passed":
                    # The plan's check is not laid on the fix PBI as a whole: it is left unresolved.
                    self.assertEqual(("passed", "passed", "fixed_by_pbi"), (statuses(self.tmp)["CO-1-FIX"], statuses(self.tmp)["PBI-1"], co["status"]))
                    self.assertEqual([(acceptance.CHECK_UNRESOLVED, PLAN_COMMAND)], [(u["kind"], u["command"]) for u in run["unresolved"]])
                    self.assertEqual(("worker", "PBI-3"), (nxt["role"], nxt["pbi"]))
                else:
                    self.assertEqual(("CO-1-FIX", 2, [PLAN_COMMAND]), (nxt["pbi"], co["tries"], co["commands"]))
                    self.assertFalse(run.get("unresolved"))


_WAVE_PLAN = test_wave_locks.wave_plan


def regression_plan() -> dict:
    """The wave lock plan with a test file in PBI-4's targets."""
    obj = _WAVE_PLAN()
    later = next(p for p in obj["pbis"] if p["id"] == "PBI-4")
    later["targets"] = ["other.py", "tests/test_other.py"]
    return obj


class LockedCaseOwners(unittest.TestCase):
    """The locked run of test_wave_locks: PBI-3 holds R1-1 and R1-2 and is the first wave; PBI-4 holds R1-3."""

    # Its helpers, without running its tests again here.
    setUp = test_wave_locks.WaveLockTests.setUp
    lock_and_launch = test_wave_locks.WaveLockTests.lock_and_launch
    build = test_wave_locks.WaveLockTests.build
    run_wave = test_wave_locks.WaveLockTests.run_wave

    def pass_first_wave(self) -> dict:
        with patch.object(test_wave_locks, "wave_plan", regression_plan):
            launch = self.lock_and_launch()
        self.build(launch)
        nxt = self.run_wave(outcomes())
        self.assertEqual(["PBI-4"], pbis_of(nxt))
        self.assertEqual(["R1-1", "R1-2"], saved(self.root)["wave_cases"][-1]["passed"])
        return nxt

    def test_a_case_that_passed_before_and_is_red_now_goes_by_the_steps(self) -> None:
        for label, message, owner in (
            ("names a test file of the wave", "AssertionError: tests/test_other.py helper changed", "PBI-4"),
            ("names nothing", "assert 4 == 3", None),
        ):
            with self.subTest(label):
                self.setUp()
                launch = self.pass_first_wave()
                check = self.build(launch)
                self.assertEqual(["PBI-4"], check["wave"]["pbis"])
                ends = outcomes(R1_3="passed")
                ends[name("R1-1")] = {"outcome": "failure", "message": message}
                nxt = self.run_wave(ends)
                run = saved(self.root)
                self.assertEqual({"R1-1": "assertion"}, run["wave_cases"][-1]["failed"])
                if owner:
                    self.assertEqual("carried", state.get_pbi(run, "PBI-4")["status"])
                    (co,) = run["carry_overs"]
                    self.assertEqual(["PBI-4"], co["pbis"])
                    self.assertIn("PBI-4: R1-1: 前の wave で通ったケースが落ちた", co["output"])
                    # PBI-4 was the last PBI: the carry-over waits for the fix stage.
                    self.assertEqual(("launch", "reviewer"), (nxt["action"], nxt["role"]))
                    self.assertFalse(run.get("unresolved"))
                else:
                    self.assertEqual("passed", state.get_pbi(run, "PBI-4")["status"])
                    self.assertEqual([], run.get("carry_overs") or [])
                    (item,) = run["unresolved"]
                    self.assertEqual(
                        (acceptance.WAVE_CASES_RED_UNRESOLVED, ["R1-1"], check["launch_id"]), (item["kind"], item["cases"], item["check"])
                    )
                    self.assertEqual(("launch", "reviewer"), (nxt["action"], nxt["role"]))
                    self.assertIn(f"{item['id']}（wave の検査 {check['launch_id']}）", "\n".join(acceptance.unresolved_lines(run)))

    def test_a_case_of_a_later_pbi_left_out_is_not_laid_on_the_wave(self) -> None:
        launch = self.lock_and_launch()
        self.build(launch)
        nxt = self.run_wave(outcomes(R1_3="absent"))
        run = saved(self.root)
        self.assertEqual("passed", state.get_pbi(run, "PBI-3")["status"])
        (item,) = run["unresolved"]
        self.assertEqual((acceptance.WAVE_CASES_UNRESOLVED, ["R1-3"]), (item["kind"], item["cases"]))
        self.assertEqual(["PBI-4"], pbis_of(nxt))


class CaseOwnerSteps(unittest.TestCase):
    PBIS = [
        {"id": "PBI-3", "targets": ["app.py"], "checks": ["make one"]},
        {"id": "PBI-4", "targets": ["other.py", "acceptance/"], "checks": ["make two"]},
        {"id": "PBI-5", "targets": ["tests/test_other.py"], "checks": ["run R1"]},
    ]

    def test_steps_for_a_case_with_no_holder_in_the_wave(self) -> None:
        group = {"file": TEST_FILE, "command": "run R1"}
        self.assertEqual(["PBI-5"], acceptance.case_owners(self.PBIS, group, ""))
        self.assertEqual(["PBI-4"], acceptance.case_owners(self.PBIS, group, "", by_checks=False))
        self.assertEqual(["PBI-4", "PBI-5"], acceptance.case_owners(self.PBIS, group, "see tests/test_other.py", by_checks=False))
        self.assertEqual([], acceptance.case_owners(self.PBIS[:1], group, "assert 4 == 3"))


class TestFileNames(unittest.TestCase):
    def test_a_test_file_is_named_by_its_path_or_its_module(self) -> None:
        path = "skills/atelier/tests/test_box.py"
        for text in (
            "FAIL: test_a (tests.test_box.BoxTests.test_a)",
            "ERROR: skills.atelier.tests.test_box (unittest.loader._FailedTest)",
            'File "C:\\x\\skills\\atelier\\tests\\test_box.py", line 3',
            "tests/test_box.py::test_a FAILED",
            "test_box.py:12: AssertionError",
        ):
            with self.subTest(text=text):
                self.assertTrue(state.names_test_file(text, path))
        for text in ("tests.test_boxes", "mytest_box.py", "test_box (a test method of another module)", "tests/test_box.pyc"):
            with self.subTest(text=text):
                self.assertFalse(state.names_test_file(text, path))

    def test_step_2_reads_only_test_files_in_the_targets(self) -> None:
        pbis = [{"id": "PBI-1", "targets": ["box.py"]}, {"id": "PBI-2", "targets": ["src/box.test.ts"]}]
        self.assertEqual(["PBI-2"], state.named_test_owners("FAIL src/box.test.ts\n at box.py:3", pbis))
        self.assertEqual(([], None), state.failure_owners(pbis, "npm test", "at box.py:3"))
        self.assertEqual((["PBI-2"], "targets"), state.failure_owners(pbis, "npm test", "FAIL src/box.test.ts"))
        pbis[0]["checks"] = ["npm test"]
        self.assertEqual((["PBI-1"], "checks"), state.failure_owners(pbis, "npm test", "FAIL src/box.test.ts"))

    def test_a_path_with_its_folder_names_only_the_file_in_that_folder(self) -> None:
        for unit, integration in (
            ("tests/unit/test_box.py", "tests/integration/test_box.py"),
            ("tests\\unit\\test_box.py", "tests\\integration\\test_box.py"),
        ):
            pbis = [{"id": "PBI-1", "targets": [unit]}, {"id": "PBI-2", "targets": [integration]}]
            for text in (
                "FAILED tests/unit/test_box.py::test_total - assert 1 == 2",
                "tests\\unit\\test_box.py:12: AssertionError",
                'File "C:\\work\\tests\\unit\\test_box.py", line 12, in test_total',
                "FAILED /work/tests/unit/test_box.py::test_total\ntest_box.py:12: AssertionError",
                "see tests/unit/test_box.py.",
                "FAIL: test_total (tests.unit.test_box.BoxTests.test_total)",
            ):
                with self.subTest(target=unit, text=text):
                    self.assertEqual((["PBI-1"], "targets"), state.failure_owners(pbis, "pytest", text))
            # A file name alone does not tell the two files apart: both are named.
            for text in ("test_box.py:12: AssertionError", "./test_box.py::test_total FAILED"):
                with self.subTest(target=unit, text=text):
                    self.assertEqual((["PBI-1", "PBI-2"], "targets"), state.failure_owners(pbis, "pytest", text))

    def test_a_path_with_a_folder_does_not_name_a_file_of_that_name_at_the_root(self) -> None:
        for unit in ("tests/unit/test_box.py", "tests\\unit\\test_box.py"):
            pbis = [{"id": "PBI-1", "targets": ["test_box.py"]}, {"id": "PBI-2", "targets": [unit]}]
            # Once the folder is given, a file name alone is taken to be that file's too.
            for text in (
                "FAILED tests/unit/test_box.py::test_total",
                "tests\\unit\\test_box.py:12: AssertionError",
                "FAILED tests/unit/test_box.py::test_total\ntest_box.py:12: AssertionError",
                "FAIL: test_total (tests.unit.test_box.BoxTests.test_total)",
            ):
                with self.subTest(target=unit, text=text):
                    self.assertEqual((["PBI-2"], "targets"), state.failure_owners(pbis, "pytest", text))
            # A path with no folder is the file at the root; the other file ends with it too.
            for text in ("FAILED test_box.py::test_total", "./test_box.py:12: AssertionError"):
                with self.subTest(target=unit, text=text):
                    self.assertEqual((["PBI-1", "PBI-2"], "targets"), state.failure_owners(pbis, "pytest", text))
            # The root file's own unittest module name, and an absolute path, which does not tell where the root is.
            text = 'File "C:\\work\\test_box.py", line 3\nFAIL: test_total (test_box.BoxTests.test_total)'
            self.assertEqual((["PBI-1"], "targets"), state.failure_owners(pbis, "unittest", text))

    def test_a_path_with_spaces_in_its_folders_is_read_whole(self) -> None:
        for user, other in (
            ("tests/user flow/test_checkout.py", "tests/other flow/test_checkout.py"),
            ("tests\\user flow\\test_checkout.py", "tests\\other flow\\test_checkout.py"),
        ):
            pbis = [{"id": "PBI-1", "targets": [user]}, {"id": "PBI-2", "targets": [other]}]
            for text in (
                "FAILED tests/user flow/test_checkout.py::test_total - assert 1 == 2",
                "tests\\user flow\\test_checkout.py:12: AssertionError",
                'File "C:\\work\\tests\\user flow\\test_checkout.py", line 12, in test_total',
                "FAILED user flow/test_checkout.py::test_total",
            ):
                with self.subTest(target=user, text=text):
                    self.assertEqual((["PBI-1"], "targets"), state.failure_owners(pbis, "pytest", text))
            # Once the folder is given, a file name alone is the other file's.
            text = "FAILED tests/other flow/test_checkout.py::test_total\ntest_checkout.py:12: AssertionError"
            self.assertEqual((["PBI-2"], "targets"), state.failure_owners(pbis, "pytest", text))


if __name__ == "__main__":
    unittest.main()
