"""A fix PBI that failed past its tries is held as unresolved without asking, with the PBIs that depend on it, and the
run goes on with the PBIs that do not; a run with unfinished PBIs is not reported as done."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import actions, eol, flow, state  # noqa: E402
from tests.test_conductor import reach_building, run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo, reach_building_with  # noqa: E402

# PBI-1 fails and its fix PBI fails twice. PBI-3 and PBI-4 depend on it, directly and through PBI-3; the chain
# PBI-5 -> PBI-6 -> PBI-7 depends only on PBI-2, which passes, so it goes on.
CHAIN = [
    {"id": "PBI-1", "title": "Add helper", "depends_on": [], "checks": ["check-1"]},
    {"id": "PBI-2", "title": "Add type", "depends_on": [], "checks": ["check-2"]},
    {"id": "PBI-3", "title": "Use helper", "depends_on": ["PBI-1"], "checks": ["check-3"]},
    {"id": "PBI-4", "title": "Wire up", "depends_on": ["PBI-3"], "checks": ["check-4"]},
    {"id": "PBI-5", "title": "Show type", "depends_on": ["PBI-2"], "checks": ["check-5"]},
    {"id": "PBI-6", "title": "Log type", "depends_on": ["PBI-5"], "checks": ["check-6"]},
    {"id": "PBI-7", "title": "Ship type", "depends_on": ["PBI-6"], "checks": ["check-7"]},
]
FIX = "CO-1-FIX"
HELD = ["PBI-1", FIX, "PBI-3", "PBI-4"]


def done(tmp: Path, act: dict, payload: dict) -> dict:
    write_result_for(act, payload)
    code, nxt = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0, nxt
    return nxt


def build_wave(tmp: Path, act: dict) -> dict:
    """Return ok for every launch of the wave; the next action is its check."""
    while act["action"] in ("launch", "launches"):
        act = done(tmp, act, {"status": "ok"})
    assert act["action"] == "check", act
    return act


def fail_fix_pbi_past_its_tries(tmp: Path, plan: list[dict], first_failed: str) -> dict:
    """Fail the first wave's check on first_failed, then its fix PBI twice; return the action after that."""
    act = build_wave(tmp, reach_building(tmp, pbis=plan))
    act = done(tmp, act, {"exit_code": 1, "output": "unused export: helper", "failed_pbis": [first_failed]})
    for _ in range(2):
        act = build_wave(tmp, act)
        act = done(tmp, act, {"exit_code": 1, "output": "lint\nunused export: helper", "failed_pbis": [FIX]})
    return act


class UnresolvedHoldTests(unittest.TestCase):
    def test_the_states_and_the_question_of_the_rebuild_are_gone(self) -> None:
        self.assertNotIn("asking_rebuild", flow.STATES)
        self.assertEqual([], [t for t in flow.TRANSITIONS if t.event in ("check_failed", "rebuild_retry", "rebuild_hold")])
        self.assertEqual([], [t for t in flow.TRANSITIONS if "asking_rebuild" in (t.source, t.target)])
        self.assertNotIn("asking_rebuild", actions.ASK_CHOICES)
        self.assertNotIn("rebuild", [kind for kind, _, _ in actions.ASK_CHOICES.values()])
        for name in ("rebuild_hold_targets", "hold_after_rebuild", "recheck_held_pbis"):
            self.assertFalse(hasattr(state, name), name)
        self.assertFalse(hasattr(actions, "_rebuild_detail"))

    def test_no_question_and_the_pbis_that_do_not_depend_on_it_go_on(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = fail_fix_pbi_past_its_tries(tmp, CHAIN, "PBI-1")
            self.assertEqual(("launch", "worker", "PBI-7"), (act["action"], act["role"], act["pbi"]))

            run = state.load_state(tmp)
            self.assertEqual("building", run["state"])
            by_id = {p["id"]: p for p in run["pbis"]}
            self.assertEqual(HELD, [p["id"] for p in run["pbis"] if p["status"] == "held"])
            for pid in HELD:
                self.assertEqual(state.UNRESOLVED_HOLD, by_id[pid]["held_reason"], pid)
                self.assertEqual("未解決による保留", state.HELD_REASON_LABELS[by_id[pid]["held_reason"]])
            self.assertEqual(["passed"] * 3, [by_id[i]["status"] for i in ("PBI-2", "PBI-5", "PBI-6")])

            co = run["carry_overs"][0]
            self.assertEqual(("CO-1", "unresolved", HELD), (co["id"], co["status"], co["held_pbis"]))
            self.assertEqual([], state.open_carry_overs(run))
            self.assertEqual(1, len(run["unresolved"]))
            item = run["unresolved"][0]
            self.assertEqual(
                ("UR-1", "carry_over", "CO-1", FIX, ["check-1"], HELD),
                (item["id"], item["kind"], item["carry_over"], item["fix_pbi"], item["commands"], item["held_pbis"]),
            )
            self.assertEqual("lint\nunused export: helper", item["output"])

            # The held PBIs are not checked again with the PBI that went on.
            act = done(tmp, act, {"status": "ok"})
            self.assertEqual("check", act["action"])
            self.assertIn("check-7", act["commands"])
            for cmd in ("check-1", "check-3", "check-4"):
                self.assertNotIn(cmd, act["commands"])
            act = done(tmp, act, {"exit_code": 0})
            self.assertEqual(("launch", "reviewer"), (act["action"], act["role"]))

    def test_the_run_finishes_unfinished_with_the_held_pbis_in_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = fail_fix_pbi_past_its_tries(tmp, CHAIN, "PBI-1")
            act = done(tmp, build_wave(tmp, act), {"exit_code": 0})
            act = done(tmp, act, {"schema": "atelier.findings/1", "findings": []})
            act = done(tmp, act, {"exit_code": 0})  # E2E runs after the final review
            # The unresolved carry-over is on the retrospective list, so the planner sorts it.
            self.assertEqual(("launch", "planner"), (act["action"], act["role"]))
            act = done(tmp, act, {"schema": "atelier.triage/1", "items": [{"id": "UR-1", "decision": "skip", "reason": "left"}]})
            self.assertEqual(("ask", "fix_approval"), (act["action"], act["kind"]))
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "nothing_to_fix")
            self.assertEqual(code, 0, act)
            self.assertEqual(act["action"], "done")
            self.assertFalse(act["complete"])
            self.assertEqual(act["unfinished_pbis"], HELD)

            report = Path(act["report"]).read_text(encoding="utf-8")
            self.assertIn("- 状態: 未完了（終わっていない PBI が 4 件ある）", report)
            self.assertIn("- PBI-1（Add helper）: 保留（未解決による保留、持ち越し CO-1）", report)
            self.assertIn(f"- {FIX}（修正: Add helper）: 保留（未解決による保留、持ち越し CO-1）", report)
            self.assertIn("- PBI-4（Wire up）: 保留（未解決による保留、持ち越し CO-1）", report)
            self.assertIn(f"- CO-1（PBI-1）: check-1 — {actions.CARRY_STATUS_LABELS['unresolved']}", report)
            self.assertIn("未解決", actions.CARRY_STATUS_LABELS["unresolved"])
            section = report.split("## 未解決", 1)[1].split("\n## ", 1)[0]
            lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
            self.assertEqual(1, len(lines), section)
            self.assertIn(f"UR-1（持ち越し CO-1、修正の PBI {FIX}）", lines[0])
            self.assertIn("PBI-1、CO-1-FIX、PBI-3、PBI-4", lines[0])
            self.assertIn("check-1", lines[0])
            self.assertIn("unused export: helper", lines[0])
            handoff = Path(act["handoff"]).read_text(encoding="utf-8")
            self.assertIn(f"- 終わっていない PBI: PBI-1、{FIX}、PBI-3、PBI-4", handoff)

    def test_without_dependents_only_the_carry_over_is_held_and_the_review_follows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pbis = [
                {"id": "PBI-1", "title": "Alone", "depends_on": []},
                {"id": "PBI-A", "title": "A", "depends_on": []},
                {"id": "PBI-B", "title": "B", "depends_on": ["PBI-A"]},
                {"id": "PBI-C", "title": "C", "depends_on": ["PBI-B"]},
            ]
            act = fail_fix_pbi_past_its_tries(tmp, pbis, "PBI-1")
            self.assertEqual(("launch", "reviewer"), (act["action"], act["role"]))
            statuses = state.pbi_statuses(state.load_state(tmp))
            self.assertEqual(
                {"PBI-1": "held", FIX: "held", "PBI-A": "passed", "PBI-B": "passed", "PBI-C": "passed"}, statuses
            )
            self.assertEqual(["PBI-1", FIX], state.load_state(tmp)["unresolved"][0]["held_pbis"])

    def save_as_the_rebuild_question(self, tmp: Path, ask: dict | None = None) -> None:
        """Save the run as an earlier version did when the fix PBI failed its second check: the fix PBI failed, the
        other PBI of the wave passed, and the run waits for the rebuild question (or for ask, raised from it)."""
        act = build_wave(tmp, reach_building(tmp, pbis=CHAIN))
        act = done(tmp, act, {"exit_code": 1, "output": "unused export: helper", "failed_pbis": ["PBI-1"]})
        act = done(tmp, build_wave(tmp, act), {"exit_code": 1, "output": "lint\nunused export: helper", "failed_pbis": [FIX]})
        build_wave(tmp, act)
        run = state.load_state(tmp)
        for pbi in run["pbis"]:
            if pbi["status"] == "built":
                pbi["status"] = "failed" if pbi["id"] == FIX else "passed"
        run["last_check_failure"] = "lint\nunused export: helper"
        run["history"].append({"from": "checking", "event": "check_failed", "to": state.REMOVED_REBUILD_STATE})
        run["state"] = state.REMOVED_REBUILD_STATE
        run["pending_action"] = {"action": "ask", "kind": "rebuild", "launch_id": "ask-0099"}
        run["pending_snapshot"] = None
        if ask is not None:
            run["return_state"] = run["state"]
            run["state"] = ask["state"]
            run["pending_action"] = {"action": "ask", "kind": ask["kind"], "launch_id": "ask-0100"}
        state.save_state(tmp, run)

    def assert_held_as_unresolved(self, tmp: Path) -> None:
        run = state.load_state(tmp)
        self.assertEqual("building", run["state"])
        self.assertEqual(HELD, [p["id"] for p in run["pbis"] if p["status"] == "held"])
        self.assertEqual({state.UNRESOLVED_HOLD}, {p["held_reason"] for p in run["pbis"] if p["status"] == "held"})
        (item,) = run["unresolved"]
        self.assertEqual(
            ("carry_over", "CO-1", FIX, HELD, "lint\nunused export: helper"),
            (item["kind"], item["carry_over"], item["fix_pbi"], item["held_pbis"], item["output"]),
        )
        self.assertIn(state.REMOVED_REBUILD_STATE, item["reason"])

    def test_a_run_saved_in_the_removed_rebuild_question_goes_on_without_asking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.save_as_the_rebuild_question(tmp)
            # The saved question is gone: an answer to it is refused, and the run goes on as the flow does now.
            code, out = run_cli("answer", "--root", str(tmp), "--choice", "hold")
            self.assertEqual(2, code)
            self.assertIn("no pending question in state building", out["error"])
            code, act = run_cli("next", "--root", str(tmp))
            self.assertEqual(0, code, act)
            self.assertEqual(("launch", "worker", "PBI-7"), (act["action"], act["role"], act["pbi"]))
            self.assert_held_as_unresolved(tmp)
            self.assertEqual(
                {"from": state.REMOVED_REBUILD_STATE, "event": "rebuild_question_removed", "to": "building"},
                state.load_state(tmp)["history"][-1],
            )

    def test_a_question_raised_from_the_removed_rebuild_question_goes_back_to_building(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.save_as_the_rebuild_question(tmp, {"state": "asking_budget", "kind": "budget"})
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "resume")
            self.assertEqual(0, code, act)
            self.assertEqual(("launch", "worker", "PBI-7"), (act["action"], act["role"], act["pbi"]))
            self.assert_held_as_unresolved(tmp)

    def test_hold_unresolved_holds_the_indirect_dependents_and_keeps_an_earlier_reason(self) -> None:
        run = {
            "pbis": [
                {"id": "PBI-1", "status": "carried"},
                {"id": "CO-1-FIX", "status": "failed", "kind": "fix"},
                {"id": "PBI-2", "status": "pending", "depends_on": ["PBI-1", "CO-1-FIX"]},
                {"id": "PBI-3", "status": "held", "held_reason": "stale_dependency", "depends_on": ["PBI-2"]},
                {"id": "PBI-4", "status": "pending", "depends_on": ["PBI-3"]},
                {"id": "PBI-5", "status": "pending", "depends_on": []},
                {"id": "PBI-6", "status": "passed", "depends_on": ["PBI-1"]},
            ],
            "carry_overs": [{"id": "CO-1", "pbis": ["PBI-1"], "fix_pbi": "CO-1-FIX", "status": "open"}],
        }
        held = state.hold_unresolved(run, run["carry_overs"][0])
        self.assertEqual(["PBI-1", "CO-1-FIX", "PBI-2", "PBI-3", "PBI-4"], held)
        by_id = {p["id"]: p for p in run["pbis"]}
        self.assertEqual("stale_dependency", by_id["PBI-3"]["held_reason"])
        self.assertEqual(state.UNRESOLVED_HOLD, by_id["PBI-4"]["held_reason"])
        self.assertEqual(("pending", "passed"), (by_id["PBI-5"]["status"], by_id["PBI-6"]["status"]))
        self.assertEqual("unresolved", run["carry_overs"][0]["status"])

    def test_passed_and_sensitive_pbis_are_finished(self) -> None:
        run = {"pbis": [{"id": "PBI-1", "status": "passed"}, {"id": "PBI-2", "status": "sensitive"}]}
        self.assertEqual(state.unfinished_pbis(run), [])

    def test_finished_run_reports_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_building(tmp)
            act = done(tmp, act, {"status": "ok"})
            act = done(tmp, act, {"exit_code": 0})
            act = done(tmp, act, {"schema": "atelier.findings/1", "findings": []})
            act = done(tmp, act, {"exit_code": 0})
            self.assertEqual(act["action"], "done")
            self.assertTrue(act["complete"])
            self.assertIn("- 状態: done", Path(act["report"]).read_text(encoding="utf-8"))


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RestoreLfTests(unittest.TestCase):
    def _repo(self, td: str, autocrlf: str = "false") -> Path:
        root = Path(td)
        git(root, "init", "-q")
        git(root, "config", "core.autocrlf", autocrlf)
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "Test")
        (root / "lf.ts").write_bytes(b"a\nb\n")
        (root / "crlf.cs").write_bytes(b"a\r\nb\r\n")
        (root / "run.bat").write_bytes(b"echo\r\n")
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "init")
        return root

    def test_lf_files_saved_with_crlf_go_back_to_lf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            (root / "lf.ts").write_bytes(b"a\r\nb\r\nc\r\n")
            (root / "new.ts").write_bytes(b"x\r\n")
            (root / "new.bat").write_bytes(b"x\r\n")
            (root / "crlf.cs").write_bytes(b"a\r\nc\r\n")
            (root / "bin.dat").write_bytes(b"\0\r\n")
            paths = ["lf.ts", "new.ts", "new.bat", "crlf.cs", "bin.dat", "missing.ts", "../outside.ts"]
            self.assertEqual(eol.restore_lf(root, paths), ["lf.ts", "new.ts"])
            self.assertEqual((root / "lf.ts").read_bytes(), b"a\nb\nc\n")
            self.assertEqual((root / "new.bat").read_bytes(), b"x\r\n")
            self.assertEqual((root / "crlf.cs").read_bytes(), b"a\r\nc\r\n")

    def test_checkout_rules_decide(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td, autocrlf="true")
            (root / "lf.ts").write_bytes(b"a\r\n")
            self.assertEqual(eol.restore_lf(root, ["lf.ts"]), [])
            (root / ".gitattributes").write_text("*.ts eol=lf\n*.md eol=crlf\n", encoding="utf-8")
            (root / "doc.md").write_bytes(b"a\r\n")
            self.assertEqual(eol.restore_lf(root, ["lf.ts", "doc.md"]), ["lf.ts"])

    def test_worker_return_restores_lf_before_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            git(root, "config", "core.autocrlf", "false")
            act = reach_building_with(root, [{"id": "PBI-1", "title": "Edit", "depends_on": [], "targets": ["app.py"]}])
            (root / "app.py").write_bytes(b"x = 2\r\ny = 3\r\n")
            act = done(root, act, {"status": "ok", "changed_files": ["app.py"]})
            self.assertEqual(act["action"], "check")
            self.assertEqual((root / "app.py").read_bytes(), b"x = 2\ny = 3\n")
            self.assertEqual(state.load_state(root)["eol_restored"], ["app.py"])


if __name__ == "__main__":
    unittest.main()
