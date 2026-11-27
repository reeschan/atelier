"""E2E PBIs, driven through the CLI in temporary git repositories (Seam 1).

In a locked run, an e2e PBI that passes its wave check adds the E2E files it wrote (the files in its targets that are
there) to the lock: the lock list is written again, the lock commit moves on, and the copy outside the work tree, the
commit id and the hash under ATELIER_LOCKS_HOME are written again. From then on G1 stops a worker that changes those
files and puts them back, the wave check finds a change that got past G1, and the copy's check goes by the new lock.
The E2E stage runs the plan's e2e commands, which take the specs the e2e PBIs added. A run without acceptance_first
has no lock, and an e2e PBI that passes makes none.
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

import conductor  # noqa: E402
from atelier import acceptance, guards, locks, state  # noqa: E402
from tests.test_acceptance_red import GHERKIN, approve_plan, drive, finish_stage, pass_review, pbis_of, saved  # noqa: E402
from tests.test_acceptance_toggle import UNIT  # noqa: E402
from tests.test_conductor import run_cli, write_result_for  # noqa: E402
from tests.test_integration import make_pbi, make_plan  # noqa: E402
from tests.test_wave_locks import ACCEPTANCE, PBI_CHECK, PLAN_CHECK, PLAN_CHECK_SCRIPT, WAVE_RUNNER, name  # noqa: E402
from tests.test_worker_checks import ticket_of  # noqa: E402

TEST_FILE = "acceptance/R1.json"
STUB = "def add(a, b):\n    raise NotImplementedError\n"
BUILT = "def add(a, b):\n    return a + b\n"
ADD_SPEC = "e2e/add.spec.json"
MORE_SPEC = "e2e/more.spec.json"
CASES = ("R1-1", "R1-2")
RED = {"cases": [{"name": name(cid), "message": "assert 0 == 3"} for cid in CASES]}
PASSED = {name(cid): {"outcome": "passed"} for cid in CASES}
# The fake E2E runner runs every spec under e2e/ and writes down which ones it ran.
E2E_RUNNER = """import json
from pathlib import Path

ran = sorted(p.as_posix() for p in Path("e2e").glob("*.spec.json"))
Path("e2e_ran.json").write_text(json.dumps(ran), encoding="utf-8")
"""
E2E_COMMAND = f'"{sys.executable}" fake_e2e.py'


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8").rstrip()


def e2e_pbi(pid: str, depends_on: list[str], target: str) -> dict:
    return {**make_pbi(pid, depends_on=depends_on, level="L3", targets=[target]), "kind": "e2e", "checks": [PBI_CHECK]}


def e2e_plan(acceptance_first: bool = True) -> dict:
    """Build PBI-3 makes app.add; e2e PBI-4 writes the add spec after it, and e2e PBI-5 writes another spec after
    PBI-4. With acceptance_first, acceptance PBI-1 writes R1's tests (both cases are PBI-3's) and stub PBI-2 places
    app.add first."""
    build = {**make_pbi("PBI-3", targets=["app.py"]), "checks": [PBI_CHECK]}
    pbis = [build, e2e_pbi("PBI-4", ["PBI-3"], ADD_SPEC), e2e_pbi("PBI-5", ["PBI-4"], MORE_SPEC)]
    if acceptance_first:
        acc = {**make_pbi("PBI-1", targets=[TEST_FILE]), "kind": "acceptance"}
        stub = {**make_pbi("PBI-2", targets=["app.py"]), "kind": "stub"}
        build["depends_on"] = ["PBI-1", "PBI-2"]
        pbis = [acc, stub, *pbis]
    obj = make_plan(pbis)
    obj["checks"] = [PLAN_CHECK]
    obj["e2e"] = [E2E_COMMAND]
    if acceptance_first:
        obj["interfaces"] = [{"module": "app", "name": "add"}]
        obj["scenarios"] = [
            {
                "id": "R1",
                "text": "要件 R1",
                "mode": "normal",
                "gherkin": GHERKIN,
                "cases": [
                    {"id": cid, "values": {"a": n, "b": 2, "sum": n + 2}, "pbi": "PBI-3", "needs": []}
                    for n, cid in enumerate(CASES, start=1)
                ],
                "spare_count": 0,
            },
        ]
    return obj


class E2ePbiTestBase(unittest.TestCase):
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
        # The runners and the check scripts are there before the run starts, so no launch writes them.
        (self.root / "fake_wave.py").write_text(WAVE_RUNNER, encoding="utf-8")
        (self.root / "fake_e2e.py").write_text(E2E_RUNNER, encoding="utf-8")
        (self.root / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (self.root / "plan_check.py").write_text(PLAN_CHECK_SCRIPT, encoding="utf-8")

    def done(self, launch: dict, result: dict | None = None) -> dict:
        write_result_for(launch, result or {"status": "ok"})
        code, nxt = run_cli("done", "--root", str(self.root), "--launch-id", launch["launch_id"])
        self.assertEqual(0, code, nxt)
        return nxt

    def write(self, rel: str, text: str) -> None:
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_bytes(text.encode("utf-8"))


class LockedE2ePbiTests(E2ePbiTestBase):
    def to_e2e_pbi(self) -> dict:
        """Lock the acceptance tests, build PBI-3 and pass its wave; return PBI-4's launch."""
        act = approve_plan(self.root, e2e_plan(), [UNIT, ACCEPTANCE])
        (self.root / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.root / ".git" / "info" / "attributes").write_bytes(b"*.json text eol=lf\n")
        self.write("app.py", STUB)
        check = finish_stage(self.root, act, {"R1": RED})
        self.assertEqual("check", check["action"])
        launch = pass_review(self.root, drive(self.root))
        self.assertEqual(["PBI-3"], pbis_of(launch))
        self.assertEqual([TEST_FILE, locks.LIST_PATH], saved(self.root)["lock"]["files"])
        self.write("app.py", BUILT)
        check = self.done(launch)
        self.assertEqual("check", check["action"])
        (self.root / "outcomes.json").write_text(json.dumps(PASSED), encoding="utf-8")
        launch = drive(self.root)
        self.assertEqual(["PBI-4"], pbis_of(launch))
        self.assertEqual("passed", state.get_pbi(saved(self.root), "PBI-3")["status"])
        return launch

    def pass_e2e_pbi(self, *, past_g1: bool = False) -> tuple[dict, dict]:
        """PBI-4 writes the add spec and passes its wave check. Returns the lock before and PBI-5's launch.

        With past_g1, PBI-5's snapshot carries no lock, so a change of the lock during it gets past G1."""
        launch = self.to_e2e_pbi()
        before = dict(saved(self.root)["lock"])
        self.write(ADD_SPEC, '{"spec": "add", "expect": 3}\n')
        check = self.done(launch)
        self.assertEqual(("check", None), (check["action"], check.get("pbi")))
        if past_g1:
            with patch.object(conductor, "_lock_guard", lambda root, run: None):
                nxt = drive(self.root)
        else:
            nxt = drive(self.root)
        self.assertEqual(["PBI-5"], pbis_of(nxt))
        self.assertEqual("passed", state.get_pbi(saved(self.root), "PBI-4")["status"])
        return before, nxt

    def test_an_e2e_pbi_that_passes_its_wave_check_adds_its_files_to_the_lock_and_moves_the_lock_commit(self) -> None:
        before, launch = self.pass_e2e_pbi()
        run = saved(self.root)
        kept = run["lock"]
        self.assertEqual([TEST_FILE, ADD_SPEC, locks.LIST_PATH], kept["files"])
        self.assertEqual(["PBI-4"], kept["e2e_pbis"])
        self.assertNotEqual(before["commit"], kept["commit"])
        self.assertEqual(kept["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(before["commit"], git(self.root, "rev-parse", f"{kept['commit']}^"))
        self.assertEqual(
            sorted([ADD_SPEC, locks.LIST_PATH]),
            sorted(git(self.root, "show", "--name-only", "--format=", kept["commit"]).splitlines()),
        )
        self.assertEqual(
            [{"pbis": ["PBI-4"], "files": [ADD_SPEC], "commit_before": before["commit"], "commit": kept["commit"]}],
            run["lock_extensions"],
        )
        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual([TEST_FILE, ADD_SPEC], listed["files"])
        self.assertEqual(listed, json.loads(git(self.root, "show", f"{kept['commit']}:{locks.LIST_PATH}")))
        self.assertEqual(list(CASES), listed["cases"])

        # The copy outside the work tree, the commit id and the hash are the new ones.
        outside = Path(kept["outside"])
        copy = outside / locks.COPY_NAME
        self.assertEqual((self.root / locks.LIST_PATH).read_bytes(), copy.read_bytes())
        self.assertEqual(kept["commit"] + "\n", (outside / locks.COMMIT_NAME).read_text(encoding="ascii"))
        record = json.loads((self.locks_home / f"{run['run_id']}.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        self.assertEqual(
            {"schema": locks.HASH_SCHEMA, "run": run["run_id"], "sha256": digest, "copy": str(copy), "commit": kept["commit"]},
            record,
        )
        self.assertEqual(digest, kept["sha256"])
        self.assertNotEqual(before["sha256"], kept["sha256"])
        self.assertIsNone(locks.copy_problem(self.root, run))
        # PBI-5's ticket names the E2E file among the locked files.
        self.assertIn(ADD_SPEC, ticket_of(launch)["locked_files"])

    def test_a_later_worker_that_changes_the_e2e_file_is_stopped_by_g1_and_the_file_is_put_back(self) -> None:
        _, launch = self.pass_e2e_pbi()
        locked = git(self.root, "rev-parse", f"{saved(self.root)['lock']['commit']}:{ADD_SPEC}")
        # PBI-5 makes the add spec expect something else, besides writing its own spec.
        self.write(ADD_SPEC, '{"spec": "add", "expect": 4}\n')
        self.write(MORE_SPEC, '{"spec": "more"}\n')
        ask = self.done(launch)
        run = saved(self.root)
        self.assertEqual(("asking_guard", "guard"), (run["state"], ask["kind"]))
        g1 = [v["detail"] for v in run["guard_issue"]["violations"] if v["rule"] == "G1"]
        self.assertEqual([f"{guards.LOCKED_DETAIL}{ADD_SPEC}"], g1)
        self.assertEqual([ADD_SPEC], run["guard_issue"]["lock_restored"])
        self.assertEqual(locked, git(self.root, "hash-object", "--", ADD_SPEC))
        self.assertIn(b'"expect": 3', (self.root / ADD_SPEC).read_bytes())
        # The worker's own spec stays.
        self.assertTrue((self.root / MORE_SPEC).is_file())

    def test_a_change_of_the_e2e_file_that_gets_past_g1_is_put_back_by_the_wave_check(self) -> None:
        _, launch = self.pass_e2e_pbi(past_g1=True)
        locked = git(self.root, "rev-parse", f"{saved(self.root)['lock']['commit']}:{ADD_SPEC}")
        self.write(ADD_SPEC, '{"spec": "add", "expect": 4}\n')
        self.write(MORE_SPEC, '{"spec": "more"}\n')
        check = self.done(launch)
        # The copy's check passes on the moved lock, and the change is found and put back; PBI-5 does not own it.
        self.assertEqual("check", check["action"])
        run = saved(self.root)
        self.assertEqual(locked, git(self.root, "hash-object", "--", ADD_SPEC))
        self.assertEqual([{"check": check["launch_id"], "files": [ADD_SPEC]}], run["lock_restored"])
        (item,) = run["unresolved"]
        self.assertEqual(acceptance.WAVE_LOCK_UNRESOLVED, item["kind"])
        self.assertEqual([f"{guards.LOCKED_DETAIL}{ADD_SPEC}"], item["changes"])

    def test_the_e2e_stage_runs_the_plans_e2e_commands_with_the_specs_the_e2e_pbis_added(self) -> None:
        _, launch = self.pass_e2e_pbi()
        self.write(MORE_SPEC, '{"spec": "more"}\n')
        check = self.done(launch)
        self.assertEqual("check", check["action"])
        review = drive(self.root)
        run = saved(self.root)
        # PBI-5 passed too: both specs are locked, each with its own lock commit.
        self.assertEqual([TEST_FILE, ADD_SPEC, MORE_SPEC, locks.LIST_PATH], run["lock"]["files"])
        self.assertEqual(["PBI-4", "PBI-5"], run["lock"]["e2e_pbis"])
        self.assertEqual([["PBI-4"], ["PBI-5"]], [e["pbis"] for e in run["lock_extensions"]])
        self.assertEqual(run["lock_extensions"][0]["commit"], run["lock_extensions"][1]["commit_before"])
        self.assertIsNone(locks.copy_problem(self.root, run))

        self.assertEqual(("launch", "reviewer"), (review["action"], review["role"]))
        e2e = self.done(review, {"schema": "atelier.findings/1", "findings": []})
        self.assertEqual(("check", [E2E_COMMAND]), (e2e["action"], e2e["commands"]))
        drive(self.root)
        self.assertEqual([ADD_SPEC, MORE_SPEC], json.loads((self.root / "e2e_ran.json").read_text(encoding="utf-8")))
        self.assertEqual("passed", saved(self.root)["e2e"]["result"])


class UnlockedE2ePbiTests(E2ePbiTestBase):
    def test_an_e2e_pbi_that_passes_in_a_run_without_acceptance_first_makes_no_lock(self) -> None:
        act = approve_plan(self.root, e2e_plan(acceptance_first=False), [UNIT])
        run = saved(self.root)
        self.assertFalse(run["acceptance_first"])
        self.assertEqual(["PBI-3"], pbis_of(act))
        head = git(self.root, "rev-parse", "HEAD")
        self.write("app.py", BUILT)
        self.assertEqual("check", self.done(act)["action"])
        launch = drive(self.root)
        self.assertEqual(["PBI-4"], pbis_of(launch))
        self.write(ADD_SPEC, '{"spec": "add", "expect": 3}\n')
        self.assertEqual("check", self.done(launch)["action"])
        nxt = drive(self.root)
        self.assertEqual(["PBI-5"], pbis_of(nxt))
        run = saved(self.root)
        self.assertEqual("passed", state.get_pbi(run, "PBI-4")["status"])
        self.assertIsNone(run["lock"])
        self.assertNotIn("lock_extensions", run)
        self.assertEqual(head, git(self.root, "rev-parse", "HEAD"))
        self.assertFalse((self.root / locks.LIST_PATH).exists())
        self.assertFalse((self.locks_home / f"{run['run_id']}.json").exists())
        self.assertFalse((self.root / ".git" / "atelier").exists())


if __name__ == "__main__":
    unittest.main()
