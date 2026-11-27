"""A worker's stop and the plan-return question, driven through the CLI (Seam 1).

A stop holds its PBI and the PBIs that depend on it, and the other PBIs of the wave are built. Once the wave has
nothing left to launch, the run asks one question: fix the plan, or go on with the stop as an objection. Going on
keeps the stop as a plan return, sends the stopped PBI to the wave's check as it was built and lets the held PBIs go
on. Fixing the plan launches the planner's revise with the stop in its request and the passed PBIs frozen; in an
acceptance-first run the acceptance PBIs of the revised requirements are built again, checked Red and reviewed, and
the tests are locked again. Every answer is counted.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import acceptance, actions, locks, state  # noqa: E402
from tests import test_wave_locks  # noqa: E402
from tests.test_acceptance_red import drive, pass_review, pbis_of, saved  # noqa: E402
from tests.test_conductor import make_valid_plan, reach_building, run_cli, write_json, write_result_for  # noqa: E402
from tests.test_worker_checks import ticket_of  # noqa: E402
# The test case class is not imported by name, so its tests do not run again from this module.
from tests.test_wave_locks import BUILT, RED, TEST_FILE, name, outcomes  # noqa: E402

STOP = {"exception": 2, "reason": "R1-1 and R1-3 contradict each other"}
LABEL = "鍵つきのケースどうしが矛盾する"
# PBI-1 and PBI-2 share box.py, so PBI-2 starts after PBI-1 in the same wave. PBI-3 and PBI-4 depend on PBI-1 (PBI-4
# through PBI-3); PBI-5 depends only on PBI-2.
WAVE = [
    {"id": "PBI-1", "title": "Read the box", "depends_on": [], "targets": ["box.py"], "checks": ["check-1"]},
    {"id": "PBI-2", "title": "Size the box", "depends_on": [], "targets": ["box.py"], "checks": ["check-2"]},
    {"id": "PBI-3", "title": "Show the box", "depends_on": ["PBI-1"], "targets": ["view.py"], "checks": ["check-3"]},
    {"id": "PBI-4", "title": "Wire the box", "depends_on": ["PBI-3"], "targets": ["wire.py"], "checks": ["check-4"]},
    {"id": "PBI-5", "title": "Log the box", "depends_on": ["PBI-2"], "targets": ["log.py"], "checks": ["check-5"]},
]
HELD = ["PBI-1", "PBI-3", "PBI-4"]
# PBI-2 needs PBI-1, which passes in the first wave; PBI-3 needs PBI-2.
CHAIN = [
    {"id": "PBI-1", "title": "Read the box", "depends_on": [], "targets": ["box.py"], "checks": ["check-1"]},
    {"id": "PBI-2", "title": "Show the box", "depends_on": ["PBI-1"], "targets": ["view.py"], "checks": ["check-2"]},
    {"id": "PBI-3", "title": "Wire the box", "depends_on": ["PBI-2"], "targets": ["wire.py"], "checks": ["check-3"]},
]


def done(root: Path, act: dict, payload: dict) -> dict:
    write_result_for(act, payload)
    code, nxt = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0, nxt
    return nxt


def answer(root: Path, *args: str) -> dict:
    code, nxt = run_cli("answer", "--root", str(root), *args)
    assert code == 0, nxt
    return nxt


def held(run: dict) -> dict[str, str | None]:
    return {p["id"]: p.get("held_reason") for p in run["pbis"] if p.get("status") == "held"}


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8").rstrip()


class StopQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)

    def stop_the_wave(self) -> dict:
        """PBI-1 returns a stop; PBI-2, which does not depend on it, is launched next; then the run asks."""
        act = reach_building(self.tmp, pbis=WAVE)
        self.assertEqual(("launch", "PBI-1"), (act["action"], act["pbi"]))
        nxt = done(self.tmp, act, {"status": "ok", "stop": STOP})
        self.assertEqual(("launch", "worker", "PBI-2"), (nxt["action"], nxt["role"], nxt["pbi"]))
        run = state.load_state(self.tmp)
        self.assertEqual("building", run["state"])
        self.assertEqual({pid: state.STOP_HOLD for pid in HELD}, held(run))
        self.assertEqual({"PBI-2": "building", "PBI-5": "pending"}, {p: state.pbi_statuses(run)[p] for p in ("PBI-2", "PBI-5")})
        return done(self.tmp, nxt, {"status": "ok"})

    def test_a_stop_holds_its_pbi_and_its_dependents_and_asks_once_the_wave_is_built(self) -> None:
        ask = self.stop_the_wave()
        self.assertEqual(("ask", "plan_return"), (ask["action"], ask["kind"]))
        self.assertEqual(
            [("replan", "計画を直す"), ("continue", "異議として続ける（振り返りに回す）")],
            [(c["id"], c["label"]) for c in ask["choices"]],
        )
        question = Path(ask["question"]).read_text(encoding="utf-8")
        for text in (f"PBI-1（Read the box）: 例外 2（{LABEL}） — {STOP['reason']}", "止まっている PBI: PBI-1、PBI-3、PBI-4"):
            self.assertIn(text, question)
        run = state.load_state(self.tmp)
        self.assertEqual("asking_plan_return", run["state"])
        # The wave's check has not run yet: PBI-2 waits for it, the held PBIs wait for the answer.
        self.assertEqual("built", state.get_pbi(run, "PBI-2")["status"])
        self.assertEqual(STOP, state.get_pbi(run, "PBI-1")["stop"])
        self.assertEqual(0, run["counters"]["plan_returns"])
        self.assertEqual([], run["plan_returns"])
        self.assertIn("作業者の stop で止めた", state.HELD_REASON_LABELS[state.STOP_HOLD])

    def test_going_on_checks_the_stopped_pbi_lets_its_dependents_go_on_and_counts_each_answer(self) -> None:
        self.stop_the_wave()
        check = answer(self.tmp, "--choice", "continue")
        self.assertEqual(["check-1", "check-2", "python -m unittest"], check["commands"])
        run = state.load_state(self.tmp)
        self.assertEqual("checking", run["state"])
        self.assertEqual({"from": "asking_plan_return", "event": "plan_return_continue", "to": "checking"}, run["history"][-1])
        self.assertEqual({}, held(run))
        self.assertEqual(("built", "PR-1"), (state.get_pbi(run, "PBI-1")["status"], state.get_pbi(run, "PBI-1")["plan_return"]))
        self.assertEqual(["pending", "pending"], [state.get_pbi(run, p)["status"] for p in ("PBI-3", "PBI-4")])
        (record,) = run["plan_returns"]
        self.assertEqual(
            {
                "id": "PR-1",
                "kind": "plan_return",
                "decision": "continue",
                "stops": [{"pbi": "PBI-1", "title": "Read the box", "exception": 2, "label": LABEL, "reason": STOP["reason"]}],
                "objections": [],
                "held_pbis": HELD,
                "note": "",
            },
            record,
        )
        self.assertEqual(1, run["counters"]["plan_returns"])

        # The wave passes; PBI-3, which depended on the stopped PBI, is built with PBI-5. It stops too.
        batch = done(self.tmp, check, {"exit_code": 0})
        self.assertEqual(["PBI-3", "PBI-5"], pbis_of(batch))
        write_result_for(batch["launches"][0], {"status": "ok", "stop": {"exception": 3, "reason": "no box driver"}})
        write_result_for(batch["launches"][1], {"status": "ok"})
        code, ask = run_cli("done", "--root", str(self.tmp), "--launch-id", batch["launch_id"])
        self.assertEqual((0, "plan_return"), (code, ask.get("kind")), ask)
        self.assertEqual({"PBI-3": state.STOP_HOLD, "PBI-4": state.STOP_HOLD}, held(state.load_state(self.tmp)))
        self.assertIn("これまで 1 回", Path(ask["question"]).read_text(encoding="utf-8"))
        check = answer(self.tmp, "--choice", "continue")
        self.assertEqual(["check-3", "check-5", "python -m unittest"], check["commands"])
        run = state.load_state(self.tmp)
        self.assertEqual(2, run["counters"]["plan_returns"])
        self.assertEqual([("PR-1", "PBI-1"), ("PR-2", "PBI-3")], [(r["id"], r["stops"][0]["pbi"]) for r in run["plan_returns"]])

        report = actions._write_report(self.tmp, run).read_text(encoding="utf-8")
        self.assertIn("## 途中の戻り", report)
        self.assertIn(f"- PR-1（異議として続ける）: PBI-1 例外 2（{LABEL}） — {STOP['reason']}", report)
        self.assertIn("- PR-2（異議として続ける）: PBI-3 例外 3（前提の外のものが無い） — no box driver", report)

    def test_fixing_the_plan_launches_the_planner_with_the_stop_and_keeps_the_passed_pbis(self) -> None:
        act = reach_building(self.tmp, pbis=CHAIN)
        act = done(self.tmp, done(self.tmp, act, {"status": "ok"}), {"exit_code": 0})
        self.assertEqual("PBI-2", act["pbi"])
        stop = {"exception": 1, "reason": "box.read() returns a list, not a dict"}
        objection = {"case": "-", "what": "the view needs a dict", "why": "box.read() gives a list", "state": "red", "interpretation": "kept the dict"}
        ask = done(self.tmp, act, {"status": "ok", "stop": stop, "objections": [objection]})
        self.assertEqual("plan_return", ask["kind"])
        self.assertIn("止まっている PBI: PBI-2、PBI-3", Path(ask["question"]).read_text(encoding="utf-8"))

        reply = write_json(self.tmp / "reply.json", {"choice": "replan", "text": "keep box.read() as it is"})
        planner = answer(self.tmp, "--reply", str(reply))
        self.assertEqual(("launch", "planner"), (planner["action"], planner["role"]))
        prompt = Path(planner["prompt"]).read_text(encoding="utf-8")
        for text in (
            "Mode: revise",
            "Passed PBIs: PBI-1",
            f"The worker on PBI-2 (Show the box) returned a stop: exception 1 (公開の口の前提が崩れた). Reason: {stop['reason']}",
            "Objection of PBI-2 on -: the view needs a dict (box.read() gives a list)",
            "The run held these PBIs while it asked the user: PBI-2, PBI-3.",
            "User note: keep box.read() as it is",
            "Change only the PBIs that the public interface and the scenarios behind this stop concern",
            "The passed PBIs cannot change.",
        ):
            self.assertIn(text, prompt)
        run = state.load_state(self.tmp)
        self.assertEqual(("planning", False), (run["state"], run["approvals"]["plan"]))
        self.assertEqual({"from": "asking_plan_return", "event": "plan_return_replan", "to": "planning"}, run["history"][-1])
        self.assertEqual(1, run["counters"]["plan_returns"])
        (record,) = run["plan_returns"]
        self.assertEqual(("PR-1", "replan", ["PBI-2", "PBI-3"]), (record["id"], record["decision"], record["held_pbis"]))
        self.assertEqual(("plan_return", ["PBI-2"]), (run["approvals"]["revisions"][-1]["action"], run["approvals"]["revisions"][-1]["changed_pbis"]))
        self.assertNotIn("plan_return_relock", run)

        # The passed PBI cannot change, as in any re-plan.
        changed = make_valid_plan(CHAIN)
        changed["pbis"][0]["targets"] = ["box.py", "extra.py"]
        write_result_for(planner, changed)
        code, out = run_cli("done", "--root", str(self.tmp), "--launch-id", planner["launch_id"])
        self.assertEqual(2, code)
        self.assertIn("合格済みの PBI-1", out["error"])

        fixed = make_valid_plan(CHAIN)
        fixed["pbis"][1]["goal"] = "Show the box's list"
        ask = done(self.tmp, planner, fixed)
        self.assertEqual("plan_approval", ask["kind"])
        act = answer(self.tmp, "--choice", "approve")
        self.assertEqual(("launch", "worker", "PBI-2"), (act["action"], act["role"], act["pbi"]))
        run = state.load_state(self.tmp)
        # A run that does not put acceptance first goes straight back to building.
        self.assertFalse(run["acceptance_first"])
        self.assertEqual("building", run["state"])
        self.assertFalse({"building_acceptance", "checking_red", "reviewing_acceptance"} & {h["to"] for h in run["history"]})
        self.assertEqual("passed", state.get_pbi(run, "PBI-1")["status"])
        self.assertEqual("Show the box's list", state.get_pbi(run, "PBI-2")["goal"])
        self.assertEqual({}, held(run))

    def test_another_choice_is_refused_and_the_question_stays(self) -> None:
        self.stop_the_wave()
        code, out = run_cli("answer", "--root", str(self.tmp), "--choice", "resume")
        self.assertEqual(2, code)
        self.assertIn("invalid choice", out["error"])
        self.assertEqual("asking_plan_return", state.load_state(self.tmp)["state"])

    def test_each_exception_is_asked_with_its_number_and_label(self) -> None:
        for exc, label in state.STOP_EXCEPTION_LABELS.items():
            with self.subTest(exception=exc):
                self.setUp()
                act = reach_building(self.tmp, pbis=CHAIN[:1])
                ask = done(self.tmp, act, {"status": "ok", "stop": {"exception": exc, "reason": f"reason {exc}"}})
                self.assertEqual("plan_return", ask["kind"])
                self.assertIn(f"- PBI-1（Read the box）: 例外 {exc}（{label}） — reason {exc}", Path(ask["question"]).read_text(encoding="utf-8"))

    def test_two_stops_in_one_wave_are_one_question_and_one_answer(self) -> None:
        apart = [{**pbi, "depends_on": []} for pbi in CHAIN]
        batch = reach_building(self.tmp, pbis=apart)
        self.assertEqual(["PBI-1", "PBI-2", "PBI-3"], pbis_of(batch))
        write_result_for(batch["launches"][0], {"status": "ok", "stop": {"exception": 1, "reason": "first"}})
        write_result_for(batch["launches"][1], {"status": "ok"})
        write_result_for(batch["launches"][2], {"status": "ok", "stop": {"exception": 4, "reason": "second"}})
        code, ask = run_cli("done", "--root", str(self.tmp), "--launch-id", batch["launch_id"])
        self.assertEqual((0, "plan_return"), (code, ask.get("kind")), ask)
        question = Path(ask["question"]).read_text(encoding="utf-8")
        self.assertIn("例外 1（公開の口の前提が崩れた） — first", question)
        self.assertIn("例外 4（安全の問題） — second", question)
        self.assertIn("止まっている PBI: PBI-1、PBI-3", question)
        check = answer(self.tmp, "--choice", "continue")
        self.assertEqual(["check-1", "check-2", "check-3", "python -m unittest"], check["commands"])
        run = state.load_state(self.tmp)
        (record,) = run["plan_returns"]
        self.assertEqual(["PBI-1", "PBI-3"], [stop["pbi"] for stop in record["stops"]])
        self.assertEqual(1, run["counters"]["plan_returns"])


class WorkerPromptTests(unittest.TestCase):
    def test_the_worker_is_told_that_a_stop_asks_the_user_one_question(self) -> None:
        worker = (SKILLS_DIR / "prompts" / "worker.md").read_text(encoding="utf-8")
        self.assertIn("asks the user one question", worker)
        self.assertIn("fix the plan, or go on with the stop as an objection", worker)
        self.assertIn("A stop holds this PBI and the PBIs that depend on it", worker)
        self.assertNotIn("does not ask a question for them", worker)


class HoldForStopTests(unittest.TestCase):
    def test_the_indirect_dependents_are_held_and_an_earlier_hold_keeps_its_reason(self) -> None:
        run = {
            "pbis": [
                {"id": "PBI-1", "status": "built"},
                {"id": "PBI-2", "status": "pending", "depends_on": ["PBI-1"]},
                {"id": "PBI-3", "status": "held", "held_reason": "stale_dependency", "depends_on": ["PBI-2"]},
                {"id": "PBI-4", "status": "pending", "depends_on": ["PBI-3"]},
                {"id": "PBI-5", "status": "built", "depends_on": []},
            ],
        }
        self.assertEqual(["PBI-1", "PBI-2", "PBI-4"], state.hold_for_stop(run, "PBI-1"))
        by_id = {p["id"]: p for p in run["pbis"]}
        self.assertEqual(("held", "built", "PBI-1"), (by_id["PBI-1"]["status"], by_id["PBI-1"]["held_from"], by_id["PBI-4"]["held_stop"]))
        self.assertEqual("stale_dependency", by_id["PBI-3"]["held_reason"])
        self.assertEqual("built", by_id["PBI-5"]["status"])
        self.assertEqual(["PBI-1"], [p["id"] for p in state.stopped_pbis(run)])
        self.assertEqual(["PBI-1", "PBI-2", "PBI-4"], state.release_stop_hold(run, "PR-1"))
        self.assertEqual(
            [("PBI-1", "built"), ("PBI-2", "pending"), ("PBI-3", "held"), ("PBI-4", "pending"), ("PBI-5", "built")],
            [(p["id"], p["status"]) for p in run["pbis"]],
        )
        self.assertEqual(("PR-1", None), (by_id["PBI-1"]["plan_return"], by_id["PBI-2"].get("held_reason")))


def scenario(rid: str, total: int = 3) -> dict:
    return {"id": rid, "text": f"要件 {rid}", "mode": "normal", "gherkin": "Scenario Outline: x", "cases": [
        {"id": f"{rid}-1", "values": {"a": 1, "b": 2, "sum": total}, "pbi": "PBI-9", "needs": []},
    ]}


class ReopenRevisedTests(unittest.TestCase):
    def test_only_the_changed_requirements_go_back_and_an_unchanged_unresolved_one_stays_out_of_the_lock(self) -> None:
        before = [scenario("R1"), {**scenario("R2"), "no_lock": True}, scenario("R3")]
        files = {"R1": "acc/r1.json", "R2": "acc/r2.json", "R3": "acc/r3.json"}
        run = {
            "acceptance_first": True,
            "pbis": [
                {"id": "PBI-1", "kind": "acceptance", "status": "passed", "requirements": ["R1", "R2"],
                 "targets": [files["R1"], files["R2"]], "acceptance_files": {r: files[r] for r in ("R1", "R2")}},
                {"id": "PBI-2", "kind": "acceptance", "status": "passed", "requirements": ["R3"],
                 "targets": [files["R3"]], "acceptance_files": {"R3": files["R3"]}},
                {"id": "PBI-9", "status": "pending"},
            ],
            # The re-plan changed R1's expected sum and wrote every scenario anew.
            "scenarios": [scenario("R1", total=4), scenario("R2"), scenario("R3")],
            "red": {
                "R1": {"status": "passed", "failures": 1, "reasons": [], "history": [{"passed": True}]},
                "R2": {"status": "unresolved", "failures": 2, "reasons": ["x"], "history": []},
                "R3": {"status": "passed", "failures": 0, "reasons": [], "history": []},
            },
            "acceptance_review": {
                "round": 1,
                "files": {files["R1"]: {"status": "ok", "requirements": ["R1"]}, files["R3"]: {"status": "ok", "requirements": ["R3"]}},
                "history": [],
                "fixes": {"R1": 1},
            },
        }
        acceptance.keep_unchanged_marks(run, before)
        out = acceptance.reopen_revised(run, before)
        self.assertEqual({"revised": ["R1"], "reopened": ["R1"], "rebuilt": ["PBI-1"]}, out)
        pbis = {p["id"]: p for p in run["pbis"]}
        self.assertEqual(("pending", ["R1"]), (pbis["PBI-1"]["status"], pbis["PBI-1"]["revised"]))
        self.assertEqual("passed", pbis["PBI-2"]["status"])
        self.assertEqual(
            {"R1": ("pending", 0), "R2": ("unresolved", 2), "R3": ("passed", 0)},
            {rid: (e["status"], e["failures"]) for rid, e in run["red"].items()},
        )
        self.assertEqual([files["R3"]], list(run["acceptance_review"]["files"]))
        self.assertEqual({}, run["acceptance_review"]["fixes"])
        self.assertEqual(["R1", "R3"], locks.locked_requirements(run))


class AcceptanceFirstPlanReturnTests(unittest.TestCase):
    """The locked run of test_wave_locks: PBI-3 holds R1-1 and R1-2 and is the first PBI after the lock; PBI-4, which
    holds R1-3, needs PBI-3."""

    # Its helpers, without running its tests again here.
    setUp = test_wave_locks.WaveLockTests.setUp
    lock_and_launch = test_wave_locks.WaveLockTests.lock_and_launch
    build = test_wave_locks.WaveLockTests.build
    run_wave = test_wave_locks.WaveLockTests.run_wave

    def test_fixing_the_plan_builds_the_revised_acceptance_tests_again_and_locks_them_again(self) -> None:
        launch = self.lock_and_launch()
        before = dict(saved(self.root)["lock"])
        copy = Path(before["outside"]) / locks.COPY_NAME
        old_copy = copy.read_bytes()

        # PBI-3's worker builds app.py, then stops: PBI-3 and PBI-4 are held and nothing else is left to launch.
        (self.root / "app.py").write_bytes(BUILT.encode("utf-8"))
        ask = done(self.root, launch, {"status": "ok", "stop": STOP})
        self.assertEqual(("ask", "plan_return"), (ask["action"], ask["kind"]))
        self.assertIn("受け入れの PBI を作り直し", Path(ask["question"]).read_text(encoding="utf-8"))
        self.assertEqual({"PBI-3": state.STOP_HOLD, "PBI-4": state.STOP_HOLD}, held(saved(self.root)))

        planner = answer(self.root, "--choice", "replan")
        self.assertEqual(("launch", "planner"), (planner["action"], planner["role"]))
        prompt = Path(planner["prompt"]).read_text(encoding="utf-8")
        self.assertIn("Passed PBIs: PBI-1, PBI-2", prompt)
        self.assertIn("the acceptance tests are then locked again", prompt)
        # The lock stays on while the planner works; it comes off when the new plan is approved.
        self.assertEqual(before["commit"], saved(self.root)["lock"]["commit"])

        # The planner revises R1: R1-3 gets new values and R1-4 is added for PBI-4.
        revised = test_wave_locks.wave_plan()
        cases = revised["scenarios"][0]["cases"]
        cases[2]["values"] = {"a": 3, "b": 3, "sum": 6}
        cases.append({"id": "R1-4", "values": {"a": 4, "b": 2, "sum": 6}, "pbi": "PBI-4", "needs": []})
        self.assertEqual("plan_approval", done(self.root, planner, revised)["kind"])
        stage = answer(self.root, "--choice", "approve")
        run = saved(self.root)
        self.assertEqual("building_acceptance", run["state"])
        self.assertIsNone(run["lock"])
        self.assertEqual([(before["commit"], "PR-1")], [(h["commit"], h["plan_return"]) for h in run["lock_history"]])
        # Only the acceptance PBI of the revised requirement is built again; the stub PBI stays passed.
        self.assertEqual(["PBI-1"], pbis_of(stage))
        self.assertEqual("passed", state.get_pbi(run, "PBI-2")["status"])
        ticket = ticket_of(stage)
        self.assertEqual(["R1"], ticket["revised"])
        self.assertEqual(["R1-1", "R1-2", "R1-3", "R1-4"], [c["id"] for c in ticket["scenarios"][0]["cases"]])
        self.assertEqual(
            {"revised": ["R1"], "reopened": ["R1"], "rebuilt": ["PBI-1"], "lock_before": before["commit"]},
            run["plan_returns"][0]["relock"],
        )

        # The rebuilt tests. PBI-3's cases already pass with its code in the work tree; PBI-4's cases fail.
        rewritten = {"cases": [*RED["cases"], {"name": name("R1-4"), "message": "assert 0 == 6"}]}
        (self.root / TEST_FILE).write_text(json.dumps(rewritten), encoding="utf-8")
        red = done(self.root, stage, {"status": "ok", "acceptance_files": {"R1": TEST_FILE}})
        self.assertEqual(("check", "checking_red"), (red["action"], saved(self.root)["state"]))
        (self.root / "outcomes.json").write_text(json.dumps(outcomes()), encoding="utf-8")
        review = drive(self.root)
        run = saved(self.root)
        self.assertEqual(("reviewing_acceptance", "passed"), (run["state"], run["red"]["R1"]["status"]))
        self.assertIsNone(run["lock"])

        nxt = pass_review(self.root, review)
        run = saved(self.root)
        self.assertEqual(("building", ["PBI-3"]), (run["state"], pbis_of(nxt)))
        after_return = [h["to"] for h in run["history"][[h.get("event") for h in run["history"]].index("plan_return_replan"):]]
        self.assertEqual(
            ["planning", "awaiting_plan_approval", "building_acceptance", "checking_red", "reviewing_acceptance", "building"],
            after_return,
        )

        # Locked again: a new list, a new lock commit on top of the old one, a new copy outside, a new hash.
        lock = run["lock"]
        self.assertNotEqual(before["commit"], lock["commit"])
        self.assertEqual(lock["commit"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(before["commit"], git(self.root, "rev-parse", f"{lock['commit']}^"))
        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual(["R1-1", "R1-2", "R1-3", "R1-4"], listed["cases"])
        self.assertEqual("PBI-4", listed["assignments"]["R1-4"])
        self.assertEqual((self.root / locks.LIST_PATH).read_bytes(), copy.read_bytes())
        self.assertNotEqual(old_copy, copy.read_bytes())
        record = json.loads(locks.hash_path(run["run_id"]).read_text(encoding="utf-8"))
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        self.assertEqual((digest, lock["commit"]), (record["sha256"], record["commit"]))
        self.assertEqual(digest, lock["sha256"])
        self.assertNotEqual(before["sha256"], digest)
        self.assertEqual(lock["commit"] + "\n", (Path(lock["outside"]) / locks.COMMIT_NAME).read_text(encoding="ascii"))
        self.assertIsNone(locks.copy_problem(self.root, run))

        # The wave check goes by the new lock: PBI-3 passes on R1-1 and R1-2, and PBI-4 is next.
        check = self.build(nxt)
        self.assertEqual(["R1-1", "R1-2", "R1-3", "R1-4"], check["wave"]["cases"])
        after = self.run_wave(outcomes())
        self.assertEqual(["PBI-4"], pbis_of(after))
        self.assertEqual("passed", state.get_pbi(saved(self.root), "PBI-3")["status"])

    def test_a_broken_plan_re_planned_in_a_locked_run_is_locked_again_too(self) -> None:
        launch = self.lock_and_launch()
        before = saved(self.root)["lock"]["commit"]
        broken = {"status": "plan_broken", "plan_broken": True, "reason": "R1 needs a fourth case", "needed_files": []}
        self.assertEqual("plan_broken", done(self.root, launch, broken)["kind"])
        planner = answer(self.root, "--choice", "replan")
        revised = test_wave_locks.wave_plan()
        revised["scenarios"][0]["cases"].append({"id": "R1-4", "values": {"a": 4, "b": 2, "sum": 6}, "pbi": "PBI-4", "needs": []})
        self.assertEqual("plan_approval", done(self.root, planner, revised)["kind"])
        stage = answer(self.root, "--choice", "approve")
        run = saved(self.root)
        self.assertEqual(("building_acceptance", None), (run["state"], run["lock"]))
        self.assertEqual([(before, None)], [(h["commit"], h["plan_return"]) for h in run["lock_history"]])
        self.assertEqual(["PBI-1"], pbis_of(stage))
        # No plan-return question was asked, so none is counted.
        self.assertEqual((0, []), (run["counters"]["plan_returns"], run["plan_returns"]))


if __name__ == "__main__":
    unittest.main()
