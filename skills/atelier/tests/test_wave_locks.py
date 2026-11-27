"""The wave check of a locked run, driven through the CLI in temporary git repositories.

Once the acceptance tests are locked, the check at the end of a wave goes in order: (0) the copy of the lock list
outside the work tree against the sha256 recorded under ATELIER_LOCKS_HOME (a difference stops the check and asks the
user; no PBI is carried over), (1) the locked files, signatures and keys against the lock (put back; laid on the PBI
of the wave with the path in its targets, or left unresolved), (2) the locked cases run with the acceptance command
and JUnit XML, their ids against the list (a case that did not run sends the wave back; a case left red with an
objection counts as run), and (3) the assigned cases, the PBIs' checks and the plan's checks.

The fake acceptance runner reads the locked acceptance test file, then outcomes.json, which stands in for how the
implementation makes each testcase end: a testcase named there ends as given there, or is left out ("absent").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

import conductor  # noqa: E402
from atelier import acceptance, actions, guards, locks, plan, state  # noqa: E402
from tests.test_acceptance_red import GHERKIN, approve_plan, drive, finish_stage, pass_review, pbis_of, saved  # noqa: E402
from tests.test_acceptance_toggle import UNIT  # noqa: E402
from tests.test_conductor import run_cli, write_result_for  # noqa: E402
from tests.test_integration import make_pbi, make_plan  # noqa: E402
from tests.test_worker_checks import ticket_of  # noqa: E402

WAVE_RUNNER = """import json, sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
given = Path("outcomes.json")
over = json.loads(given.read_text(encoding="utf-8")) if given.is_file() else {}
rows = []
failed = False
for case in spec["cases"]:
    got = over.get(case["name"], case)
    if got == "absent":
        continue
    outcome = got.get("outcome", "failure")
    failed = failed or outcome != "passed"
    attrs = "".join(" %s=%s" % (key, quoteattr(got[key])) for key in ("message", "type") if got.get(key))
    inner = "" if outcome == "passed" else "<%s%s/>" % (outcome, attrs)
    rows.append('<testcase classname="acc" name=%s>%s</testcase>' % (quoteattr(case["name"]), inner))
junit = Path(sys.argv[2])
junit.parent.mkdir(parents=True, exist_ok=True)
junit.write_text('<?xml version="1.0"?><testsuites><testsuite name="acc">' + "".join(rows) + "</testsuite></testsuites>", encoding="utf-8")
raise SystemExit(1 if failed else 0)
"""
COMMAND = f'"{sys.executable}" fake_wave.py {{file}} {{junit}}'
ACCEPTANCE = {"id": "c1", "text": "run one acceptance file", "source": "Makefile:3", "kind": "acceptance", "command": COMMAND}
PLAN_CHECK_SCRIPT ="from pathlib import Path\nraise SystemExit(1 if Path('plan_fail').exists() else 0)\n"
PBI_CHECK = f'"{sys.executable}" ok.py'
PLAN_CHECK = f'"{sys.executable}" plan_check.py'
TEST_FILE = "acceptance/R1.json"
STUB = "def add(a, b):\n    raise NotImplementedError\n"
BUILT = "def add(a, b):\n    return a + b\n"
CASES = {"R1-1": "PBI-3", "R1-2": "PBI-3", "R1-3": "PBI-4"}
RED = {"cases": [{"name": f"test_R1[{cid}]", "message": "assert 0 == 3"} for cid in CASES]}
SIGNATURE = f"{guards.SIGNATURE_DETAIL}app add"
OBJECTION = {
    "case": "R1-2",
    "what": "the scenario wants the sum rounded",
    "why": "the example contradicts the requirement text",
    "state": "red",
    "interpretation": "left red as the requirement text says",
}


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8").rstrip()


def name(cid: str) -> str:
    return f"test_R1[{cid}]"


def outcomes(**ends: str) -> dict:
    """outcomes.json: R1-1 and R1-2 pass unless given (R1_2="failure", "skipped" or "absent"); R1-3 stays red."""
    out = {name("R1-1"): {"outcome": "passed"}, name("R1-2"): {"outcome": "passed"}}
    for key, end in ends.items():
        cid = key.replace("_", "-")
        out[name(cid)] = "absent" if end == "absent" else {"outcome": end, **({"message": "assert 4 == 3"} if end == "failure" else {})}
    return out


def wave_plan() -> dict:
    """Acceptance PBI-1 writes R1's tests and stub PBI-2 places app.add. Build PBI-3, which R1-1 and R1-2 are assigned
    to, is the first PBI after the lock; build PBI-4, which R1-3 is assigned to, needs PBI-3, so it waits a wave."""
    acc = {**make_pbi("PBI-1", targets=[TEST_FILE]), "kind": "acceptance"}
    stub = {**make_pbi("PBI-2", targets=["app.py"]), "kind": "stub"}
    build = {**make_pbi("PBI-3", depends_on=["PBI-1", "PBI-2"], targets=["app.py"]), "checks": [PBI_CHECK]}
    later = {**make_pbi("PBI-4", depends_on=["PBI-3"], targets=["other.py"]), "checks": [PBI_CHECK]}
    obj = make_plan([acc, stub, build, later])
    obj["checks"] = [PLAN_CHECK]
    obj["interfaces"] = [{"module": "app", "name": "add"}]
    obj["scenarios"] = [
        {
            "id": "R1",
            "text": "要件 R1",
            "mode": "normal",
            "gherkin": GHERKIN,
            "cases": [
                {"id": cid, "values": {"a": n, "b": 2, "sum": n + 2}, "pbi": pid, "needs": []}
                for n, (cid, pid) in enumerate(CASES.items(), start=1)
            ],
            "spare_count": 0,
        },
    ]
    return obj


class WaveLockTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.locks_home = Path(home.name).resolve()
        env = patch.dict(os.environ, {locks.HOME_ENV: str(self.locks_home)})
        env.start()
        self.addCleanup(env.stop)

    def lock_and_launch(self, *, past_g1: bool = False) -> dict:
        """Build the acceptance stage, pass the Red check and the review, lock, and give back PBI-3's launch.

        With past_g1, the launch's snapshot carries no lock, so a change of the lock during it gets past G1 and only
        the wave check sees it."""
        # The runner and the check scripts are there before the run starts, so no launch writes them.
        (self.root / "fake_wave.py").write_text(WAVE_RUNNER, encoding="utf-8")
        (self.root / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (self.root / "plan_check.py").write_text(PLAN_CHECK_SCRIPT, encoding="utf-8")
        act = approve_plan(self.root, wave_plan(), [UNIT, ACCEPTANCE])
        (self.root / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.root / ".git" / "info" / "attributes").write_bytes(b"*.json text eol=lf\n")
        (self.root / "app.py").write_bytes(STUB.encode("utf-8"))
        check = finish_stage(self.root, act, {"R1": RED})
        self.assertEqual("check", check["action"])
        review = drive(self.root)
        self.assertEqual("reviewing_acceptance", saved(self.root)["state"])
        if past_g1:
            with patch.object(conductor, "_lock_guard", lambda root, run: None):
                launch = pass_review(self.root, review)
        else:
            launch = pass_review(self.root, review)
        self.assertEqual(("launch", ["PBI-3"]), (launch["action"], pbis_of(launch)))
        self.assertTrue(saved(self.root)["lock"])
        return launch

    def build(self, launch: dict, result: dict | None = None, source: str = BUILT) -> dict:
        """The fake worker of PBI-3: writes app.py, returns, and gives back the next action (the wave check)."""
        (self.root / "app.py").write_bytes(source.encode("utf-8"))
        write_result_for(launch, result or {"status": "ok"})
        code, nxt = run_cli("done", "--root", str(self.root), "--launch-id", launch["launch_id"])
        self.assertEqual(0, code, nxt)
        return nxt

    def run_wave(self, ends: dict) -> dict:
        """How the implementation makes the cases end, then the wave check run by drive. Returns the next action."""
        (self.root / "outcomes.json").write_text(json.dumps(ends), encoding="utf-8")
        return drive(self.root)

    def test_the_check_runs_the_locked_cases_first_and_passes_with_the_later_pbis_cases_still_red(self) -> None:
        check = self.build(self.lock_and_launch())
        self.assertEqual(("checking", "check"), (saved(self.root)["state"], check["action"]))
        junit = f"docs/pbi/results/{check['launch_id']}/1.xml"
        command = COMMAND.replace("{file}", TEST_FILE).replace("{junit}", junit)
        self.assertEqual([command, PBI_CHECK, PLAN_CHECK], check["commands"])
        self.assertEqual(["PBI-3"], check["wave"]["pbis"])
        self.assertEqual(list(CASES), check["wave"]["cases"])

        nxt = self.run_wave(outcomes())
        run = saved(self.root)
        # R1-3 is PBI-4's and stays red: the acceptance command fails, but the wave passes on the cases it holds.
        self.assertEqual(["PBI-4"], pbis_of(nxt))
        self.assertEqual("passed", state.get_pbi(run, "PBI-3")["status"])
        self.assertEqual([], run.get("carry_overs") or [])
        (kept,) = run["wave_cases"]
        self.assertEqual((3, list(CASES), [], {"R1-3": "assertion"}, []), (kept["total"], kept["ran"], kept["missing"], kept["failed"], kept["sent_back"]))

    def test_a_rewritten_copy_of_the_list_stops_the_check_and_asks_the_user_without_carrying_over(self) -> None:
        launch = self.lock_and_launch()
        copy = Path(saved(self.root)["lock"]["outside"]) / locks.COPY_NAME
        original = copy.read_bytes()
        # The copy is outside the work tree, so G1 does not see it change during the launch.
        copy.write_bytes(original.replace(b'"R1-3"', b'"R1-9"'))
        ask = self.build(launch)
        run = saved(self.root)
        self.assertEqual(("asking_guard", "guard"), (run["state"], ask["kind"]))
        self.assertEqual("checking", run["return_state"])
        self.assertEqual(locks.COPY_RULE, run["guard_issue"]["rule"])
        self.assertIn(actions.LOCK_COPY_STOP, run["guard_issue"]["detail"])
        self.assertIn("sha256 が記録と違う", run["guard_issue"]["detail"])
        self.assertEqual("built", state.get_pbi(run, "PBI-3")["status"])
        self.assertEqual(([], None, None), (run.get("carry_overs") or [], run.get("wave_cases"), run.get("pending_action", {}).get("commands")))

        # Once the copy is put right, trying again runs the wave check.
        copy.write_bytes(original)
        code, check = run_cli("answer", "--root", str(self.root), "--choice", "retry")
        self.assertEqual(0, code, check)
        self.assertEqual(("checking", "check"), (saved(self.root)["state"], check["action"]))
        self.assertEqual(["PBI-4"], pbis_of(self.run_wave(outcomes())))

    def test_a_locked_file_changed_past_g1_is_put_back_and_left_unresolved_when_no_pbi_owns_it(self) -> None:
        launch = self.lock_and_launch(past_g1=True)
        locked_blob = git(self.root, "rev-parse", f"{saved(self.root)['lock']['commit']}:{TEST_FILE}")
        # The worker makes its cases pass by rewriting the locked test file, and G1 misses it.
        cheat = {"cases": [{"name": name(cid), "outcome": "passed"} for cid in CASES]}
        (self.root / TEST_FILE).write_text(json.dumps(cheat), encoding="utf-8")
        check = self.build(launch)
        self.assertEqual("check", check["action"])
        run = saved(self.root)
        self.assertEqual(locked_blob, git(self.root, "hash-object", "--", TEST_FILE))
        self.assertEqual([{"check": check["launch_id"], "files": [TEST_FILE]}], run["lock_restored"])
        (item,) = run["unresolved"]
        self.assertEqual((acceptance.WAVE_LOCK_UNRESOLVED, check["launch_id"]), (item["kind"], item["check"]))
        self.assertEqual([f"{guards.LOCKED_DETAIL}{TEST_FILE}"], item["changes"])

        # The cases run from the locked file, so they end as the implementation makes them.
        nxt = self.run_wave(outcomes(R1_2="failure"))
        run = saved(self.root)
        self.assertEqual("carried", state.get_pbi(run, "PBI-3")["status"])
        self.assertEqual(["CO-1-FIX"], pbis_of(nxt))
        self.assertNotIn("鍵と違った", run["carry_overs"][0]["output"])
        report = actions._write_report(self.root, run).read_text(encoding="utf-8")
        self.assertIn(f"{item['id']}（wave の検査 {check['launch_id']}）", report)

    def test_a_signature_changed_past_g1_fails_the_pbi_that_owns_its_file(self) -> None:
        launch = self.lock_and_launch(past_g1=True)
        check = self.build(launch, source="def add(a, b, c=0):\n    return a + b + c\n")
        run = saved(self.root)
        (detail,) = run["wave_check"]["lock_failures"]["PBI-3"]
        self.assertTrue(detail.startswith(SIGNATURE), detail)
        self.assertEqual([{"check": check["launch_id"], "files": [], "signatures": ["app add"]}], run["lock_restored"])
        self.assertFalse(run.get("unresolved"))

        nxt = self.run_wave(outcomes())
        run = saved(self.root)
        self.assertEqual("carried", state.get_pbi(run, "PBI-3")["status"])
        (co,) = run["carry_overs"]
        self.assertEqual(["PBI-3"], co["pbis"])
        self.assertIn(f"PBI-3: 鍵と違った: {SIGNATURE}", co["output"])
        self.assertEqual(["CO-1-FIX"], pbis_of(nxt))
        self.assertNotIn("wave_check", run)

    def test_cases_that_did_not_run_send_the_wave_back(self) -> None:
        for label, ends, reason in (
            ("skipped", outcomes(R1_2="skipped"), "R1-2: 流れなかった"),
            ("left out", outcomes(R1_2="absent"), "R1-2: 流れなかった"),
            ("another wave's case left out", outcomes(R1_3="absent"), "R1-3: 流れなかった"),
        ):
            with self.subTest(label):
                self.setUp()
                nxt = self.run_wave_after_build(ends)
                run = saved(self.root)
                if reason.startswith("R1-3"):
                    # R1-3 is PBI-4's, and no PBI of the wave has its test file in its targets: it is left unresolved,
                    # and PBI-3 is not failed for it.
                    self.assertEqual("passed", state.get_pbi(run, "PBI-3")["status"])
                    self.assertEqual([], run.get("carry_overs") or [])
                    (item,) = run["unresolved"]
                    self.assertEqual((acceptance.WAVE_CASES_UNRESOLVED, ["R1-3"]), (item["kind"], item["cases"]))
                    self.assertIn(reason, item["reason"])
                    self.assertEqual(["PBI-4"], pbis_of(nxt))
                    continue
                self.assertEqual("carried", state.get_pbi(run, "PBI-3")["status"])
                (co,) = run["carry_overs"]
                self.assertEqual((["PBI-3"], [PBI_CHECK]), (co["pbis"], co["commands"]))
                self.assertIn(f"PBI-3: {reason}", co["output"])
                self.assertEqual(["CO-1-FIX"], pbis_of(nxt))
                self.assertEqual([acceptance.WAVE_CASES_RULE], run["last_failed_rules"])
                self.assertEqual(["PBI-3"], run["wave_cases"][-1]["sent_back"])
                # The fix PBI is judged on the carried PBI's cases.
                self.assertEqual(["R1-1", "R1-2"], ticket_of(nxt)["cases"])

    def test_an_assigned_case_that_fails_carries_its_pbi_over(self) -> None:
        nxt = self.run_wave_after_build(outcomes(R1_1="failure"))
        run = saved(self.root)
        self.assertEqual("carried", state.get_pbi(run, "PBI-3")["status"])
        self.assertIn("PBI-3: R1-1: 割り当てたケースが落ちた（assertion）", run["carry_overs"][0]["output"])
        self.assertEqual(["CO-1-FIX"], pbis_of(nxt))

    def test_a_case_left_red_with_an_objection_counts_as_run_and_is_not_sent_back(self) -> None:
        for label, ends in (("red", outcomes(R1_2="failure")), ("not run", outcomes(R1_2="absent"))):
            with self.subTest(label):
                self.setUp()
                nxt = self.run_wave_after_build(ends, {"status": "ok", "objections": [OBJECTION]})
                run = saved(self.root)
                self.assertEqual(["PBI-4"], pbis_of(nxt))
                self.assertEqual("passed", state.get_pbi(run, "PBI-3")["status"])
                self.assertEqual([], run.get("carry_overs") or [])
                kept = run["wave_cases"][-1]
                self.assertEqual((["R1-2"], [], list(CASES)), (kept["excused"], kept["missing"], kept["ran"]))

    def test_the_pbis_checks_and_the_plans_checks_are_judged_as_before_after_the_cases(self) -> None:
        launch = self.lock_and_launch()
        self.build(launch)
        (self.root / "plan_fail").write_bytes(b"")
        nxt = self.run_wave(outcomes())
        run = saved(self.root)
        # The plan's check names no test file of PBI-3's targets: it is left unresolved and PBI-3 passes.
        self.assertEqual("passed", state.get_pbi(run, "PBI-3")["status"])
        self.assertEqual([], run.get("carry_overs") or [])
        (item,) = run["unresolved"]
        self.assertEqual((acceptance.CHECK_UNRESOLVED, PLAN_CHECK), (item["kind"], item["command"]))
        self.assertEqual([], run["last_failed_rules"])
        self.assertEqual(["PBI-4"], pbis_of(nxt))

    def run_wave_after_build(self, ends: dict, result: dict | None = None) -> dict:
        self.build(self.lock_and_launch(), result)
        return self.run_wave(ends)


class UnlockedWaveTests(unittest.TestCase):
    def test_a_run_without_acceptance_first_checks_the_wave_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for first, lock in ((False, None), (False, {"commit": "0" * 40}), (True, None)):
                with self.subTest(acceptance_first=first, lock=bool(lock)):
                    run = state.new_run_state(root, root / "goal.md", None)
                    run.update(acceptance_first=first, lock=lock, state="checking", plan_checks=["make all"])
                    run["pbis"] = [{"id": "PBI-1", "status": "built", "checks": ["make one"]}]
                    act = actions._make_wave_check(root, run)
                    self.assertEqual(plan.wave_commands(run["pbis"], ["make all"]), act["commands"])
                    self.assertNotIn("wave", act)


if __name__ == "__main__":
    unittest.main()
