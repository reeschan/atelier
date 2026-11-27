"""The fix stage runs once, then the final check.

Once every fix group is built, the final check runs the whole check (the plan's checks and the checks of the PBIs
that own a fixed file) and the plan's E2E once. What fails is not built again: it is left over on the run (its kind,
the command, the end of the output and the retrospective items it comes from) and the run goes to the report. What a
fix worker returns in `open` is left over too. A run with nothing to fix goes to the report without the final check.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from atelier import flow, state
from tests.test_acceptance_red import approve_plan, drive, finish_stage, pass_review, pbis_of, saved
from tests.test_acceptance_toggle import UNIT
from tests.test_conductor import VALID_FACTS, make_finding, make_valid_plan, run_cli
from tests.test_review_loops import approve, done, drive_to_plan_approval, run_state
from tests.test_unlock import BUILT, IMPL, R1_FILE, R2, R2_FILE, STUB, UnlockTestBase, r1, unlock_plan
from tests.test_wave_locks import PBI_CHECK

WHOLE = ["npm run type-check"]
E2E = ["npm run e2e"]


def two_pbi_plan() -> dict:
    plan_obj = make_valid_plan([
        {"id": "PBI-1", "title": "a", "checks": ["npm test -- a"], "targets": ["a.py"]},
        {"id": "PBI-2", "title": "b", "checks": ["npm test -- b"], "targets": ["b.py"]},
    ])
    plan_obj["checks"] = list(WHOLE)
    plan_obj["e2e"] = list(E2E)
    return plan_obj


def reach_fix_approval(tmp: Path, findings: list[dict], decisions: list[dict]) -> dict:
    """Build both PBIs, pass the wave, review, pass E2E and triage; return the fix approval question."""
    drive_to_plan_approval(tmp, VALID_FACTS, two_pbi_plan())
    act = approve(tmp)
    code, act = done(tmp, act, {"status": "ok"})
    assert act["action"] == "check", act
    code, act = done(tmp, act, {"exit_code": 0})
    code, act = done(tmp, act, {"schema": "atelier.findings/1", "findings": findings})
    assert (act["action"], act["commands"]) == ("check", E2E), act
    code, act = done(tmp, act, {"exit_code": 0})
    if act["action"] == "launch":
        code, act = done(tmp, act, {"schema": "atelier.triage/1", "items": decisions})
    assert (code, act["action"], act.get("kind")) == (0, "ask", "fix_approval"), act
    return act


def reach_final_check(tmp: Path, worker_result: dict | None = None) -> dict:
    """Fix F-1 (in b.py) and return the final check."""
    reach_fix_approval(
        tmp,
        [make_finding("F-1", files=["b.py"])],
        [{"id": "F-1", "decision": "fix", "reason": "bug"}],
    )
    act = approve(tmp)
    assert (act["action"], act["role"]) == ("launch", "worker"), act
    code, act = done(tmp, act, worker_result or {"status": "ok"})
    assert code == 0, act
    return act


def moves(tmp: Path) -> list[tuple[str, str, str]]:
    return [(h["from"], h["event"], h["to"]) for h in run_state(tmp)["history"]]


class FinalCheckTests(unittest.TestCase):
    def test_after_the_fix_workers_one_check_runs_the_whole_check_and_e2e(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = reach_final_check(tmp)
            self.assertEqual("check", act["action"], act)
            self.assertEqual(["npm test -- b", *WHOLE, *E2E], act["commands"])
            self.assertEqual("final_checking", run_state(tmp)["state"])
            self.assertIn(("fixing", "fixes_built", "final_checking"), moves(tmp))

            code, act = done(tmp, act, {"exit_code": 0})
            self.assertEqual((0, "done"), (code, act["action"]), act)
            run = run_state(tmp)
            self.assertEqual("passed", run["final_check"]["result"])
            self.assertEqual([], run["leftovers"])
            self.assertEqual(1, [to for _, _, to in moves(tmp)].count("fixing"))
            self.assertIn(("final_checking", "final_check_done", "reporting"), moves(tmp))
            self.assertIn("- 最後の検査: 通った", Path(act["report"]).read_text(encoding="utf-8"))

    def test_a_failed_final_check_is_left_over_and_no_fix_worker_is_launched(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = reach_final_check(tmp)
            tail = "\n".join(f"line {i}" for i in range(40)) + "\nFAIL b.test"
            code, act = done(tmp, act, {
                "exit_code": 1,
                "output": tail,
                "commands": [
                    {"command": "npm test -- b", "exit_code": 1},
                    {"command": "npm run type-check", "exit_code": 0},
                    {"command": "npm run e2e", "exit_code": 2},
                ],
            })
            self.assertEqual((0, "done"), (code, act["action"]), act)
            run = run_state(tmp)
            self.assertEqual("done", run["state"])
            self.assertEqual("failed", run["final_check"]["result"])
            leftovers = run["leftovers"]
            self.assertEqual(
                [("check_failure", "npm test -- b", 1), ("e2e_failure", "npm run e2e", 2)],
                [(lo["kind"], lo["command"], lo["exit_code"]) for lo in leftovers],
            )
            for lo in leftovers:
                self.assertEqual(["F-1"], lo["items"])
                self.assertTrue(lo["output"].endswith("FAIL b.test"))
                self.assertNotIn("line 0\n", lo["output"])  # only the end of the output
            # No fix worker after the final check, and the fix stage was entered once
            history = moves(tmp)
            after = history[[m[2] for m in history].index("final_checking"):]
            self.assertEqual([], [m for m in after if m[2] == "fixing"])
            self.assertEqual(1, [m[2] for m in history].count("fixing"))
            self.assertEqual(1, run["counters"]["fix_stages"])
            report = Path(act["report"]).read_text(encoding="utf-8")
            self.assertIn("- 最後の検査: 落ちた（2 件を残りにした。作り直していない）", report)
            self.assertIn("## 残り", report)
            self.assertIn("LO-2 E2E の失敗: 最後の検査で npm run e2e が落ちた（元の件: F-1）", report)

    def test_a_failed_final_check_without_its_commands_is_one_leftover(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = reach_final_check(tmp)
            code, act = done(tmp, act, {"exit_code": 1, "output": "something failed"})
            self.assertEqual((0, "done"), (code, act["action"]), act)
            leftovers = run_state(tmp)["leftovers"]
            self.assertEqual(1, len(leftovers))
            self.assertEqual("check_failure", leftovers[0]["kind"])
            self.assertIsNone(leftovers[0]["command"])
            self.assertEqual(["npm test -- b", *WHOLE, *E2E], leftovers[0]["commands"])
            self.assertEqual("something failed", leftovers[0]["output"])

    def test_what_a_fix_worker_could_not_fix_is_left_over(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = reach_final_check(tmp, {
                "status": "ok",
                "open": ["the b.py fallback needs a design decision", {"what": "c.py", "why": "outside the files"}],
            })
            self.assertEqual("check", act["action"], act)
            run = run_state(tmp)
            self.assertEqual(
                [
                    ("fix_open", "the b.py fallback needs a design decision", ["F-1"], "FIX-1"),
                    ("fix_open", "c.py — outside the files", ["F-1"], "FIX-1"),
                ],
                [(lo["kind"], lo["what"], lo["items"], lo["fix_group"]) for lo in run["leftovers"]],
            )
            code, act = done(tmp, act, {"exit_code": 0})
            self.assertEqual("done", act["action"], act)
            self.assertEqual(2, len(run_state(tmp)["leftovers"]))
            report = Path(act["report"]).read_text(encoding="utf-8")
            self.assertIn("修正の作業者が直せなかった: c.py", report)
            self.assertIn("- 最後の検査: 通った（修正の段で直らなかった 2 件を残りにした）", report)

    def test_nothing_to_fix_goes_to_the_report_without_the_final_check(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            reach_fix_approval(
                tmp,
                [make_finding("F-1", files=["b.py"])],
                [{"id": "F-1", "decision": "skip", "reason": "later"}],
            )
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "nothing_to_fix")
            self.assertEqual((0, "done"), (code, act["action"]), act)
            run = run_state(tmp)
            self.assertNotIn("final_checking", [to for _, _, to in moves(tmp)])
            self.assertIsNone(run["final_check"])
            self.assertIn("- 最後の検査: 流していない（修正の段が無かった）", Path(act["report"]).read_text(encoding="utf-8"))

    def test_a_run_saved_waiting_for_the_old_fix_check_takes_the_final_check(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = reach_final_check(tmp)
            run = state.load_state(tmp)
            run["state"] = "fixing"
            run["subphase"] = "check"
            run["pending_action"] = None
            run["history"] = [h for h in run["history"] if h["to"] != "final_checking"]
            state.save_state(tmp, run)
            code, act = run_cli("next", "--root", str(tmp))
            self.assertEqual((0, "check"), (code, act["action"]), act)
            self.assertEqual(["npm test -- b", *WHOLE, *E2E], act["commands"])
            self.assertEqual("final_checking", run_state(tmp)["state"])


class LockedCarryOverFinalCheckTests(UnlockTestBase):
    """In a locked run, a carry-over laid by a failed locked case settles only when that case passes in the final
    check, which runs the case's acceptance test file (the carry-over's commands are only its PBI's checks)."""

    def reach_final_check(self, fixed: bool) -> tuple[dict, dict]:
        """PBI-3 builds R2-1 wrong (9, not 10): the last wave check carries it over as CO-1 on the case alone. The fix
        worker of CO-1 fixes app.json when fixed. Returns the final check and the run's carry-over."""
        acceptance_command = {"id": "c1", "text": "run one acceptance file", "source": "Makefile:3", "kind": "acceptance", "command": self.command}
        act = approve_plan(self.root, unlock_plan(), [UNIT, acceptance_command])
        (self.root / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.root / ".git" / "info" / "attributes").write_bytes(b"*.json text eol=lf\n")
        self.write("app.py", STUB)
        finish_stage(self.root, act, {"R1": r1(), "R2": R2})
        launch = pass_review(self.root, drive(self.root))
        self.assertEqual(["PBI-3"], pbis_of(launch))
        self.write("app.py", BUILT)
        self.write_json("app.json", {**IMPL, "test_R1[R1-2]": 4, "test_R2[R2-1]": 9})
        self.done(launch, {"status": "ok"})
        review = drive(self.root)
        self.assertEqual(("launch", "reviewer"), (review["action"], review["role"]), review)
        (co,) = saved(self.root)["carry_overs"]
        self.assertEqual((["PBI-3"], [PBI_CHECK]), (co["pbis"], co["commands"]))
        ask = self.done(review, {"schema": "atelier.findings/1", "findings": []})
        self.assertEqual(("ask", "fix_approval"), (ask["action"], ask["kind"]), ask)
        code, fix = run_cli("answer", "--root", str(self.root), "--choice", "approve")
        self.assertEqual((0, "launch"), (code, fix["action"]), fix)
        if fixed:
            self.write_json("app.json", {**IMPL, "test_R1[R1-2]": 4})
        final = self.done(fix, {"status": "ok"})
        self.assertEqual(("check", "final_checking"), (final["action"], saved(self.root)["state"]), final)
        return final, co

    def test_the_final_check_runs_the_carried_cases_and_the_carry_over_is_fixed_when_they_pass(self) -> None:
        final, _ = self.reach_final_check(fixed=True)
        self.assertEqual([R1_FILE, R2_FILE], [g["file"] for g in final["final"]["groups"]])
        self.assertEqual({"R1-1": ["CO-1"], "R1-2": ["CO-1"], "R2-1": ["CO-1"]}, final["final"]["cases"])
        self.assertEqual([g["command"] for g in final["final"]["groups"]], final["commands"][:2])
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        self.assertTrue(out["complete"], out)
        run = saved(self.root)
        self.assertEqual(("passed", []), (run["final_check"]["result"], run["leftovers"]))
        self.assertEqual("fixed", run["carry_overs"][0]["status"])

    def test_a_carried_case_that_still_fails_keeps_the_carry_over_open_and_is_left_over(self) -> None:
        self.reach_final_check(fixed=False)
        out = drive(self.root)
        self.assertEqual("done", out["action"], out)
        self.assertFalse(out["complete"], out)
        run = saved(self.root)
        # The PBI's own check and the plan's check pass; the case does not.
        self.assertEqual("failed", run["final_check"]["result"])
        self.assertEqual([], run["final_check"]["failed"])
        self.assertEqual({"R1-1": "passed", "R1-2": "passed", "R2-1": "assertion"}, run["final_check"]["cases"])
        self.assertEqual("open", run["carry_overs"][0]["status"])
        (left,) = run["leftovers"]
        self.assertEqual(("check_failure", "R2-1", ["CO-1"]), (left["kind"], left["case"], left["items"]))
        report = (self.root / "docs" / "pbi" / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("最後の検査で受け入れのケース R2-1 が通らなかった（assertion）（元の件: CO-1）", report)

    def test_a_failed_check_whose_result_names_only_the_acceptance_commands_is_left_over(self) -> None:
        final, _ = self.reach_final_check(fixed=True)
        runs = [g["command"] for g in final["final"]["groups"]]
        for command in runs:
            subprocess.run(command, shell=True, cwd=str(self.root), capture_output=True, check=False)
        # The cases pass, but the result has no row for the PBI's check or the plan's, and the check's own exit code
        # says something failed: that stands for the commands with no row.
        out = self.done(final, {"exit_code": 1, "output": "a check failed", "commands": [{"command": c, "exit_code": 0} for c in runs]})
        self.assertEqual("done", out["action"], out)
        run = saved(self.root)
        self.assertEqual("failed", run["final_check"]["result"])
        self.assertEqual({"R1-1": "passed", "R1-2": "passed", "R2-1": "passed"}, run["final_check"]["cases"])
        self.assertEqual("open", run["carry_overs"][0]["status"])
        (left,) = run["leftovers"]
        self.assertEqual(("check_failure", None, 1), (left["kind"], left["command"], left["exit_code"]))
        self.assertEqual(final["commands"], left["commands"])


def fix_run(**fields) -> dict:
    run = state.new_run_state(Path("."), Path("goal.md"), None)
    run["approvals"]["spec"] = True
    run["approvals"]["plan"] = True
    run["counters"]["review_count"] = 1
    run.update(fields)
    return run


class NeverRuleTests(unittest.TestCase):
    def test_a_second_entry_into_the_fix_stage_is_refused(self) -> None:
        run = fix_run(state="fixing")
        run["approvals"]["fix_list"] = True
        run["history"] = [
            {"from": "awaiting_fix_approval", "event": "fix_list_approved", "to": "fixing"},
            {"from": "fixing", "event": "fixes_built", "to": "awaiting_fix_approval"},
            {"from": "awaiting_fix_approval", "event": "fix_list_approved", "to": "fixing"},
        ]
        with self.assertRaisesRegex(flow.InvariantError, "never SecondFixStage"):
            flow.check_invariants(run)

        counted = fix_run(state="fixing")
        counted["approvals"]["fix_list"] = True
        counted["counters"]["fix_stages"] = 2
        with self.assertRaisesRegex(flow.InvariantError, "never SecondFixStage"):
            flow.check_invariants(counted)

    def test_the_fix_list_cannot_be_approved_a_second_time(self) -> None:
        run = fix_run(state="awaiting_fix_approval")
        self.assertEqual("fixing", flow.apply_event(run, "fix_list_approved"))
        self.assertEqual(1, run["counters"]["fix_stages"])
        run["state"] = "awaiting_fix_approval"
        with self.assertRaises(flow.TransitionError):
            flow.apply_event(run, "fix_list_approved")
        self.assertEqual(1, run["counters"]["fix_stages"])

    def test_going_back_to_the_fix_stage_after_the_final_check_is_refused(self) -> None:
        run = fix_run(state="fixing")
        run["approvals"]["fix_list"] = True
        run["history"] = [
            {"from": "awaiting_fix_approval", "event": "fix_list_approved", "to": "fixing"},
            {"from": "fixing", "event": "fixes_built", "to": "final_checking"},
        ]
        run["counters"]["fix_stages"] = 1
        with self.assertRaisesRegex(flow.InvariantError, "never FixAfterFinalCheck"):
            flow.check_invariants(run)

        recorded = fix_run(state="fixing", final_check={"result": "failed"})
        recorded["approvals"]["fix_list"] = True
        with self.assertRaisesRegex(flow.InvariantError, "never FixAfterFinalCheck"):
            flow.check_invariants(recorded)

    def test_the_final_check_goes_only_to_the_report(self) -> None:
        self.assertEqual(
            [("final_checking", "final_check_done", "reporting")],
            [(t.source, t.event, t.target) for t in flow.TRANSITIONS if t.source == "final_checking"],
        )
        self.assertEqual([], [t for t in flow.TRANSITIONS if t.target == "fixing" and t.source != "awaiting_fix_approval"])
        self.assertEqual([], [t for t in flow.TRANSITIONS if t.event == "fix_check_passed"])
        self.assertLess(flow.STATES.index("fixing"), flow.STATES.index("final_checking"))
        self.assertLess(flow.STATES.index("final_checking"), flow.STATES.index("reporting"))

    def test_a_resume_into_the_fix_stage_is_the_same_fix_stage(self) -> None:
        run = fix_run(state="awaiting_fix_approval")
        flow.apply_event(run, "fix_list_approved")
        flow.apply_event(run, "budget_exceeded")
        self.assertEqual("asking_budget", run["state"])
        self.assertEqual("fixing", flow.apply_event(run, "resume"))
        flow.check_invariants(run)
        self.assertEqual("final_checking", flow.apply_event(run, "fixes_built"))
        self.assertEqual("reporting", flow.apply_event(run, "final_check_done"))

    def test_leftover_kinds_are_checked(self) -> None:
        run = fix_run()
        entry = state.add_leftover(run, "check_failure", "x failed", command="x", output="a\nb")
        self.assertEqual("LO-1", entry["id"])
        with self.assertRaises(state.StateError):
            state.add_leftover(run, "review", "not a leftover kind")


if __name__ == "__main__":
    unittest.main()
