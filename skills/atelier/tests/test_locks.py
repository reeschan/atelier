"""The lock on the acceptance tests, driven through the CLI in temporary git repositories.

Once the planner's acceptance review is ok the conductor writes docs/pbi/locks.json, commits the locked files and the
list and nothing else, keeps a copy of the list and the lock commit's id in <git common dir>/atelier/<run>/, and writes
the copy's sha256 under ATELIER_LOCKS_HOME. A fake worker that then changes a locked file or the list is stopped by G1
and the file is put back to the lock commit's content; a change of line endings only is not a change. No build PBI is
launched before the lock.
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

from atelier import flow, locks, state  # noqa: E402
from tests.test_acceptance_red import (  # noqa: E402
    GHERKIN,
    approve_plan,
    drive,
    finish_stage,
    good,
    pass_review,
    pbis_of,
    red_plan,
    saved,
)
from tests.test_conductor import run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo  # noqa: E402
from tests.test_integration import make_pbi, make_plan  # noqa: E402

SUPPORT = "acceptance/support.json"
LOCKED = ["acceptance/R1.json", SUPPORT, locks.LIST_PATH]
STUB = "def add(a, b):\n    raise NotImplementedError\n"


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8").rstrip()


def lock_plan() -> dict:
    """Acceptance PBI-1 writes R1's tests and a support file; stub PBI-2 places app.add; build PBI-3, which R1's
    cases are assigned to, needs both, so it is the only PBI launched after the lock."""
    acc = {**make_pbi("PBI-1", targets=["acceptance/R1.json", SUPPORT]), "kind": "acceptance"}
    stub = {**make_pbi("PBI-2", targets=["app.py"]), "kind": "stub"}
    build = make_pbi("PBI-3", depends_on=["PBI-1", "PBI-2"], targets=["app.py"])
    obj = make_plan([acc, stub, build])
    obj["interfaces"] = [{"module": "app", "name": "add"}]
    obj["scenarios"] = [
        {
            "id": "R1",
            "text": "要件 R1",
            "mode": "normal",
            "gherkin": GHERKIN,
            "cases": [{"id": f"R1-{n}", "values": {"a": n, "b": 2, "sum": n + 2}, "pbi": "PBI-3", "needs": []} for n in (1, 2)],
            "spare_count": 0,
        },
    ]
    return obj


def blob_now(root: Path, name: str) -> str:
    return git(root, "hash-object", "--", name)


def blob_locked(root: Path, name: str) -> str:
    return git(root, "rev-parse", f"{saved(root)['lock']['commit']}:{name}")


class LockTests(unittest.TestCase):
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

    def stage_to_review(self, obj: dict) -> tuple[dict, str]:
        """Approve the plan, build the acceptance stage (the stub's change to app.py stays uncommitted) and pass
        the Red check. Returns the planner's review launch and the commit before the lock."""
        # A file the user left uncommitted before the run; the lock commit leaves it out.
        (self.root / "notes.txt").write_bytes(b"not part of the lock\n")
        act = approve_plan(self.root, obj)
        # The repository converts the JSON files' line endings to LF, whatever core.autocrlf says.
        (self.root / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.root / ".git" / "info" / "attributes").write_bytes(b"*.json text eol=lf\n")
        run = saved(self.root)
        self.assertEqual("building_acceptance", run["state"])
        self.assertEqual(["PBI-1", "PBI-2"], pbis_of(act))
        self.assertIsNone(run["lock"])
        self.assertFalse((self.root / locks.LIST_PATH).exists())
        before = git(self.root, "rev-parse", "HEAD")

        (self.root / "app.py").write_bytes(STUB.encode("utf-8"))
        (self.root / SUPPORT).parent.mkdir(parents=True, exist_ok=True)
        (self.root / SUPPORT).write_bytes(b'{\n  "fixture": [1, 2]\n}\n')
        check = finish_stage(self.root, act, {"R1": good("R1")})
        self.assertEqual("check", check["action"])
        review = drive(self.root)
        self.assertEqual(("reviewing_acceptance", "planner"), (saved(self.root)["state"], review["role"]))
        self.assertIsNone(saved(self.root)["lock"])
        self.assertEqual(before, git(self.root, "rev-parse", "HEAD"))
        return review, before

    def lock_and_launch(self, obj: dict | None = None) -> dict:
        review, _ = self.stage_to_review(obj or lock_plan())
        return pass_review(self.root, review)

    def test_review_ok_writes_the_list_and_commits_only_the_locked_files(self) -> None:
        review, before = self.stage_to_review(lock_plan())
        nxt = pass_review(self.root, review)
        run = saved(self.root)
        self.assertEqual("building", run["state"])
        self.assertEqual(["PBI-3"], pbis_of(nxt))

        commit = run["lock"]["commit"]
        self.assertEqual(commit, git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(before, git(self.root, "rev-parse", f"{commit}^"))
        committed = git(self.root, "show", "--name-only", "--format=", commit).splitlines()
        self.assertEqual(sorted(LOCKED), sorted(committed))
        self.assertEqual(LOCKED, run["lock"]["files"])
        # The other uncommitted changes stay out of the lock commit, as they were.
        status = git(self.root, "status", "--porcelain", "--untracked-files=all").splitlines()
        self.assertIn(" M app.py", status)
        self.assertIn("?? notes.txt", status)
        self.assertFalse([line for line in status if line[3:] in LOCKED])

        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "schema": locks.SCHEMA,
                "run": run["run_id"],
                "files": LOCKED[:-1],
                "requirements": ["R1"],
                "cases": ["R1-1", "R1-2"],
                "assignments": {"R1-1": "PBI-3", "R1-2": "PBI-3"},
                "unlocks": [],
                "signatures": {
                    "app:add": {"module": "app", "name": "add", "language": "Python", "file": "app.py", "signature": "def add(a, b)"},
                },
                "keys": {},
                "unextractable": [],
            },
            listed,
        )

    def test_copy_and_commit_id_go_outside_the_work_tree_and_the_hash_to_the_locks_home(self) -> None:
        self.lock_and_launch()
        run = saved(self.root)
        outside = self.root / ".git" / "atelier" / run["run_id"]
        self.assertEqual(str(outside.resolve()), run["lock"]["outside"])
        copy = outside / locks.COPY_NAME
        self.assertEqual((self.root / locks.LIST_PATH).read_bytes(), copy.read_bytes())
        self.assertEqual(run["lock"]["commit"] + "\n", (outside / locks.COMMIT_NAME).read_text(encoding="ascii"))

        record_file = self.locks_home / f"{run['run_id']}.json"
        self.assertEqual(str(record_file), run["lock"]["hash_file"])
        record = json.loads(record_file.read_text(encoding="utf-8"))
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        self.assertEqual(
            {"schema": locks.HASH_SCHEMA, "run": run["run_id"], "sha256": digest, "copy": str(copy), "commit": run["lock"]["commit"]},
            record,
        )
        self.assertEqual(digest, run["lock"]["sha256"])
        self.assertFalse((Path.home() / ".config" / "atelier" / "locks" / f"{run['run_id']}.json").exists())

    def test_a_worker_that_changes_a_locked_test_is_stopped_by_g1_and_the_file_is_put_back(self) -> None:
        launch = self.lock_and_launch()
        self.assertEqual(("launch", "PBI-3"), (launch["action"], launch["pbi"]))
        original = (self.root / "acceptance" / "R1.json").read_bytes()
        (self.root / "acceptance" / "R1.json").write_text(json.dumps({"cases": []}), encoding="utf-8")
        (self.root / "app.py").write_bytes(b"def add(a, b):\n    return a + b\n")
        write_result_for(launch, {"status": "ok"})
        code, ask = run_cli("done", "--root", str(self.root), "--launch-id", launch["launch_id"])
        self.assertEqual(0, code, ask)
        run = saved(self.root)
        self.assertEqual(("asking_guard", "guard"), (run["state"], ask["kind"]))
        g1 = [v["detail"] for v in run["guard_issue"]["violations"] if v["rule"] == "G1"]
        self.assertEqual(["locked file changed: acceptance/R1.json"], g1)
        self.assertEqual(["acceptance/R1.json"], run["guard_issue"]["lock_restored"])
        self.assertEqual([{"launch_id": launch["launch_id"], "files": ["acceptance/R1.json"]}], run["lock_restored"])
        self.assertEqual(blob_locked(self.root, "acceptance/R1.json"), blob_now(self.root, "acceptance/R1.json"))
        self.assertEqual(original.replace(b"\r\n", b"\n"), (self.root / "acceptance" / "R1.json").read_bytes().replace(b"\r\n", b"\n"))
        # The worker's own change stays: only the locked file is put back.
        self.assertIn(b"return a + b", (self.root / "app.py").read_bytes())

    def test_a_worker_that_rewrites_the_list_or_deletes_a_locked_file_is_stopped_and_both_come_back(self) -> None:
        launch = self.lock_and_launch()
        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        listed["cases"] = ["R1-1"]
        (self.root / locks.LIST_PATH).write_text(json.dumps(listed), encoding="utf-8")
        (self.root / SUPPORT).unlink()
        write_result_for(launch, {"status": "ok"})
        code, ask = run_cli("done", "--root", str(self.root), "--launch-id", launch["launch_id"])
        self.assertEqual(0, code, ask)
        run = saved(self.root)
        self.assertEqual("asking_guard", run["state"])
        g1 = sorted(v["detail"] for v in run["guard_issue"]["violations"] if v["rule"] == "G1")
        self.assertEqual([f"locked file changed: {SUPPORT}", f"locked file changed: {locks.LIST_PATH}"], g1)
        self.assertIn(f"鍵のファイルを鍵のコミットの中身に戻した: {SUPPORT}, {locks.LIST_PATH}", run["guard_issue"]["detail"])
        for name in (locks.LIST_PATH, SUPPORT):
            with self.subTest(name=name):
                self.assertEqual(blob_locked(self.root, name), blob_now(self.root, name))
        self.assertEqual(["R1-1", "R1-2"], json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))["cases"])

    def test_a_line_ending_change_of_a_locked_file_is_not_a_violation(self) -> None:
        launch = self.lock_and_launch()
        for name in (locks.LIST_PATH, SUPPORT):
            path = self.root / name
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertIn(b"\r\n", path.read_bytes())
            self.assertEqual(blob_locked(self.root, name), blob_now(self.root, name))
        (self.root / "app.py").write_bytes(b"def add(a, b):\n    return a + b\n")
        write_result_for(launch, {"status": "ok"})
        code, nxt = run_cli("done", "--root", str(self.root), "--launch-id", launch["launch_id"])
        self.assertEqual(0, code, nxt)
        run = saved(self.root)
        self.assertEqual(("checking", "check"), (run["state"], nxt["action"]))
        self.assertFalse(run.get("guard_issue"))

    def test_a_launch_batch_member_that_changes_a_locked_test_is_stopped_and_the_file_is_put_back(self) -> None:
        act = approve_plan(self.root, red_plan(["R1"]))
        check = finish_stage(self.root, act, {"R1": good("R1"), "R2": {"cases": []}})
        self.assertEqual("check", check["action"])
        batch = pass_review(self.root, drive(self.root))
        self.assertEqual(("launches", ["PBI-3", "PBI-4"]), (batch["action"], pbis_of(batch)))
        locked = saved(self.root)["lock"]
        self.assertEqual(["acceptance/R1.json", "acceptance/R2.json", locks.LIST_PATH], locked["files"])

        (self.root / "acceptance" / "R1.json").write_text(json.dumps({"cases": []}), encoding="utf-8")
        write_result_for(batch, {"status": "ok"})
        code, ask = run_cli("done", "--root", str(self.root), "--launch-id", batch["launch_id"])
        self.assertEqual(0, code, ask)
        run = saved(self.root)
        self.assertEqual("asking_guard", run["state"])
        self.assertIn("G1: locked file changed: acceptance/R1.json", run["guard_issue"]["detail"])
        self.assertEqual(["acceptance/R1.json"], run["guard_issue"]["lock_restored"])
        self.assertEqual(blob_locked(self.root, "acceptance/R1.json"), blob_now(self.root, "acceptance/R1.json"))

    def test_no_build_or_e2e_pbi_is_launched_before_the_lock(self) -> None:
        review, before = self.stage_to_review(lock_plan())
        # The never rule: a build or e2e PBI being launched before the lock is refused; the stage's kinds are not.
        run = saved(self.root)
        for kind, refused in (("build", True), (None, True), ("e2e", True), ("acceptance", False), ("stub", False)):
            with self.subTest(kind=kind):
                trial = json.loads(json.dumps(run))
                pbi = state.get_pbi(trial, "PBI-3")
                pbi["status"] = "building"
                if kind is None:
                    pbi.pop("kind", None)
                else:
                    pbi["kind"] = kind
                if refused:
                    with self.assertRaisesRegex(flow.InvariantError, "BuildBeforeLock"):
                        flow.check_invariants(trial)
                    trial["lock"] = {"commit": "0" * 40}
                flow.check_invariants(trial)
                trial.update(lock=None, acceptance_first=False, state="building")
                flow.check_invariants(trial)

        # If the lock did not happen, the conductor refuses to launch the build PBI and keeps the run where it was.
        files = [{"file": "acceptance/R1.json", "verdict": "ok"}]
        write_result_for(review, {"schema": "atelier.acceptance_review/1", "files": files})
        with patch.object(locks, "lock", lambda root, run: None):
            code, out = run_cli("done", "--root", str(self.root), "--launch-id", review["launch_id"])
        self.assertEqual(2, code, out)
        self.assertIn("never BuildBeforeLock: launched PBI-3", out["error"])
        self.assertEqual(("reviewing_acceptance", None), (saved(self.root)["state"], saved(self.root)["lock"]))
        self.assertEqual(before, git(self.root, "rev-parse", "HEAD"))

        # With the lock in place the same review goes on, and the build PBI is launched after the lock commit.
        code, nxt = run_cli("done", "--root", str(self.root), "--launch-id", review["launch_id"])
        self.assertEqual(0, code, nxt)
        run = saved(self.root)
        self.assertEqual(["PBI-3"], pbis_of(nxt))
        self.assertEqual(run["lock"]["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertNotEqual(before, run["lock"]["commit"])


class LockFilesTests(unittest.TestCase):
    """Which files the lock takes, and that the lock commit leaves staged changes of other files alone."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {locks.HOME_ENV: home.name})
        env.start()
        self.addCleanup(env.stop)
        init_git_repo(self.root)
        for name in ("acc/R1.json", "acc/R2.json", "acc/support/a.json", "acc/support/b.json", "app_stub.py"):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_bytes(b"{}\n")

    def run_state(self) -> dict:
        run = state.new_run_state(self.root, self.root / "goal.md", None)
        run["acceptance_first"] = True
        run["pbis"] = [
            {
                "id": "PBI-1",
                "kind": "acceptance",
                "targets": ["acc/R1.json", "acc/R2.json", "acc/support/"],
                "acceptance_files": {"R1": "acc/R1.json", "R2": "acc/R2.json"},
                "status": "passed",
            },
            {"id": "PBI-2", "kind": "stub", "targets": ["app_stub.py"], "status": "passed"},
            {"id": "PBI-3", "kind": "build", "targets": ["app.py"], "status": "pending"},
        ]
        run["scenarios"] = [
            {"id": "R1", "mode": "normal", "cases": [{"id": "R1-1", "pbi": "PBI-3"}]},
            {"id": "R2", "mode": "normal", "no_lock": True, "cases": [{"id": "R2-1", "pbi": "PBI-3"}]},
        ]
        return run

    def test_unresolved_requirements_and_stub_files_are_left_out_and_directory_targets_are_covered(self) -> None:
        run = self.run_state()
        files = locks.lock_files(self.root, run)
        self.assertEqual(["acc/R1.json", "acc/support/a.json", "acc/support/b.json"], files)
        listed = locks.lock_list(run, files)
        self.assertEqual((["R1"], ["R1-1"], {"R1-1": "PBI-3"}), (listed["requirements"], listed["cases"], listed["assignments"]))

    def test_the_lock_commit_leaves_staged_changes_of_other_files_staged(self) -> None:
        (self.root / "app.py").write_bytes(b"x = 2\n")
        git(self.root, "add", "app.py")
        head = git(self.root, "rev-parse", "HEAD")
        run = self.run_state()
        kept = locks.lock(self.root, run)
        self.assertEqual(head, git(self.root, "rev-parse", f"{kept['commit']}^"))
        self.assertEqual(
            sorted(["acc/R1.json", "acc/support/a.json", "acc/support/b.json", locks.LIST_PATH]),
            sorted(git(self.root, "show", "--name-only", "--format=", kept["commit"]).splitlines()),
        )
        self.assertEqual(["app.py"], git(self.root, "diff", "--cached", "--name-only").splitlines())
        self.assertEqual("x = 1", git(self.root, "show", f"{kept['commit']}:app.py"))
        # The guard entry carries the lock commit's blob of every locked file.
        entry = locks.guard_entry(self.root, run)
        self.assertEqual(set(kept["files"]), set(entry["blobs"]))


if __name__ == "__main__":
    unittest.main()
