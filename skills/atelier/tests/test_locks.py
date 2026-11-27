"""The lock on the acceptance tests, driven through the CLI in temporary git repositories.

Once the planner's acceptance review is ok the conductor writes docs/pbi/locks.json, commits the locked files and the
list and nothing else, keeps a copy of the list and the lock commit's id in <git common dir>/atelier/<run>/, and writes
the copy's sha256 under ATELIER_LOCKS_HOME. A fake worker that then changes a locked file or the list is stopped by G1
and the file is put back to the lock commit's content; a change of line endings only is not a change. No build PBI is
launched before the lock. An e2e PBI that passed adds its E2E files to the lock (extend), and the lock commit, the
copy outside the work tree, the commit id and the hash move on with them. After an unlock whose lines were checked,
the fixed file and then the list with the unlock record are committed (relock), and the copy, the commit id and the
hash move on again; the check's temporary worktree is made outside the work tree and taken away with git worktree
remove and prune (add_worktree, remove_worktree).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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


class LockExtendTests(unittest.TestCase):
    """extend: the E2E files of an e2e PBI that passed join the lock list, and the lock commit moves on."""

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
        init_git_repo(self.root)
        (self.root / "acc").mkdir()
        (self.root / "acc" / "R1.json").write_bytes(b"{}\n")

    def run_state(self) -> dict:
        run = state.new_run_state(self.root, self.root / "goal.md", None)
        run["acceptance_first"] = True
        run["pbis"] = [
            {"id": "PBI-1", "kind": "acceptance", "targets": ["acc/R1.json"], "acceptance_files": {"R1": "acc/R1.json"}, "status": "passed"},
            {"id": "PBI-2", "kind": "build", "targets": ["app.py"], "status": "passed"},
            {
                "id": "PBI-3",
                "kind": "e2e",
                "targets": ["e2e/add.spec.js", "e2e/fixtures/", "e2e/missing.spec.js", "e2e/package.json", "docs/pbi/notes.md"],
                "status": "pending",
            },
        ]
        run["scenarios"] = [{"id": "R1", "mode": "normal", "cases": [{"id": "R1-1", "pbi": "PBI-2"}]}]
        return run

    def write_e2e(self) -> None:
        for name in ("e2e/add.spec.js", "e2e/fixtures/a.json", "e2e/package.json", "docs/pbi/notes.md"):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_bytes(b"// e2e\n")

    def test_the_e2e_files_join_the_list_and_the_lock_commit_moves_on(self) -> None:
        run = self.run_state()
        first = dict(locks.lock(self.root, run))
        self.assertEqual(["acc/R1.json", locks.LIST_PATH], first["files"])
        self.write_e2e()
        # A change the user staged in another file stays staged and out of the lock commit.
        (self.root / "app.py").write_bytes(b"x = 2\n")
        git(self.root, "add", "app.py")
        # Before the e2e PBI passes there is nothing to add.
        self.assertIsNone(locks.extend(self.root, run))
        self.assertEqual(first["commit"], run["lock"]["commit"])

        state.get_pbi(run, "PBI-3")["status"] = "passed"
        added = ["e2e/add.spec.js", "e2e/fixtures/a.json"]
        extension = locks.extend(self.root, run)
        kept = run["lock"]
        self.assertEqual({"pbis": ["PBI-3"], "files": added, "commit_before": first["commit"], "commit": kept["commit"]}, extension)
        self.assertEqual([extension], run["lock_extensions"])
        self.assertNotEqual(first["commit"], kept["commit"])
        self.assertEqual(kept["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(first["commit"], git(self.root, "rev-parse", f"{kept['commit']}^"))
        self.assertEqual(
            sorted([*added, locks.LIST_PATH]),
            sorted(git(self.root, "show", "--name-only", "--format=", kept["commit"]).splitlines()),
        )
        self.assertEqual(["acc/R1.json", *added, locks.LIST_PATH], kept["files"])
        self.assertEqual(["PBI-3"], kept["e2e_pbis"])
        self.assertEqual(["app.py"], git(self.root, "diff", "--cached", "--name-only").splitlines())

        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual(["acc/R1.json", *added], listed["files"])
        self.assertEqual((["R1"], ["R1-1"]), (listed["requirements"], listed["cases"]))
        # The copy outside the work tree, the commit id and the hash are the new ones, and the copy's check passes.
        outside = Path(kept["outside"])
        copy = outside / locks.COPY_NAME
        self.assertEqual((self.root / locks.LIST_PATH).read_bytes(), copy.read_bytes())
        self.assertEqual(kept["commit"] + "\n", (outside / locks.COMMIT_NAME).read_text(encoding="ascii"))
        record = json.loads((self.locks_home / f"{run['run_id']}.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        self.assertEqual((digest, kept["commit"]), (record["sha256"], record["commit"]))
        self.assertEqual(digest, kept["sha256"])
        self.assertNotEqual(first["sha256"], kept["sha256"])
        self.assertIsNone(locks.copy_problem(self.root, run))
        self.assertEqual(listed, locks.locked_list(self.root, run))
        # G1 and the wave check go by the new lock: a changed E2E file is a locked change, and it is put back.
        self.assertEqual(set(kept["files"]), set(locks.guard_entry(self.root, run)["blobs"]))
        (self.root / "e2e" / "add.spec.js").write_bytes(b"// loosened\n")
        self.assertEqual(["e2e/add.spec.js"], [c["path"] for c in locks.wave_changes(self.root, run)])
        self.assertEqual(["e2e/add.spec.js"], locks.restore(self.root, run, ["e2e/add.spec.js"]))
        self.assertEqual(b"// e2e\n", (self.root / "e2e" / "add.spec.js").read_bytes().replace(b"\r\n", b"\n"))

        # The same PBI is not added again.
        self.assertIsNone(locks.extend(self.root, run))
        self.assertEqual([extension], run["lock_extensions"])

    def test_the_files_the_lock_held_keep_the_old_lock_commits_content(self) -> None:
        run = self.run_state()
        first = locks.lock(self.root, run)
        locked_blob = git(self.root, "rev-parse", f"{first['commit']}:acc/R1.json")
        self.write_e2e()
        # A locked file changed in the work tree does not go into the new lock commit.
        (self.root / "acc" / "R1.json").write_bytes(b'{"cases": []}\n')
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        locks.extend(self.root, run)
        self.assertEqual(locked_blob, git(self.root, "rev-parse", f"{run['lock']['commit']}:acc/R1.json"))

    def test_a_lock_made_again_after_a_lift_keeps_the_passed_e2e_files(self) -> None:
        run = self.run_state()
        locks.lock(self.root, run)
        self.write_e2e()
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        locks.extend(self.root, run)
        locks.lift(run, "PR-1")
        again = locks.lock(self.root, run)
        self.assertEqual(["acc/R1.json", "e2e/add.spec.js", "e2e/fixtures/a.json", locks.LIST_PATH], again["files"])
        self.assertEqual(["PBI-3"], again["e2e_pbis"])
        self.assertIsNone(locks.extend(self.root, run))

    def test_a_lock_made_again_after_a_lift_keeps_the_e2e_files_as_their_wave_check_passed_them(self) -> None:
        run = self.run_state()
        locks.lock(self.root, run)
        self.write_e2e()
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        locks.extend(self.root, run)
        passed = run["lock"]["commit"]
        spec = git(self.root, "rev-parse", f"{passed}:e2e/add.spec.js")
        fixture = git(self.root, "rev-parse", f"{passed}:e2e/fixtures/a.json")
        locks.lift(run, "PR-1")
        # While the lock is off, the passed PBI's spec is loosened and its fixture deleted, and R1's test is revised.
        (self.root / "e2e" / "add.spec.js").write_bytes(b"// loosened\n")
        (self.root / "e2e" / "fixtures" / "a.json").unlink()
        (self.root / "acc" / "R1.json").write_bytes(b'{"revised": true}\n')
        again = locks.lock(self.root, run)
        self.assertEqual(["acc/R1.json", "e2e/add.spec.js", "e2e/fixtures/a.json", locks.LIST_PATH], again["files"])
        self.assertEqual(spec, git(self.root, "rev-parse", f"{again['commit']}:e2e/add.spec.js"))
        self.assertEqual(fixture, git(self.root, "rev-parse", f"{again['commit']}:e2e/fixtures/a.json"))
        # The revised acceptance test is read from the work tree.
        self.assertEqual('{"revised": true}', git(self.root, "show", f"{again['commit']}:acc/R1.json"))
        # The wave check finds both E2E changes against the new lock.
        self.assertEqual(["e2e/add.spec.js", "e2e/fixtures/a.json"], sorted(c["path"] for c in locks.wave_changes(self.root, run)))

    def test_a_passed_e2e_file_a_revised_acceptance_pbi_also_targets_keeps_its_locked_content(self) -> None:
        run = self.run_state()
        locks.lock(self.root, run)
        self.write_e2e()
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        locks.extend(self.root, run)
        spec = git(self.root, "rev-parse", f"{run['lock']['commit']}:e2e/add.spec.js")
        locks.lift(run, "PR-1")
        # The revised acceptance PBI also has the passed e2e PBI's spec in its targets, and the spec is loosened.
        state.get_pbi(run, "PBI-1")["targets"] = ["acc/R1.json", "e2e/add.spec.js"]
        (self.root / "e2e" / "add.spec.js").write_bytes(b"// loosened\n")
        again = locks.lock(self.root, run)
        self.assertIn("e2e/add.spec.js", again["files"])
        self.assertEqual(spec, git(self.root, "rev-parse", f"{again['commit']}:e2e/add.spec.js"))
        self.assertEqual(["e2e/add.spec.js"], [c["path"] for c in locks.wave_changes(self.root, run)])

    def test_a_lock_made_again_after_a_lift_keeps_the_locked_e2e_keys(self) -> None:
        (self.root / "package.json").write_bytes(json.dumps({"scripts": {"test": "jest"}}).encode("utf-8"))
        run = self.run_state()
        run["plan_e2e"] = ["npm run e2e"]
        locks.lock(self.root, run)
        self.write_e2e()
        (self.root / "e2e" / "package.json").write_bytes(json.dumps({"scripts": {"test": "playwright test"}}).encode("utf-8"))
        (self.root / "package.json").write_bytes(
            json.dumps({"scripts": {"test": "jest", "e2e": "npm --prefix e2e test"}}).encode("utf-8")
        )
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        locks.extend(self.root, run)
        locks.lift(run, "PR-1")
        # While the lock is off, the e2e scripts are loosened, and the acceptance script is revised.
        (self.root / "e2e" / "package.json").write_bytes(json.dumps({"scripts": {"test": "exit 0"}}).encode("utf-8"))
        (self.root / "package.json").write_bytes(
            json.dumps({"scripts": {"test": "jest --ci", "e2e": "exit 0"}}).encode("utf-8")
        )
        locks.lock(self.root, run)
        keys = locks.locked_list(self.root, run)["keys"]
        self.assertEqual("playwright test", keys["e2e/package.json"]["scripts.test"])
        self.assertEqual("npm --prefix e2e test", keys["package.json"]["scripts.e2e"])
        # The acceptance key is read from the work tree, as the revision left it.
        self.assertEqual("jest --ci", keys["package.json"]["scripts.test"])
        changed = sorted((c["path"], c.get("key")) for c in locks.wave_changes(self.root, run))
        self.assertEqual([("e2e/package.json", "scripts.test"), ("package.json", "scripts.e2e")], changed)

    def test_a_key_file_of_a_passed_e2e_pbi_that_cannot_be_read_is_recorded_even_when_nothing_else_is_added(self) -> None:
        run = self.run_state()
        state.get_pbi(run, "PBI-3")["targets"] = ["e2e/package.json"]
        first = dict(locks.lock(self.root, run))
        (self.root / "e2e").mkdir()
        (self.root / "e2e" / "package.json").write_bytes(b"{ not json\n")
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        extension = locks.extend(self.root, run)
        self.assertIsNotNone(extension)
        self.assertEqual(([], first["commit"]), (extension["files"], extension["commit_before"]))
        listed = locks.locked_list(self.root, run)
        self.assertEqual(["e2e/package.json"], [u.get("file") for u in listed["unextractable"]])
        self.assertEqual(listed["unextractable"], run["lock"]["unextractable"])
        self.assertEqual(["PBI-3"], run["lock"]["e2e_pbis"])
        self.assertIsNone(locks.copy_problem(self.root, run))
        # It is recorded once: the same PBI is not taken again.
        self.assertIsNone(locks.extend(self.root, run))

    def test_a_staged_change_of_a_file_the_new_lock_commit_carries_stays_staged(self) -> None:
        run = self.run_state()
        first = locks.lock(self.root, run)
        locked_blob = git(self.root, "rev-parse", f"{first['commit']}:acc/R1.json")
        # The user stages an edit of the locked test, then changes the work tree copy again.
        (self.root / "acc" / "R1.json").write_bytes(b'{"staged": 1}\n')
        git(self.root, "add", "acc/R1.json")
        staged = git(self.root, "rev-parse", ":acc/R1.json")
        (self.root / "acc" / "R1.json").write_bytes(b'{"work tree": 2}\n')
        self.write_e2e()
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        locks.extend(self.root, run)
        self.assertEqual(locked_blob, git(self.root, "rev-parse", f"{run['lock']['commit']}:acc/R1.json"))
        self.assertEqual(staged, git(self.root, "rev-parse", ":acc/R1.json"))
        # The files the commit took from the work tree are staged as committed.
        self.assertEqual(["acc/R1.json"], git(self.root, "diff", "--cached", "--name-only").splitlines())

    def test_the_keys_of_the_files_the_e2e_targets_name_and_the_e2e_scripts_join_the_lock(self) -> None:
        (self.root / "package.json").write_bytes(json.dumps({"scripts": {"test": "jest"}}).encode("utf-8"))
        run = self.run_state()
        run["plan_e2e"] = ["npm run e2e"]
        locks.lock(self.root, run)
        # Before an e2e PBI passes, the e2e script is not a locked key (the e2e PBI may still write it).
        self.assertEqual({"package.json": {"scripts.test": "jest", "jest": None, "cucumber": None, "mocha": None, "ava": None}},
                         locks.locked_list(self.root, run)["keys"])
        self.write_e2e()
        (self.root / "e2e" / "package.json").write_bytes(json.dumps({"scripts": {"test": "playwright test"}}).encode("utf-8"))
        (self.root / "package.json").write_bytes(
            json.dumps({"scripts": {"test": "jest", "e2e": "npm --prefix e2e test"}}).encode("utf-8")
        )
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        extension = locks.extend(self.root, run)
        keys = locks.locked_list(self.root, run)["keys"]
        self.assertEqual("npm --prefix e2e test", keys["package.json"]["scripts.e2e"])
        self.assertEqual("jest", keys["package.json"]["scripts.test"])
        self.assertEqual("playwright test", keys["e2e/package.json"]["scripts.test"])
        self.assertIn("package.json scripts.e2e", extension["keys"])
        self.assertIn("e2e/package.json scripts.test", extension["keys"])
        # A later change of those keys is a locked change.
        (self.root / "e2e" / "package.json").write_bytes(json.dumps({"scripts": {"test": "exit 0"}}).encode("utf-8"))
        (self.root / "package.json").write_bytes(json.dumps({"scripts": {"test": "jest", "e2e": "exit 0"}}).encode("utf-8"))
        changed = sorted((c["path"], c.get("key")) for c in locks.wave_changes(self.root, run))
        self.assertEqual([("e2e/package.json", "scripts.test"), ("package.json", "scripts.e2e")], changed)

    def test_without_a_lock_nothing_is_added(self) -> None:
        run = self.run_state()
        self.write_e2e()
        state.get_pbi(run, "PBI-3")["status"] = "passed"
        head = git(self.root, "rev-parse", "HEAD")
        self.assertIsNone(locks.extend(self.root, run))
        self.assertEqual((None, head), (run["lock"], git(self.root, "rev-parse", "HEAD")))
        self.assertFalse((self.root / locks.LIST_PATH).exists())


class LockAgainAfterUnlockTests(unittest.TestCase):
    """relock: once an unlock's fixed lines are checked, the fixed file is committed, then the list with the unlock
    record on top of it, and the copy outside the work tree, the commit id and the hash move on."""

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
        init_git_repo(self.root)
        for name in ("acc/R1.json", "acc/R2.json"):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_bytes(b"{}\n")

    def run_state(self) -> dict:
        run = state.new_run_state(self.root, self.root / "goal.md", None)
        run["acceptance_first"] = True
        run["pbis"] = [
            {
                "id": "PBI-1",
                "kind": "acceptance",
                "targets": ["acc/R1.json", "acc/R2.json"],
                "acceptance_files": {"R1": "acc/R1.json", "R2": "acc/R2.json"},
                "status": "passed",
            },
            {"id": "PBI-2", "kind": "build", "targets": ["app.py"], "status": "passed"},
        ]
        run["scenarios"] = [
            {"id": "R1", "mode": "normal", "cases": [{"id": "R1-1", "pbi": "PBI-2"}, {"id": "R1-2", "pbi": "PBI-2"}]},
            {"id": "R2", "mode": "normal", "cases": [{"id": "R2-1", "pbi": "PBI-2"}]},
            {"id": "R3", "mode": "normal", "no_lock": True, "cases": [{"id": "R3-1", "pbi": "PBI-2"}]},
        ]
        return run

    def test_the_fixed_file_then_the_list_with_the_record_become_the_new_lock(self) -> None:
        run = self.run_state()
        first = dict(locks.lock(self.root, run))
        r2_blob = git(self.root, "rev-parse", f"{first['commit']}:acc/R2.json")
        (self.root / "acc" / "R1.json").write_bytes(b'{"R1-2": 5}\n')
        # A locked file changed in the work tree that the unlock did not open keeps its locked content.
        (self.root / "acc" / "R2.json").write_bytes(b'{"loosened": true}\n')
        # A change the user staged in another file stays staged and out of both commits.
        (self.root / "app.py").write_bytes(b"x = 2\n")
        git(self.root, "add", "app.py")
        record = {"item": "OB-1", "cases": ["R1-2", "R1-3"], "fix": "scenario", "reason": "r", "verified": {}}
        entry = locks.relock(self.root, run, ["acc/R1.json"], record, added={"R1-3": "PBI-2"})

        kept = run["lock"]
        self.assertEqual({**record, "commit_before": first["commit"], "commit": entry["commit"]}, entry)
        self.assertEqual([entry], run["unlocks"])
        self.assertEqual(kept["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(entry["commit"], git(self.root, "rev-parse", f"{kept['commit']}^"))
        self.assertEqual(first["commit"], git(self.root, "rev-parse", f"{entry['commit']}^"))
        self.assertEqual(["acc/R1.json"], git(self.root, "show", "--name-only", "--format=", entry["commit"]).splitlines())
        self.assertEqual([locks.LIST_PATH], git(self.root, "show", "--name-only", "--format=", kept["commit"]).splitlines())
        self.assertEqual('{"R1-2": 5}', git(self.root, "show", f"{kept['commit']}:acc/R1.json"))
        self.assertEqual(r2_blob, git(self.root, "rev-parse", f"{kept['commit']}:acc/R2.json"))
        self.assertEqual(first["files"], kept["files"])
        self.assertEqual(["app.py"], git(self.root, "diff", "--cached", "--name-only").splitlines())

        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual([entry], listed["unlocks"])
        self.assertEqual(["R1-1", "R1-2", "R2-1", "R1-3"], listed["cases"])
        self.assertEqual("PBI-2", listed["assignments"]["R1-3"])
        self.assertEqual(listed, json.loads(git(self.root, "show", f"{kept['commit']}:{locks.LIST_PATH}")))
        outside = Path(kept["outside"])
        copy = outside / locks.COPY_NAME
        self.assertEqual((self.root / locks.LIST_PATH).read_bytes(), copy.read_bytes())
        self.assertEqual(kept["commit"] + "\n", (outside / locks.COMMIT_NAME).read_text(encoding="ascii"))
        record_file = json.loads((self.locks_home / f"{run['run_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual((hashlib.sha256(copy.read_bytes()).hexdigest(), kept["commit"]), (record_file["sha256"], record_file["commit"]))
        self.assertEqual(record_file["sha256"], kept["sha256"])
        self.assertNotEqual(first["sha256"], kept["sha256"])
        self.assertIsNone(locks.copy_problem(self.root, run))
        self.assertEqual(listed, locks.locked_list(self.root, run))
        # A lock made again later keeps the unlock record.
        locks.lift(run)
        self.assertEqual([entry], json.loads(json.dumps(locks.lock_list(run, ["acc/R1.json"])))["unlocks"])

    def test_the_checked_content_is_what_is_locked_again(self) -> None:
        run = self.run_state()
        first = dict(locks.lock(self.root, run))
        checked = b'{"R1-2": 5}\r\n'
        (self.root / "acc" / "R1.json").write_bytes(checked)
        digest = hashlib.sha256(checked).hexdigest()
        # A write after the check: the content is not the checked one, so nothing is locked again.
        (self.root / "acc" / "R1.json").write_bytes(b'{"R1-2": "anything"}\n')
        with self.assertRaisesRegex(state.StateError, "changed after its check"):
            locks.relock(self.root, run, ["acc/R1.json"], {"cases": ["R1-2"]}, checked={"acc/R1.json": digest})
        self.assertEqual((first["commit"], first["sha256"]), (run["lock"]["commit"], run["lock"]["sha256"]))
        self.assertEqual(first["commit"], git(self.root, "rev-parse", "HEAD"))
        # The checked content is committed as git add converts it (here, its line endings to LF).
        (self.root / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.root / ".git" / "info" / "attributes").write_bytes(b"*.json text eol=lf\n")
        (self.root / "acc" / "R1.json").write_bytes(checked)
        entry = locks.relock(self.root, run, ["acc/R1.json"], {"cases": ["R1-2"]}, checked={"acc/R1.json": digest})
        self.assertEqual(blob_now(self.root, "acc/R1.json"), git(self.root, "rev-parse", f"{entry['commit']}:acc/R1.json"))
        self.assertEqual('{"R1-2": 5}', git(self.root, "show", f"{run['lock']['commit']}:acc/R1.json"))
        self.assertEqual([], git(self.root, "diff", "--cached", "--name-only").splitlines())

    def test_a_file_that_is_not_locked_is_refused_and_nothing_moves(self) -> None:
        run = self.run_state()
        first = dict(locks.lock(self.root, run))
        with self.assertRaises(state.StateError):
            locks.relock(self.root, run, ["app.py"], {"cases": ["R1-2"]})
        self.assertEqual((first["commit"], first["sha256"]), (run["lock"]["commit"], run["lock"]["sha256"]))
        self.assertEqual(first["commit"], git(self.root, "rev-parse", "HEAD"))
        with self.assertRaises(state.StateError):
            locks.relock(self.root, {**run, "lock": None}, ["acc/R1.json"], {})

    def test_the_locked_cases_and_a_lock_entry_less_the_opened_files(self) -> None:
        run = self.run_state()
        self.assertEqual({}, locks.locked_cases(run))
        locks.lock(self.root, run)
        self.assertEqual({"R1-1": "R1", "R1-2": "R1", "R2-1": "R2"}, locks.locked_cases(run))
        entry = locks.guard_entry(self.root, run)
        opened = locks.without(entry, ["acc/R1.json"])
        self.assertEqual(["acc/R2.json", locks.LIST_PATH], sorted(opened["blobs"]))
        self.assertEqual(set(run["lock"]["files"]), set(entry["blobs"]))
        self.assertIs(entry, locks.without(entry, []))
        self.assertIsNone(locks.without(None, ["acc/R1.json"]))


class TemporaryWorktreeTests(unittest.TestCase):
    """add_worktree makes the lock commit's state again outside the work tree; remove_worktree takes it away with git
    worktree remove and prune."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        init_git_repo(self.root)
        self.head = git(self.root, "rev-parse", "HEAD")

    def listed(self) -> list[str]:
        return [ln for ln in git(self.root, "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]

    def test_a_worktree_of_a_commit_is_made_outside_the_work_tree_and_taken_away(self) -> None:
        # An uncommitted change is not in the worktree: it holds the commit's state.
        (self.root / "app.py").write_bytes(b"x = 2\n")
        tree = locks.add_worktree(self.root, self.head)
        self.addCleanup(locks.remove_worktree, self.root, tree)
        self.assertTrue(tree.is_dir())
        self.assertNotIn(self.root, tree.parents)
        self.assertTrue(tree.parent.name.startswith(locks.WORKTREE_PREFIX))
        self.assertEqual(self.head, git(tree, "rev-parse", "HEAD"))
        self.assertEqual(b"x = 1\n", (tree / "app.py").read_bytes().replace(b"\r\n", b"\n"))
        self.assertEqual(2, len(self.listed()))

        self.assertEqual([], locks.remove_worktree(self.root, tree))
        self.assertFalse(tree.exists())
        self.assertFalse(tree.parent.exists())
        self.assertEqual(1, len(self.listed()))
        # Taking it away again finds nothing left to do.
        self.assertEqual([], locks.remove_worktree(self.root, tree))

    def test_a_worktree_whose_folder_was_deleted_is_pruned(self) -> None:
        tree = locks.add_worktree(self.root, self.head)
        shutil.rmtree(tree.parent)
        self.assertEqual(2, len(self.listed()))
        self.assertEqual([], locks.remove_worktree(self.root, tree))
        self.assertEqual(1, len(self.listed()))

    def test_a_temporary_folder_inside_the_work_tree_is_refused_and_nothing_is_left(self) -> None:
        inside = self.root / "tmp"
        inside.mkdir()
        with patch.object(tempfile, "tempdir", str(inside)):
            with self.assertRaisesRegex(state.StateError, "inside the work tree"):
                locks.add_worktree(self.root, self.head)
        self.assertEqual([], os.listdir(inside))
        self.assertEqual(1, len(self.listed()))

    def test_a_folder_that_cannot_be_set_up_is_taken_away_and_the_error_is_a_state_error(self) -> None:
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        with patch.object(tempfile, "tempdir", other.name), patch.object(Path, "mkdir", side_effect=OSError("denied")):
            with self.assertRaisesRegex(state.StateError, "cannot be set up"):
                locks.add_worktree(self.root, self.head)
        self.assertEqual([], os.listdir(other.name))
        self.assertEqual(1, len(self.listed()))

    def test_only_a_folder_add_worktree_made_is_deleted(self) -> None:
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        kept = Path(other.name) / "tree"
        kept.mkdir()
        (kept / "data.txt").write_bytes(b"keep me\n")
        problems = locks.remove_worktree(self.root, kept)
        self.assertTrue((kept / "data.txt").is_file())
        self.assertTrue(problems)

    def test_the_repositorys_hooks_do_not_run_for_the_worktree(self) -> None:
        marker = self.root.parent / f"{self.root.name}-hook-ran"
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        hook = self.root / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n".encode("utf-8"))
        os.chmod(hook, 0o755)
        # The hook runs for a worktree that git adds the usual way (otherwise this test cannot tell).
        plain = Path(tempfile.mkdtemp()) / "plain"
        self.addCleanup(shutil.rmtree, plain.parent, True)
        git(self.root, "worktree", "add", "--detach", str(plain), self.head)
        git(self.root, "worktree", "remove", "--force", str(plain))
        if not marker.exists():
            self.skipTest("git does not run the post-checkout hook here")
        marker.unlink()
        tree = locks.add_worktree(self.root, self.head)
        self.addCleanup(locks.remove_worktree, self.root, tree)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
