"""Unlock fixes (鍵を外す修正), driven through the CLI in temporary git repositories (Seam 1).

When the triage takes the lock off locked cases (an `unlock` on its decision) and the user approves the fix list, the
fix stage launches the unlock's fix worker first, as an acceptance PBI fixing its acceptance test file: its ticket names
the cases and the file, and G1 lets that launch change that file and nothing else. The run then checks the fixed and
added lines with the acceptance command: in a temporary git worktree at the lock commit, outside the work tree, they
must fail, and in the work tree they must pass (the scenario is fixed) or fail with an assertion (the implementation is
fixed). When they do, the tests are locked again: the list's unlocks get the record, and the copy outside the work tree
and the hash move on. When they do not, the file goes back to the lock commit's content and the item is left over. The
worktree is gone after the check, also when it fails part way. Every test points ATELIER_LOCKS_HOME at a temporary
folder.

The fake acceptance runner reads the acceptance test file (a JSON list of cases, each with the sum it expects) and
app.json in the folder it runs in, which stands in for the implementation: a case passes when app.json gives its
expected sum, fails with an assertion when it gives another, and fails as not implemented when app.json is not there,
as at the lock commit (app.json is never committed).
"""

from __future__ import annotations

import hashlib
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

from atelier import acceptance, guards, locks, plan, retro, state  # noqa: E402
from tests.test_acceptance_red import GHERKIN, approve_plan, drive, finish_stage, pass_review, pbis_of, saved  # noqa: E402
from tests.test_acceptance_toggle import UNIT  # noqa: E402
from tests.test_conductor import run_cli, write_json, write_result_for  # noqa: E402
from tests.test_integration import make_pbi, make_plan  # noqa: E402
from tests.test_wave_locks import PBI_CHECK, PLAN_CHECK, PLAN_CHECK_SCRIPT  # noqa: E402
from tests.test_worker_checks import ticket_of  # noqa: E402

RUNNER = """import json, sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
impl = Path("app.json")
values = json.loads(impl.read_text(encoding="utf-8")) if impl.is_file() else None
rows = []
failed = False
for case in spec["cases"]:
    name = case["name"]
    if case.get("outcome") == "error":
        inner = '<error message="ImportError: cannot import name add"/>'
    elif case.get("outcome") == "passed":
        inner = ""
    elif values is None or name not in values:
        inner = '<failure type="NotImplementedError" message="NotImplementedError"/>'
    elif values[name] == case["expect"]:
        inner = ""
    else:
        inner = "<failure message=%s/>" % quoteattr("assert %r == %r" % (values[name], case["expect"]))
    failed = failed or bool(inner)
    rows.append('<testcase classname="acc" name=%s>%s</testcase>' % (quoteattr(name), inner))
junit = Path(sys.argv[2])
junit.parent.mkdir(parents=True, exist_ok=True)
junit.write_text('<?xml version="1.0"?><testsuites><testsuite name="acc">' + "".join(rows) + "</testsuite></testsuites>", encoding="utf-8")
raise SystemExit(1 if failed else 0)
"""
R1_FILE = "acceptance/R1.json"
R2_FILE = "acceptance/R2.json"
STUB = "def add(a, b):\n    raise NotImplementedError\n"
BUILT = "def add(a, b):\n    return a + b\n"
# The implementation: PBI-3's worker built R1-2 to its own reading (5) and left the locked case red. R1-3 is not a case
# of the plan; the implementation already gives what a line added for it would expect.
IMPL = {"test_R1[R1-1]": 3, "test_R1[R1-2]": 5, "test_R1[R1-3]": 7, "test_R2[R2-1]": 10}
OBJECTION = {
    "case": "R1-2",
    "what": "the scenario wants 2 + 2 to be 4",
    "why": "the requirement text adds one for a pair",
    "state": "red",
    "interpretation": "a pair sums to 5, as the requirement text says",
}
REASON = "the user reads R1 as the worker does"


def r1(expect_2: int = 4, **extra: object) -> dict:
    """R1's acceptance test file: R1-1 expects 3, R1-2 expects expect_2 (extra fields go on R1-2)."""
    return {"cases": [{"name": "test_R1[R1-1]", "expect": 3}, {"name": "test_R1[R1-2]", "expect": expect_2, **extra}]}


R2 = {"cases": [{"name": "test_R2[R2-1]", "expect": 10}]}


def unlock_plan() -> dict:
    """Acceptance PBI-1 writes R1's and R2's tests, stub PBI-2 places app.add, and build PBI-3 (every case is its)
    builds app.py and app.json after them. There is no E2E, so the run goes from the review to the triage."""
    acc = {**make_pbi("PBI-1", targets=[R1_FILE, R2_FILE]), "kind": "acceptance", "checks": []}
    stub = {**make_pbi("PBI-2", targets=["app.py"]), "kind": "stub", "checks": []}
    build = {**make_pbi("PBI-3", depends_on=["PBI-1", "PBI-2"], targets=["app.py", "app.json"]), "checks": [PBI_CHECK]}
    obj = make_plan([acc, stub, build])
    obj["checks"] = [PLAN_CHECK]
    obj["e2e"] = []
    obj["interfaces"] = [{"module": "app", "name": "add"}]
    obj["scenarios"] = [
        {
            "id": "R1",
            "text": "要件 R1",
            "mode": "normal",
            "gherkin": GHERKIN,
            "cases": [
                {"id": "R1-1", "values": {"a": 1, "b": 2, "sum": 3}, "pbi": "PBI-3", "needs": []},
                {"id": "R1-2", "values": {"a": 2, "b": 2, "sum": 4}, "pbi": "PBI-3", "needs": []},
            ],
            "spare_count": 0,
        },
        {
            "id": "R2",
            "text": "要件 R2",
            "mode": "normal",
            "gherkin": GHERKIN,
            "cases": [{"id": "R2-1", "values": {"a": 5, "b": 5, "sum": 10}, "pbi": "PBI-3", "needs": []}],
            "spare_count": 0,
        },
    ]
    return obj


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8").rstrip()


def worktrees(root: Path) -> list[str]:
    """The worktrees git lists for the repository."""
    return [line[len("worktree "):] for line in git(root, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


def unlock(cases: list[str], fix: str, **extra: str) -> dict:
    return {"cases": cases, "fix": fix, **extra}


def records(root: Path) -> list[dict]:
    path = root / "docs" / "pbi" / "record.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class UnlockTestBase(unittest.TestCase):
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
        # The runner and the reply files sit outside the repository; the runner runs the same way in the work tree
        # and in a worktree.
        tools = tempfile.TemporaryDirectory()
        self.addCleanup(tools.cleanup)
        self.tools = Path(tools.name).resolve()
        (self.tools / "fake_acceptance.py").write_text(RUNNER, encoding="utf-8")
        self.command = f'"{sys.executable}" "{self.tools / "fake_acceptance.py"}" {{file}} {{junit}}'
        (self.root / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (self.root / "plan_check.py").write_text(PLAN_CHECK_SCRIPT, encoding="utf-8")
        # Runs before the repository goes: a test that stops while a check waits takes its worktree away too.
        self.addCleanup(self.take_worktrees_away, self.root)

    @staticmethod
    def take_worktrees_away(root: Path) -> None:
        if not (root / ".git").is_dir():
            return
        for tree in worktrees(root)[1:]:
            locks.remove_worktree(root, tree)

    def write(self, rel: str, text: str) -> None:
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_bytes(text.encode("utf-8"))

    def write_json(self, rel: str, data: dict) -> None:
        self.write(rel, json.dumps(data) + "\n")

    def done(self, act: dict, result: dict, code: int = 0) -> dict:
        write_result_for(act, result)
        got, nxt = run_cli("done", "--root", str(self.root), "--launch-id", act["launch_id"])
        self.assertEqual(code, got, nxt)
        return nxt

    def to_triage(self, objections: list[dict] | None = None) -> dict:
        """Lock R1's and R2's tests, build PBI-3 (with the objections, by default one on R1-2) and pass its wave and
        the final review. Returns the planner's triage launch."""
        acceptance_command = {"id": "c1", "text": "run one acceptance file", "source": "Makefile:3", "kind": "acceptance", "command": self.command}
        act = approve_plan(self.root, unlock_plan(), [UNIT, acceptance_command])
        (self.root / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.root / ".git" / "info" / "attributes").write_bytes(b"*.json text eol=lf\n")
        self.write("app.py", STUB)
        check = finish_stage(self.root, act, {"R1": r1(), "R2": R2})
        self.assertEqual("check", check["action"])
        launch = pass_review(self.root, drive(self.root))
        self.assertEqual(["PBI-3"], pbis_of(launch))
        self.assertEqual([R1_FILE, R2_FILE, locks.LIST_PATH], saved(self.root)["lock"]["files"])
        self.write("app.py", BUILT)
        self.write_json("app.json", IMPL)
        check = self.done(launch, {"status": "ok", "objections": objections or [OBJECTION]})
        self.assertEqual("check", check["action"])
        review = drive(self.root)
        self.assertEqual(("launch", "reviewer"), (review["action"], review["role"]))
        self.assertEqual("passed", state.get_pbi(saved(self.root), "PBI-3")["status"])
        triage = self.done(review, {"schema": "atelier.findings/1", "findings": []})
        self.assertEqual(("launch", "planner"), (triage["action"], triage["role"]), triage)
        return triage

    def to_fix_stage(self, fix: str = "scenario", decision: str = "ask", cases: tuple[str, ...] = ("R1-2",)) -> dict:
        """The triage unlocks the cases of OB-1 (the objection on R1-2), and the user approves the fix list with it.
        Returns the next action: the launch of the unlock's fix worker."""
        triage = self.to_triage()
        item = {"id": "OB-1", "decision": decision, "reason": REASON, "unlock": unlock(list(cases), fix)}
        ask = self.done(triage, {"schema": "atelier.triage/1", "items": [item]})
        self.assertEqual(("ask", "fix_approval"), (ask["action"], ask["kind"]))
        self.lock_before = dict(saved(self.root)["lock"])
        reply = write_json(self.tools / "reply.json", {"choice": "approve", "include_ids": ["OB-1"]})
        code, launch = run_cli("answer", "--root", str(self.root), "--reply", str(reply))
        self.assertEqual(0, code, launch)
        self.assertEqual("fixing", saved(self.root)["state"])
        return launch

    def fix_lines(self, launch: dict, test_file: dict, result: dict | None = None) -> dict:
        """The fake unlock worker writes R1's test file; returns the next action (the unlock's check)."""
        self.write_json(R1_FILE, test_file)
        return self.done(launch, result or {"status": "ok"})

    def assert_no_worktree(self, check: dict) -> None:
        """The temporary worktree's folder is gone, and git lists only the work tree."""
        tree = Path(check["unlock"]["worktree"])
        self.assertFalse(tree.exists(), tree)
        self.assertFalse(tree.parent.exists(), tree.parent)
        self.assertEqual(1, len(worktrees(self.root)), worktrees(self.root))

    def locked_blob(self, name: str) -> str:
        return git(self.root, "rev-parse", f"{saved(self.root)['lock']['commit']}:{name}")

    def assert_not_locked_again(self, run: dict, check: dict) -> dict:
        """The lock did not move, the opened file is back to its locked content, and the unlock is left over. Returns
        the leftover."""
        self.assert_no_worktree(check)
        self.assertEqual((self.lock_before["commit"], self.lock_before["sha256"]), (run["lock"]["commit"], run["lock"]["sha256"]))
        self.assertEqual(self.lock_before["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertFalse(run.get("unlocks"))
        self.assertEqual([], json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))["unlocks"])
        self.assertEqual(self.locked_blob(R1_FILE), git(self.root, "hash-object", "--", R1_FILE))
        self.assertEqual(4, json.loads((self.root / R1_FILE).read_text(encoding="utf-8"))["cases"][1]["expect"])
        self.assertIsNone(locks.copy_problem(self.root, run))
        (left,) = run["leftovers"]
        self.assertEqual(
            ("unlock_failed", ["OB-1"], "FIX-1", ["R1-2"], [R1_FILE]),
            (left["kind"], left["items"], left["fix_group"], left["cases"], left["restored"]),
        )
        (event,) = [r for r in records(self.root) if r.get("stage") == "unlock"]
        self.assertEqual(("fail", [acceptance.UNLOCK_RULE]), (event["result"], event["rules"]))
        report = (self.root / "docs" / "pbi" / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("鍵を外す修正を確かめられなかった: 鍵を外す: 要件 R1 の R1-2", report)
        return left


class UnlockTicketAndGuardTests(UnlockTestBase):
    def test_the_unlock_is_fixed_first_as_an_acceptance_pbi_with_the_cases_and_the_file_in_its_ticket(self) -> None:
        launch = self.to_fix_stage()
        self.assertEqual(("launch", "worker", 0), (launch["action"], launch["role"], launch["fix_group"]))
        ticket = ticket_of(launch)
        self.assertEqual(("FIX-1", "unlock", [R1_FILE]), (ticket["id"], ticket["kind"], ticket["files"]))
        self.assertEqual(
            {
                "cases": ["R1-2"],
                "fixed": ["R1-2"],
                "added": [],
                "fix": "scenario",
                "requirement": "R1",
                "acceptance_files": [R1_FILE],
                "acceptance_command": self.command,
                "reason": REASON,
            },
            {k: v for k, v in ticket["unlock"].items() if k != "scenario"},
        )
        self.assertEqual(("R1", GHERKIN), (ticket["unlock"]["scenario"]["id"], ticket["unlock"]["scenario"]["gherkin"]))
        # The opened file is not among the locked files the worker must leave alone; the other locked files are.
        self.assertEqual([R2_FILE, locks.LIST_PATH], ticket["locked_files"])
        self.assertEqual([{"module": "app", "name": "add"}], ticket["interfaces"])
        self.assertIn("## Unlock fixes", Path(launch["prompt"]).read_text(encoding="utf-8"))
        # The item on the fix list carries the unlock resolved against the lock.
        (item,) = saved(self.root)["fix_list"]
        self.assertEqual(("OB-1", R1_FILE, "R1"), (item["id"], item["unlock"]["file"], item["unlock"]["requirement"]))
        # The objection named the case's requirement and that it is locked.
        (listed,) = retro.load_items(saved(self.root))
        self.assertEqual(("R1-2", "R1", True), (listed["case"], listed["requirement"], listed["locked"]))

    def test_g1_lets_the_unlock_change_its_file_and_stops_a_change_of_another_locked_file(self) -> None:
        launch = self.to_fix_stage()
        self.write_json(R1_FILE, r1(5))
        r2_locked = self.locked_blob(R2_FILE)
        self.write_json(R2_FILE, {"cases": [{"name": "test_R2[R2-1]", "outcome": "passed"}]})
        ask = self.done(launch, {"status": "ok"})
        run = saved(self.root)
        self.assertEqual(("asking_guard", "guard"), (run["state"], ask["kind"]))
        g1 = [v["detail"] for v in run["guard_issue"]["violations"] if v["rule"] == "G1"]
        self.assertEqual([f"{guards.LOCKED_DETAIL}{R2_FILE}"], g1)
        self.assertEqual([R2_FILE], run["guard_issue"]["lock_restored"])
        self.assertEqual(r2_locked, git(self.root, "hash-object", "--", R2_FILE))
        # The opened file keeps the worker's lines.
        self.assertEqual(5, json.loads((self.root / R1_FILE).read_text(encoding="utf-8"))["cases"][1]["expect"])

        # Tried again, the same fix passes G1 and goes on to the unlock's check, which locks the tests again.
        code, again = run_cli("answer", "--root", str(self.root), "--choice", "retry")
        self.assertEqual((0, "launch", 0), (code, again["action"], again["fix_group"]), again)
        check = self.done(again, {"status": "ok"})
        self.assertEqual(("check", "unlock"), (check["action"], check["stage"]))
        self.assertEqual("done", drive(self.root)["action"])
        self.assert_no_worktree(check)
        self.assertEqual(["OB-1"], [r["item"] for r in saved(self.root)["unlocks"]])

    def test_g1_stops_the_unlock_worker_changing_a_source_file(self) -> None:
        launch = self.to_fix_stage()
        self.write_json(R1_FILE, r1(5))
        self.write("app.py", "def add(a, b):\n    return a + b + 1\n")
        ask = self.done(launch, {"status": "ok"})
        run = saved(self.root)
        self.assertEqual("asking_guard", run["state"], ask)
        self.assertIn("G1: changed outside allowed paths: app.py", run["guard_issue"]["detail"])
        self.assertNotIn(R1_FILE, run["guard_issue"]["detail"])

    def test_the_cli_refuses_a_triage_whose_unlock_is_wrong(self) -> None:
        triage = self.to_triage()
        for bad in (unlock(["R1-2"], "rewrite"), unlock(["R1-2", "R2-1"], "scenario"), unlock(["R1-9"], "scenario", requirement="R9")):
            with self.subTest(unlock=bad):
                item = {"id": "OB-1", "decision": "ask", "reason": "r", "unlock": bad}
                out = self.done(triage, {"schema": plan.SCHEMA_TRIAGE, "items": [item]}, code=2)
                self.assertIn("unlock", out["error"])
                self.assertEqual("triaging", saved(self.root)["state"])


class UnlockCheckTests(UnlockTestBase):
    def test_lines_that_fail_at_the_lock_commit_and_pass_now_lock_the_tests_again(self) -> None:
        launch = self.to_fix_stage("scenario")
        check = self.fix_lines(launch, r1(5))
        self.assertEqual(("check", "unlock"), (check["action"], check["stage"]))
        tree = Path(check["unlock"]["worktree"])
        # The lock commit's state is made again outside the work tree, with the fixed file copied in.
        self.assertTrue(tree.is_dir())
        self.assertNotIn(self.root, tree.parents)
        self.assertEqual(self.lock_before["commit"], git(tree, "rev-parse", "HEAD"))
        self.assertEqual(5, json.loads((tree / R1_FILE).read_text(encoding="utf-8"))["cases"][1]["expect"])
        self.assertFalse((tree / "app.json").exists())
        self.assertEqual(2, len(worktrees(self.root)))
        lock_run, now_run = check["unlock"]["runs"]
        self.assertEqual(("lock_commit", "work_tree"), (lock_run["where"], now_run["where"]))
        self.assertEqual([lock_run["command"], now_run["command"]], check["commands"])
        self.assertEqual({lock_run["command"]: str(tree)}, check["cwds"])
        self.assertEqual(self.command.replace("{file}", R1_FILE).replace("{junit}", now_run["junit"]), now_run["command"])

        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        run = saved(self.root)
        self.assert_no_worktree(check)
        self.assertEqual([], run["leftovers"])

        # The lock moved on: the fixed tests, then the list with the unlock record on top.
        kept = run["lock"]
        before = self.lock_before["commit"]
        (record,) = run["unlocks"]
        self.assertNotEqual(before, kept["commit"])
        self.assertEqual(kept["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(record["commit"], git(self.root, "rev-parse", f"{kept['commit']}^"))
        self.assertEqual(before, git(self.root, "rev-parse", f"{record['commit']}^"))
        self.assertEqual([R1_FILE], git(self.root, "show", "--name-only", "--format=", record["commit"]).splitlines())
        self.assertEqual([locks.LIST_PATH], git(self.root, "show", "--name-only", "--format=", kept["commit"]).splitlines())
        self.assertEqual(5, json.loads(git(self.root, "show", f"{kept['commit']}:{R1_FILE}"))["cases"][1]["expect"])
        self.assertEqual(self.locked_blob(R2_FILE), git(self.root, "rev-parse", f"{before}:{R2_FILE}"))
        self.assertEqual(
            {
                "item": "OB-1",
                "kind": "objection",
                "requirement": "R1",
                "file": R1_FILE,
                "cases": ["R1-2"],
                "fixed": ["R1-2"],
                "added": [],
                "fix": "scenario",
                "reason": REASON,
                "check": check["launch_id"],
                "verified": {"lock_commit": {"R1-2": "not_implemented"}, "work_tree": {"R1-2": "passed"}},
                "commit_before": before,
                "commit": record["commit"],
            },
            record,
        )
        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual([record], listed["unlocks"])
        self.assertEqual(listed, json.loads(git(self.root, "show", f"{kept['commit']}:{locks.LIST_PATH}")))
        self.assertEqual((["R1-1", "R1-2", "R2-1"], [R1_FILE, R2_FILE]), (listed["cases"], listed["files"]))

        # The copy outside the work tree, the commit id and the hash are the new ones.
        outside = Path(kept["outside"])
        copy = outside / locks.COPY_NAME
        self.assertEqual((self.root / locks.LIST_PATH).read_bytes(), copy.read_bytes())
        self.assertEqual(kept["commit"] + "\n", (outside / locks.COMMIT_NAME).read_text(encoding="ascii"))
        hashed = json.loads((self.locks_home / f"{run['run_id']}.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        self.assertEqual((digest, kept["commit"]), (hashed["sha256"], hashed["commit"]))
        self.assertEqual(digest, kept["sha256"])
        self.assertNotEqual(self.lock_before["sha256"], kept["sha256"])
        self.assertIsNone(locks.copy_problem(self.root, run))
        self.assertEqual(listed, locks.locked_list(self.root, run))

        # The unlock's check is recorded, and the final check ran once after it.
        (event,) = [r for r in records(self.root) if r.get("stage") == "unlock"]
        self.assertEqual(("pass", [], [launch["launch_id"]]), (event["result"], event["rules"], event["read_launch_ids"]))
        self.assertEqual("passed", run["final_check"]["result"])

    def test_lines_that_already_pass_at_the_lock_commit_are_put_back_and_left_over(self) -> None:
        check = self.fix_lines(self.to_fix_stage("scenario"), r1(5, outcome="passed"))
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        left = self.assert_not_locked_again(saved(self.root), check)
        self.assertIn("R1-2: 鍵のコミットの状態で通った", left["what"])

    def test_lines_that_do_not_end_now_as_decided_are_put_back_and_left_over(self) -> None:
        for label, fix, expect, reason in (
            ("scenario, but red now", "scenario", 6, "R1-2: シナリオを直すと決めたが、今の実装で通らない（assertion）"),
            ("implementation, but green now", "implementation", 5, "R1-2: 実装を直すと決めたが、今の実装で通った"),
        ):
            with self.subTest(label):
                self.setUp()
                check = self.fix_lines(self.to_fix_stage(fix), r1(expect))
                out = drive(self.root)
                self.assertEqual("done", out["action"], out)
                left = self.assert_not_locked_again(saved(self.root), check)
                self.assertIn(reason, left["what"])
                self.assertEqual(fix, left["fix"])

    def test_a_changed_line_of_another_case_of_the_file_fails_the_check(self) -> None:
        launch = self.to_fix_stage("scenario")
        # R1-1 passed in the wave check; the worker also changes its line to expect 4, so it fails now.
        changed = {"cases": [{"name": "test_R1[R1-1]", "expect": 4}, {"name": "test_R1[R1-2]", "expect": 5}]}
        check = self.fix_lines(launch, changed)
        drive(self.root)
        left = self.assert_not_locked_again(saved(self.root), check)
        self.assertIn("R1-1: 対象の外のケースが、前の wave の検査では通ったのに今の実装で通らない", left["what"])

    def test_fixing_the_implementation_locks_the_new_lines_and_then_fixes_the_implementation(self) -> None:
        launch = self.to_fix_stage("implementation")
        # The user reads R1-2 as 6: the line changes, and the implementation (5) must follow it.
        check = self.fix_lines(launch, r1(6))
        follow = drive(self.root)
        run = saved(self.root)
        self.assert_no_worktree(check)
        (record,) = run["unlocks"]
        self.assertEqual(
            ("implementation", {"lock_commit": {"R1-2": "not_implemented"}, "work_tree": {"R1-2": "assertion"}}),
            (record["fix"], record["verified"]),
        )
        self.assertEqual(6, json.loads(git(self.root, "show", f"{run['lock']['commit']}:{R1_FILE}"))["cases"][1]["expect"])
        # Then a fix group makes the implementation pass the new line, with the test file locked again.
        self.assertEqual(("launch", 1), (follow["action"], follow["fix_group"]), follow)
        ticket = ticket_of(follow)
        self.assertNotIn("kind", ticket)
        self.assertEqual(["app.py", "app.json"], ticket["files"])
        self.assertIn(R1_FILE, ticket["locked_files"])
        (item,) = ticket["items"]
        self.assertEqual((["R1-2"], [R1_FILE]), (item["after_unlock"]["cases"], item["after_unlock"]["acceptance_files"]))
        self.assertEqual(record["commit"], item["after_unlock"]["commit"])
        self.write_json("app.json", {**IMPL, "test_R1[R1-2]": 6})
        final = self.done(follow, {"status": "ok"})
        self.assertEqual("check", final["action"])
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        self.assertEqual([], saved(self.root)["leftovers"])

    def test_an_added_line_is_checked_and_joins_the_lock_list(self) -> None:
        # The unlock fixes R1-2 and adds a line for R1-3, a new case of R1.
        launch = self.to_fix_stage("scenario", decision="fix", cases=("R1-2", "R1-3"))
        self.assertEqual((["R1-2"], ["R1-3"]), (ticket_of(launch)["unlock"]["fixed"], ticket_of(launch)["unlock"]["added"]))
        check = self.fix_lines(launch, {"cases": [*r1(5)["cases"], {"name": "test_R1[R1-3]", "expect": 7}]})
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        run = saved(self.root)
        self.assert_no_worktree(check)
        (record,) = run["unlocks"]
        self.assertEqual((["R1-2"], ["R1-3"]), (record["fixed"], record["added"]))
        self.assertEqual({"R1-2": "not_implemented", "R1-3": "not_implemented"}, record["verified"]["lock_commit"])
        self.assertEqual({"R1-2": "passed", "R1-3": "passed"}, record["verified"]["work_tree"])
        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual(["R1-1", "R1-2", "R2-1", "R1-3"], listed["cases"])
        self.assertEqual("PBI-3", listed["assignments"]["R1-3"])
        r1_cases = [c["id"] for c in next(sc for sc in run["scenarios"] if sc["id"] == "R1")["cases"]]
        self.assertEqual(["R1-1", "R1-2", "R1-3"], r1_cases)

    def test_an_unlock_whose_worker_changed_nothing_is_left_over_without_a_check(self) -> None:
        launch = self.to_fix_stage("implementation")
        # The locked line fails now as an implementation fix would want, but no line was fixed: nothing to lock again.
        out = self.done(launch, {"status": "ok"})
        run = saved(self.root)
        self.assertEqual(("check", "final_checking"), (out["action"], run["state"]))
        self.assertNotIn("stage", out)
        (left,) = run["leftovers"]
        self.assertEqual("unlock_failed", left["kind"])
        self.assertIn(f"{R1_FILE} が鍵のコミットの中身から変わっていない", left["what"])
        self.assertEqual((self.lock_before["commit"], []), (run["lock"]["commit"], run.get("unlocks") or []))
        self.assertEqual(1, len(worktrees(self.root)))
        self.assertEqual(1, len([g for g in run["fix_groups"]]))

    def test_a_file_changed_after_its_check_was_made_is_not_locked_again(self) -> None:
        check = self.fix_lines(self.to_fix_stage("scenario"), r1(5))
        # The same lines, written again in another layout while the check waits: not the content that was checked.
        self.write(R1_FILE, json.dumps(r1(5), indent=2) + "\n")
        drive(self.root)
        left = self.assert_not_locked_again(saved(self.root), check)
        self.assertIn(f"{R1_FILE} が確かめた後に変わった", left["what"])

    def test_a_line_of_no_case_fails_the_check(self) -> None:
        launch = self.to_fix_stage("scenario")
        check = self.fix_lines(launch, {"cases": [*r1(5)["cases"], {"name": "test_R1[extra]", "expect": 1}]})
        drive(self.root)
        left = self.assert_not_locked_again(saved(self.root), check)
        self.assertIn("鍵を外すケースにもファイルの鍵のケースにも無い testcase がある: test_R1[extra]", left["what"])


class UnlockBatchTests(UnlockTestBase):
    def test_two_unlocks_are_fixed_together_each_with_its_own_file_and_checked_one_after_the_other(self) -> None:
        green = {
            "case": "R2-1",
            "what": "the scenario wants 5 + 5 to be 10",
            "why": "the requirement text adds one for a pair",
            "state": "green",
            "interpretation": "built 10 as written; a pair would make it 11",
        }
        triage = self.to_triage([OBJECTION, green])
        items = [
            {"id": "OB-1", "decision": "fix", "reason": REASON, "unlock": unlock(["R1-2"], "scenario")},
            {"id": "OB-2", "decision": "fix", "reason": "R2 changes too", "unlock": unlock(["R2-1"], "implementation")},
        ]
        ask = self.done(triage, {"schema": plan.SCHEMA_TRIAGE, "items": items})
        self.assertEqual("fix_approval", ask["kind"])
        draft = Path(ask["draft"]).read_text(encoding="utf-8")
        self.assertIn("鍵を外す: 要件 R1 の R1-2（シナリオを直す）", draft)
        self.assertIn("鍵を外す: 要件 R2 の R2-1（実装を直す）", draft)
        code, batch = run_cli("answer", "--root", str(self.root), "--choice", "approve")
        self.assertEqual(0, code, batch)
        self.assertEqual(("launches", [0, 1]), (batch["action"], [m["fix_group"] for m in batch["launches"]]))
        first, second = (ticket_of(m) for m in batch["launches"])
        self.assertEqual(([R1_FILE], [R2_FILE]), (first["unlock"]["acceptance_files"], second["unlock"]["acceptance_files"]))
        self.assertNotIn(R1_FILE, first["locked_files"])
        self.assertIn(R1_FILE, second["locked_files"])

        lock_before = saved(self.root)["lock"]["commit"]
        self.write_json(R1_FILE, r1(5))
        self.write_json(R2_FILE, {"cases": [{"name": "test_R2[R2-1]", "expect": 11}]})
        check = self.done(batch, {"status": "ok"})
        # Each launch may change the file its unlock opened; the two checks wait in line.
        self.assertEqual(("check", "unlock", 0), (check["action"], check["stage"], check["unlock"]["group"]))
        self.assertEqual([0, 1], [e["group"] for e in saved(self.root)["unlock_checks"]])
        follow = drive(self.root)
        run = saved(self.root)
        self.assertEqual(1, len(worktrees(self.root)))
        one, two = run["unlocks"]
        self.assertEqual((["OB-1", "OB-2"], "scenario", "implementation"), ([one["item"], two["item"]], one["fix"], two["fix"]))
        # The second check ran at the lock commit the first one made.
        self.assertEqual(lock_before, one["commit_before"])
        self.assertEqual(one["commit"], git(self.root, "rev-parse", f"{two['commit_before']}^"))
        self.assertEqual(run["lock"]["commit"], git(self.root, "rev-parse", "HEAD"))
        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual([one, two], listed["unlocks"])
        self.assertIsNone(locks.copy_problem(self.root, run))
        # The implementation follows R2-1's new line in a group after both.
        self.assertEqual(("launch", 2), (follow["action"], follow["fix_group"]), follow)
        self.assertEqual(["R2-1"], ticket_of(follow)["items"][0]["after_unlock"]["cases"])


class WorktreeCleanupTests(UnlockTestBase):
    def test_the_worktree_is_gone_when_the_runner_writes_no_junit(self) -> None:
        launch = self.to_fix_stage("scenario")
        # A file the runner cannot read: neither run writes JUnit XML.
        self.write(R1_FILE, "{ not json\n")
        check = self.done(launch, {"status": "ok"})
        self.assertTrue(Path(check["unlock"]["worktree"]).is_dir())
        drive(self.root)
        left = self.assert_not_locked_again(saved(self.root), check)
        self.assertIn("鍵のコミットの状態で流せなかった: JUnit XML が書き出されていない", left["what"])

    def test_the_worktree_is_gone_when_locking_again_fails(self) -> None:
        check = self.fix_lines(self.to_fix_stage("scenario"), r1(5))

        def broken(*args: object, **kwargs: object) -> dict:
            raise state.StateError("lock: git commit-tree failed: disk full")

        with patch.object(locks, "relock", broken):
            code, out = run_cli("drive", "--root", str(self.root))
        self.assertEqual(2, code, out)
        self.assertIn("disk full", out["error"])
        self.assert_no_worktree(check)
        # The run still waits for the check. Run again, it reads the JUnit XML it kept and locks the tests again.
        self.assertEqual("unlock", saved(self.root)["pending_action"]["stage"])
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        run = saved(self.root)
        self.assertEqual(1, len(run["unlocks"]))
        self.assertEqual([], run["leftovers"])

    def test_the_worktree_is_gone_when_it_cannot_be_made(self) -> None:
        launch = self.to_fix_stage("scenario")
        made: list[Path] = []
        real = locks._git

        def fail_worktree_add(root, *args, **kwargs):
            # git worktree add makes the folder, then fails.
            if "worktree" in args and "add" in args:
                made.append(Path(args[-2]))
                made[-1].mkdir(parents=True)
                raise state.StateError("lock: git worktree add failed: simulated")
            return real(root, *args, **kwargs)

        with patch.object(locks, "_git", side_effect=fail_worktree_add):
            out = self.fix_lines(launch, r1(5))
        run = saved(self.root)
        # The check could not be made: the unlock is left over and the run goes on to the final check.
        self.assertEqual(("check", "final_checking"), (out["action"], run["state"]))
        (left,) = run["leftovers"]
        self.assertIn("一時的な worktree を作れなかった", left["what"])
        self.assertEqual(4, json.loads((self.root / R1_FILE).read_text(encoding="utf-8"))["cases"][1]["expect"])
        (tree,) = made
        self.assertFalse(tree.exists())
        self.assertFalse(tree.parent.exists())
        self.assertEqual(1, len(worktrees(self.root)))


class TriageShapeTests(unittest.TestCase):
    LOCKED = {"R1-1": "R1", "R1-2": "R1", "R2-1": "R2"}
    PLAN = ["R1-1", "R1-2", "R2-1", "R3-1"]

    def errors(self, *items: dict, locked: dict | None = LOCKED) -> list[str]:
        obj = {"schema": plan.SCHEMA_TRIAGE, "items": list(items)}
        return plan.validate_triage(obj, locked_cases=locked, plan_cases=self.PLAN)

    def item(self, unlock_value: object, decision: str = "ask", iid: str = "OB-1") -> dict:
        return {"id": iid, "decision": decision, "reason": "r", "unlock": unlock_value}

    def test_an_unlock_holds_the_cases_and_the_decision(self) -> None:
        for good in (
            unlock(["R1-2"], "scenario"),
            unlock(["R1-2"], "implementation"),
            unlock(["R1-2", "R1-9"], "scenario"),
            unlock(["R1-9"], "implementation", requirement="R1"),
            unlock(["R1-1", "R1-2"], "scenario", requirement="R1"),
        ):
            with self.subTest(unlock=good):
                self.assertEqual([], self.errors(self.item(good)))
                self.assertEqual([], self.errors(self.item(good, "fix")))
        # Without the lock's cases only the shape is checked.
        self.assertEqual([], plan.validate_triage({"schema": plan.SCHEMA_TRIAGE, "items": [self.item(unlock(["X-1"], "scenario"))]}))

    def test_an_unlock_of_another_shape_is_refused(self) -> None:
        for label, bad, needle in (
            ("not an object", ["R1-2"], "cases と fix を持つオブジェクト"),
            ("no cases", {"fix": "scenario"}, "unlock.cases は配列"),
            ("empty cases", unlock([], "scenario"), "unlock.cases は 1 件以上"),
            ("a case that is not a string", {"cases": [3], "fix": "scenario"}, "unlock.cases[0] は空でない文字列"),
            ("the same case twice", unlock(["R1-2", "R1-2"], "scenario"), "重複"),
            ("no decision", {"cases": ["R1-2"]}, "unlock.fix は scenario"),
            ("another decision", unlock(["R1-2"], "both"), "unlock.fix は scenario"),
            ("an unknown field", {**unlock(["R1-2"], "scenario"), "files": ["a"]}, "使えない項目"),
            ("an empty requirement", unlock(["R1-2"], "scenario", requirement=""), "requirement は空でない"),
            ("two requirements", unlock(["R1-2", "R2-1"], "scenario"), "1 つの要件のケース"),
            ("a requirement that is not locked", unlock(["R3-9"], "scenario", requirement="R3"), "鍵をかけた要件ではありません"),
            ("another requirement", unlock(["R1-2"], "scenario", requirement="R2"), "requirement の R2 と違います"),
            ("a case that is not locked", unlock(["R3-1"], "scenario", requirement="R1"), "鍵をかけていない要件のケース"),
            ("a new line of no requirement", unlock(["R9-1"], "scenario"), "足す要件を requirement に"),
        ):
            with self.subTest(label):
                errors = self.errors(self.item(bad))
                self.assertTrue(any(needle in e for e in errors), errors)

    def test_an_unlock_is_never_skipped_needs_a_lock_and_unlocks_a_case_once(self) -> None:
        errors = self.errors(self.item(unlock(["R1-2"], "scenario"), "skip"))
        self.assertTrue(any("decision を fix か ask" in e for e in errors), errors)
        errors = self.errors(self.item(unlock(["R1-2"], "scenario")), locked={})
        self.assertTrue(any("鍵をかけたケースが無い" in e for e in errors), errors)
        errors = self.errors(self.item(unlock(["R1-2"], "scenario")), self.item(unlock(["R1-2"], "implementation"), iid="OB-2"))
        self.assertTrue(any("ケース R1-2 の鍵を OB-1 と OB-2 の 2 件で外しています" in e for e in errors), errors)

    def test_a_run_without_a_lock_cannot_unlock(self) -> None:
        run = state.new_run_state(Path("."), Path("goal.md"), None)
        run.update(acceptance_first=False, lock=None)
        self.assertEqual({}, locks.locked_cases(run))
        item = {"id": "F-1", "decision": "ask", "reason": "r", "unlock": unlock(["R1-1"], "scenario")}
        errors = plan.validate_triage({"schema": plan.SCHEMA_TRIAGE, "items": [item]}, locked_cases=locks.locked_cases(run), plan_cases=[])
        self.assertTrue(any("鍵をかけたケースが無い" in e for e in errors), errors)

    def test_an_unlock_request_carries_the_unlock_it_asks_for(self) -> None:
        item = retro.make_item("unlock_request", "UL-1", what="w", why="y", unlock=unlock(["R1-2"], "scenario"))
        self.assertEqual(unlock(["R1-2"], "scenario"), retro.requested_unlock(item))
        self.assertIsNone(retro.requested_unlock(retro.make_item("objection", "OB-1", what="w", why="y")))

    def test_an_unlock_item_is_a_fix_group_of_its_own_before_the_others(self) -> None:
        shared = {"id": "F-1", "file": "app.py"}
        opened = {"id": "OB-1", "affects": {"pbis": [], "files": ["app.py"]}, "unlock": {"file": R1_FILE, "cases": ["R1-2"]}}
        other = {"id": "F-2", "file": "app.py"}
        groups = plan.fix_groups([shared, opened, other], max_workers=1)
        self.assertEqual([["OB-1"], ["F-1", "F-2"]], [[i["id"] for i in g] for g in groups])
        self.assertEqual([R1_FILE], plan._item_files(opened))


class PromptTests(unittest.TestCase):
    def test_the_worker_prompt_says_how_to_read_an_unlock_ticket(self) -> None:
        text = (SKILLS_DIR / "prompts" / "worker.md").read_text(encoding="utf-8")
        section = text.split("## Unlock fixes", 1)[1].split("\n## ", 1)[0]
        for phrase in (
            "`kind` is `unlock`", "`unlock.acceptance_files`", "`unlock.fixed`", "`unlock.added`", "`scenario`",
            "`implementation`", "temporary worktree", "lock commit", "`after_unlock`",
        ):
            self.assertIn(phrase, section)

    def test_the_planner_prompt_says_how_the_triage_takes_the_lock_off(self) -> None:
        text = (SKILLS_DIR / "prompts" / "planner.md").read_text(encoding="utf-8")
        section = text.split("## Mode: `triage`", 1)[1]
        for phrase in (
            "`unlock`", '"cases"', '"fix": "scenario" | "implementation"', '"requirement"', "never `skip`",
            "temporary worktree", "lock commit", "docs/pbi/locks.json",
        ):
            self.assertIn(phrase, section)


if __name__ == "__main__":
    unittest.main()
