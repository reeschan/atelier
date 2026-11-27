"""The backlog and the report (Seam 1).

What the final check leaves over goes to <git common dir>/atelier/backlog.jsonl at the report, one line per leftover in
the shape of a retrospective item with the run and the leftover it came from; the run is then not complete and the
report's first line says how many were left over. The next run's plan launch gives the planner the entries no run took
as PBI candidates; a PBI names the ones it takes, and when its plan is approved they record the run that took them. A
work root that is not a git repository writes no backlog, and the report says so.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from atelier import actions, backlog, plan, retro, state
from tests.test_conductor import SKILLS_DIR, VALID_FACTS, make_valid_plan, run_cli
from tests.test_final_check import reach_final_check
from tests.test_flow_rules import init_git_repo
from tests.test_review_loops import approve, done, run_state, start

FAILED_FINAL_CHECK = {
    "exit_code": 1,
    "output": "line 1\nFAIL b.test",
    "commands": [
        {"command": "npm test -- b", "exit_code": 1},
        {"command": "npm run type-check", "exit_code": 0},
        {"command": "npm run e2e", "exit_code": 2},
    ],
}
NOT_GIT_LINE = "- backlog: 作業ルートが git のリポジトリでないため、backlog を読み書きしていない"


def backlog_file(root: Path) -> Path:
    common = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=True
    ).stdout.strip()
    path = Path(common)
    return (path if path.is_absolute() else root / path).resolve() / "atelier" / "backlog.jsonl"


def entries(root: Path) -> list[dict]:
    text = backlog_file(root).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def finish_with_leftovers(root: Path) -> dict:
    """A run whose final check fails on two commands; returns the done action."""
    act = reach_final_check(root)
    code, act = done(root, act, FAILED_FINAL_CHECK)
    assert (code, act["action"]) == (0, "done"), act
    return act


def plan_launch(root: Path) -> dict:
    """Start the next run on the same work root and return the plan launch of its planner."""
    act = start(root)
    assert act["role"] == "surveyor", act
    code, act = done(root, act, VALID_FACTS)
    assert code == 0, act
    code, act = run_cli("answer", "--root", str(root), "--choice", "skip")
    assert code == 0 and act["role"] == "planner", act
    return act


def plan_taking(*taken: str) -> dict:
    obj = make_valid_plan([{"id": "PBI-1", "title": "fix b", "targets": ["b.py"], "checks": ["npm test -- b"]}])
    if taken:
        obj["pbis"][0]["backlog"] = list(taken)
    return obj


class BacklogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def git_root(self) -> Path:
        init_git_repo(self.root)
        return self.root

    def test_leftovers_go_to_the_backlog_one_line_each(self) -> None:
        root = self.git_root()
        act = finish_with_leftovers(root)
        run = run_state(root)
        rid = run["run_id"]
        lines = entries(root)
        self.assertEqual([f"{rid}:LO-1", f"{rid}:LO-2"], [e["id"] for e in lines])
        for entry, lo in zip(lines, run["leftovers"]):
            self.assertEqual((rid, lo["id"]), (entry["run"], entry["item"]))
            for field in retro.FIELDS[1:]:
                self.assertIn(field, entry)
            self.assertEqual(lo["kind"], entry["kind"])
            self.assertEqual(lo["what"], entry["what"])
            self.assertEqual("FAIL b.test", entry["why"])
            self.assertEqual(dict(lo), entry["source"]["leftover"])
            # The retrospective item the leftover comes from, as it is.
            self.assertEqual(["F-1"], [i["id"] for i in entry["source"]["items"]])
            self.assertIsNone(entry["taken_by"])
        self.assertEqual(["b.py"], lines[0]["affects"]["files"])

        self.assertFalse(act["complete"])
        self.assertEqual(["LO-1", "LO-2"], act["leftovers"])
        self.assertEqual(str(backlog_file(root)), act["backlog"])
        report = Path(act["report"]).read_text(encoding="utf-8")
        first = report.splitlines()[0]
        self.assertEqual(f"# Atelier v8 報告 ({rid}) — 未完了: 残り 2 件を backlog に入れた", first)
        self.assertIn("- 状態: 未完了（残りが 2 件ある）", report)
        self.assertIn(f"- backlog: 残り 2 件（LO-1、LO-2）を {backlog_file(root)} に入れた", report)
        self.assertIn("- 残り: 2 件（backlog", Path(act["handoff"]).read_text(encoding="utf-8"))

        # The report is written again (next), the backlog is not.
        code, again = run_cli("next", "--root", str(root))
        self.assertEqual((0, "done", False), (code, again["action"], again["complete"]))
        self.assertEqual(2, len(entries(root)))

    def test_the_next_run_gets_the_backlog_as_candidates_and_takes_one(self) -> None:
        root = self.git_root()
        finish_with_leftovers(root)
        first = run_state(root)["run_id"]
        taken, left = f"{first}:LO-1", f"{first}:LO-2"

        act = plan_launch(root)
        prompt = Path(act["prompt"]).read_text(encoding="utf-8")
        line = next(ln for ln in prompt.splitlines() if ln.startswith("Backlog candidates: "))
        shown = json.loads(line[len("Backlog candidates: "):])
        self.assertEqual([taken, left], [c["id"] for c in shown])
        self.assertEqual(("e2e_failure", first, "LO-2"), (shown[1]["kind"], shown[1]["run"], shown[1]["item"]))
        self.assertIn("`backlog`", prompt)  # the planner's instructions name the field

        code, ask = done(root, act, plan_taking(taken))
        self.assertEqual((0, "plan_approval"), (code, ask.get("kind")), ask)
        self.assertEqual(["PBI-1"], [p["id"] for p in run_state(root)["pbis"] if p.get("backlog") == [taken]])
        self.assertIn(f"backlog の 1 件を取り込みます（{taken} → PBI-1）", Path(ask["draft"]).read_text(encoding="utf-8"))
        self.assertIsNone(entries(root)[0]["taken_by"])  # not before the plan is approved

        approve(root)
        second = run_state(root)["run_id"]
        by_id = {e["id"]: e for e in entries(root)}
        self.assertEqual((second, ["PBI-1"]), (by_id[taken]["taken_by"], by_id[taken]["taken_pbis"]))
        self.assertIsNone(by_id[left]["taken_by"])
        self.assertEqual([taken], run_state(root)["backlog"]["taken"])

        # A run after it no longer gets the entry that was taken.
        act = start(root, "--force")
        code, act = done(root, act, VALID_FACTS)
        code, act = run_cli("answer", "--root", str(root), "--choice", "skip")
        line = next(ln for ln in Path(act["prompt"]).read_text(encoding="utf-8").splitlines() if ln.startswith("Backlog candidates: "))
        self.assertEqual([left], [c["id"] for c in json.loads(line[len("Backlog candidates: "):])])

    def test_a_backlog_file_that_is_not_utf8_does_not_stop_the_plan_approval(self) -> None:
        root = self.git_root()
        finish_with_leftovers(root)
        taken = f"{run_state(root)['run_id']}:LO-1"
        act = plan_launch(root)
        code, ask = done(root, act, plan_taking(taken))
        self.assertEqual((0, "plan_approval"), (code, ask.get("kind")), ask)
        # The file goes bad between the plan and its approval: the approval is kept and the error is recorded.
        backlog_file(root).write_bytes(b"\xff\xfe not utf-8\n")
        approve(root)
        run = run_state(root)
        self.assertTrue(run["approvals"]["plan"])
        self.assertIn("utf-8", run["backlog"]["take_error"].lower())

    def test_a_plan_naming_an_unknown_backlog_entry_is_refused(self) -> None:
        root = self.git_root()
        finish_with_leftovers(root)
        act = plan_launch(root)
        code, out = done(root, act, plan_taking("run-elsewhere:LO-9"))
        self.assertEqual(2, code, out)
        self.assertIn("run-elsewhere:LO-9", out["error"])
        self.assertIn("backlog", out["error"])

    def test_validate_plan_takes_only_the_candidates(self) -> None:
        self.assertEqual([], plan.validate_plan(plan_taking("r:LO-1"), backlog_ids=["r:LO-1", "r:LO-2"]))
        self.assertEqual([], plan.validate_plan(plan_taking()))
        errors = plan.validate_plan(plan_taking("r:LO-3"), backlog_ids=["r:LO-1"])
        self.assertEqual(1, len(errors), errors)
        self.assertIn("'r:LO-3'", errors[0])
        self.assertTrue(plan.validate_plan(plan_taking("r:LO-1")))  # no candidates: no name is known
        bad = plan_taking()
        bad["pbis"][0]["backlog"] = "r:LO-1"
        self.assertTrue(plan.validate_plan(bad, backlog_ids=["r:LO-1"]))

    def test_a_run_without_leftovers_is_complete_as_before(self) -> None:
        root = self.git_root()
        act = reach_final_check(root)
        code, act = done(root, act, {"exit_code": 0})
        self.assertEqual((0, "done"), (code, act["action"]), act)
        self.assertTrue(act["complete"])
        self.assertEqual([], act["leftovers"])
        report = Path(act["report"]).read_text(encoding="utf-8")
        self.assertEqual(f"# Atelier v8 報告 ({run_state(root)['run_id']})", report.splitlines()[0])
        self.assertIn("- 状態: done", report)
        self.assertIn("- backlog: 残りが無いため、backlog に入れたものは無い", report)
        self.assertFalse(backlog_file(root).exists())

    def test_a_work_root_outside_git_writes_no_backlog_and_says_so(self) -> None:
        act = finish_with_leftovers(self.root)
        self.assertFalse(act["complete"])
        self.assertIsNone(act["backlog"])
        run = run_state(self.root)
        self.assertFalse(run["backlog"]["git"])
        self.assertEqual([], run["backlog"]["written"])
        report = Path(act["report"]).read_text(encoding="utf-8")
        self.assertIn("残り 2 件（作業ルートが git のリポジトリでないため backlog に入れていない）", report.splitlines()[0])
        self.assertIn(NOT_GIT_LINE + "（残り 2 件はこの報告の「残り」にだけある）", report)
        self.assertEqual([], list(self.root.rglob("backlog.jsonl")))

        act = plan_launch(self.root)
        self.assertIn(
            "Backlog candidates: none (the work root is not a git repository, so there is no backlog)",
            Path(act["prompt"]).read_text(encoding="utf-8"),
        )

    def test_the_report_counts_plan_returns_and_names_the_skipped_checks(self) -> None:
        run = state.new_run_state(self.root, self.root / "goal.md", None)
        run["state"] = "done"
        run["plan_returns"] = [{"id": "PR-1", "decision": "continue", "stops": []}]
        run["counters"]["plan_returns"] = 1
        run["e2e_skipped"] = True
        run["final_check"] = {"result": "skipped", "commands": []}
        report = actions._write_report(self.root, run).read_text(encoding="utf-8")
        self.assertIn("- 途中の戻り: 1 回", report)
        self.assertIn("- 飛ばした機械の検査: E2E（コマンドが無い）、最後の検査（流すコマンドが無い）", report)
        self.assertIn(NOT_GIT_LINE, report)
        run = state.new_run_state(self.root, self.root / "goal.md", None)
        run["state"] = "done"
        report = actions._write_report(self.root, run).read_text(encoding="utf-8")
        self.assertIn("- 途中の戻り: 0 回", report)
        self.assertIn("- 飛ばした機械の検査: なし", report)

    def test_an_entry_another_run_took_first_keeps_that_run(self) -> None:
        root = self.git_root()
        path = backlog_file(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"id": "r1:LO-1", "run": "r1", "item": "LO-1", "taken_by": "r0"},
            {"id": "r1:LO-2", "run": "r1", "item": "LO-2", "taken_by": None},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
        run = {"run_id": "r2", "pbis": [{"id": "PBI-1", "backlog": ["r1:LO-1", "r1:LO-2"]}]}
        self.assertEqual(["r1:LO-2"], backlog.take(root, run))
        self.assertEqual(["r1:LO-1"], run["backlog"]["taken_elsewhere"])
        kept = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual("not json", kept[-1])  # a line that is no entry stays as it was
        self.assertEqual(["r0", "r2"], [json.loads(ln)["taken_by"] for ln in kept[:2]])
        self.assertEqual(["r1:LO-2"], [c["id"] for c in backlog.candidates(root, {"run_id": "r2"})])
        self.assertEqual([], backlog.candidates(root, {"run_id": "r3"}))

    def write_backlog(self, root: Path, *ids: str) -> Path:
        path = backlog_file(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"id": i, "run": i.split(":")[0], "item": i.split(":")[1], "taken_by": None} for i in ids]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return path

    def test_a_re_plan_that_drops_the_naming_pbi_releases_the_entry(self) -> None:
        root = self.git_root()
        self.write_backlog(root, "r1:LO-1", "r1:LO-2")
        run = {"run_id": "r2", "pbis": [
            {"id": "PBI-1", "backlog": ["r1:LO-1"]},
            {"id": "PBI-2", "backlog": ["r1:LO-2"]},
        ]}
        self.assertEqual(["r1:LO-1", "r1:LO-2"], backlog.take(root, run))
        self.assertEqual([], backlog.candidates(root, {"run_id": "r3"}))

        # The re-plan keeps PBI-2 only: LO-1 is no longer this run's.
        run["pbis"] = [{"id": "PBI-2", "backlog": ["r1:LO-2"]}]
        self.assertEqual(["r1:LO-2"], backlog.take(root, run))
        by_id = {e["id"]: e for e in entries(root)}
        self.assertIsNone(by_id["r1:LO-1"]["taken_by"])
        self.assertNotIn("taken_pbis", by_id["r1:LO-1"])
        self.assertNotIn("taken_at", by_id["r1:LO-1"])
        self.assertEqual("r2", by_id["r1:LO-2"]["taken_by"])
        self.assertEqual(["r1:LO-2"], run["backlog"]["taken"])
        self.assertEqual(["r1:LO-1"], [c["id"] for c in backlog.candidates(root, {"run_id": "r3"})])

        # A re-plan that names no entry releases them all.
        run["pbis"] = [{"id": "PBI-3"}]
        self.assertEqual([], backlog.take(root, run))
        self.assertEqual([], run["backlog"]["taken"])
        self.assertEqual(["r1:LO-1", "r1:LO-2"], [c["id"] for c in backlog.candidates(root, {"run_id": "r3"})])

    def test_an_unfinished_naming_pbi_releases_the_entry_at_the_report(self) -> None:
        root = self.git_root()
        self.write_backlog(root, "r1:LO-1", "r1:LO-2", "r1:LO-3")
        run = {"run_id": "r2", "pbis": [
            {"id": "PBI-1", "backlog": ["r1:LO-1"], "status": "passed"},
            {"id": "PBI-2", "backlog": ["r1:LO-2"], "status": "held"},
            {"id": "PBI-3", "backlog": ["r1:LO-3"], "status": "pending"},
        ]}
        backlog.take(root, run)
        self.assertEqual([], backlog.candidates(root, {"run_id": "r3"}))

        backlog.add_leftovers(root, run)
        by_id = {e["id"]: e for e in entries(root)}
        self.assertEqual("r2", by_id["r1:LO-1"]["taken_by"])
        self.assertEqual([None, None], [by_id[i]["taken_by"] for i in ("r1:LO-2", "r1:LO-3")])
        self.assertEqual(["r1:LO-1"], run["backlog"]["taken"])
        self.assertEqual(["r1:LO-2", "r1:LO-3"], run["backlog"]["released"])
        self.assertEqual(["r1:LO-2", "r1:LO-3"], [c["id"] for c in backlog.candidates(root, {"run_id": "r3"})])
        lines = backlog.report_lines(run)
        self.assertIn("- backlog から取り込んだ件: r1:LO-1（PBI-1）", lines)
        self.assertIn("- backlog に戻した件（この run で直さなかった）: r1:LO-2、r1:LO-3", lines)

        # An entry one passed PBI of several names stays taken.
        self.write_backlog(root, "r1:LO-4")
        run = {"run_id": "r4", "pbis": [
            {"id": "PBI-1", "backlog": ["r1:LO-4"], "status": "held"},
            {"id": "PBI-2", "backlog": ["r1:LO-4"], "status": "passed"},
        ]}
        backlog.take(root, run)
        backlog.add_leftovers(root, run)
        self.assertEqual("r4", entries(root)[0]["taken_by"])
        self.assertNotIn("released", run["backlog"])


class BacklogLockTests(unittest.TestCase):
    """Runs in other worktrees change the same backlog: each change holds the lock from its read to its write."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        init_git_repo(self.root)
        self.path = backlog_file(self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"id": "r1:LO-1", "run": "r1", "item": "LO-1", "taken_by": None}) + "\n", encoding="utf-8")

    def run_during_write(self, other) -> dict:
        """take r2's entry, running `other` in a second thread while the take is about to replace the file."""
        real_write = backlog._write_lines
        thread = threading.Thread(target=other)
        run = {"run_id": "r2", "pbis": [{"id": "PBI-1", "backlog": ["r1:LO-1"]}]}

        def write_after_other(path: Path, lines: list[str]) -> None:
            if thread.ident is None:  # The first write only.
                thread.start()
                thread.join(0.5)  # With the lock, the other change waits for this write.
            real_write(path, lines)

        with mock.patch.object(backlog, "_write_lines", write_after_other):
            backlog.take(self.root, run)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assert_lock_free()
        return run

    def assert_lock_free(self) -> None:
        """The lock is taken at the first try: no change holds it any more."""
        with mock.patch.object(backlog, "LOCK_WAIT_SECONDS", 0):
            with backlog._locked(self.path):
                pass

    def test_an_append_waits_for_a_take_and_is_not_lost(self) -> None:
        other = {"run_id": "r3", "pbis": [], "leftovers": [{"id": "LO-1", "kind": "check_failure", "what": "w", "output": "o"}]}
        self.run_during_write(lambda: backlog.add_leftovers(self.root, other))
        self.assertNotIn("error", other["backlog"])
        by_id = {e["id"]: e for e in entries(self.root)}
        self.assertEqual(["r1:LO-1", "r3:LO-1"], list(by_id))
        self.assertEqual("r2", by_id["r1:LO-1"]["taken_by"])

    def test_two_runs_do_not_both_take_one_entry(self) -> None:
        other = {"run_id": "r3", "pbis": [{"id": "PBI-1", "backlog": ["r1:LO-1"]}]}
        run = self.run_during_write(lambda: backlog.take(self.root, other))
        self.assertEqual(["r1:LO-1"], run["backlog"]["taken"])
        self.assertEqual(([], ["r1:LO-1"]), (other["backlog"]["taken"], other["backlog"]["taken_elsewhere"]))
        self.assertEqual("r2", entries(self.root)[0]["taken_by"])

    def start_holder(self) -> subprocess.Popen:
        """Another process (a run in another worktree) that takes the lock and holds it until it is killed."""
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from atelier import backlog\n"
            "with backlog._locked(Path(sys.argv[2])):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(120)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(SKILLS_DIR), str(self.path)], stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(self.stop, proc)
        self.assertEqual("held", proc.stdout.readline().strip())
        return proc

    @staticmethod
    def stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)
        proc.stdout.close()

    def test_a_live_holder_keeps_the_lock_however_old_its_file_is(self) -> None:
        lock = backlog.lock_path(self.path)
        holder = self.start_holder()
        old = time.time() - 3600
        os.utime(lock, (old, old))  # The file's age does not matter: no waiter takes the lock from a live holder.
        run = {"run_id": "r2", "pbis": [{"id": "PBI-1", "backlog": ["r1:LO-1"]}]}
        with mock.patch.object(backlog, "LOCK_WAIT_SECONDS", 0.3):
            with self.assertRaises(TimeoutError):
                backlog.take(self.root, run)
            left = {"run_id": "r3", "pbis": [], "leftovers": [{"id": "LO-1", "kind": "check_failure", "what": "w"}]}
            backlog.add_leftovers(self.root, left)
        self.assertIn(str(lock), left["backlog"]["error"])
        self.assertEqual(["r1:LO-1"], [e["id"] for e in entries(self.root)])
        self.assertIsNone(entries(self.root)[0]["taken_by"])

        # The holder dies: the OS lets its lock go, and the file left behind does not hold anyone up.
        self.stop(holder)
        self.assertTrue(lock.exists())
        self.assert_lock_free()

    def test_runs_waiting_for_a_holder_that_dies_go_on_one_at_a_time(self) -> None:
        holder = self.start_holder()
        run = {"run_id": "r2", "pbis": [{"id": "PBI-1", "backlog": ["r1:LO-1"]}]}
        left = {"run_id": "r3", "pbis": [], "leftovers": [{"id": "LO-1", "kind": "check_failure", "what": "w", "output": "o"}]}
        real_write = backlog._write_lines

        def slow_write(path: Path, lines: list[str]) -> None:
            time.sleep(0.3)  # An append that got in between the take's read and this write would be lost by it.
            real_write(path, lines)

        threads = [
            threading.Thread(target=backlog.take, args=(self.root, run)),
            threading.Thread(target=backlog.add_leftovers, args=(self.root, left)),
        ]
        with mock.patch.object(backlog, "_write_lines", slow_write):
            for thread in threads:
                thread.start()
            time.sleep(0.3)
            self.assertTrue(all(thread.is_alive() for thread in threads))  # Both wait for the holder.
            self.stop(holder)
            for thread in threads:
                thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertNotIn("error", left["backlog"])
        by_id = {e["id"]: e for e in entries(self.root)}
        self.assertEqual(["r1:LO-1", "r3:LO-1"], list(by_id))
        self.assertEqual("r2", by_id["r1:LO-1"]["taken_by"])
        self.assert_lock_free()


if __name__ == "__main__":
    unittest.main()
