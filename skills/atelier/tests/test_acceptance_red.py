"""The acceptance stage and its Red check, driven through the CLI with a fake acceptance command.

The fake command stands in for a test runner: it reads the acceptance test file the fake worker wrote (a JSON list
of testcases and how each ended) and writes the JUnit XML the run reads.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import actions, flow, state  # noqa: E402
from tests.test_acceptance_toggle import UNIT  # noqa: E402
from tests.test_conductor import VALID_FACTS, init_workspace, run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo  # noqa: E402
from tests.test_integration import make_pbi, make_plan  # noqa: E402
from tests.test_worker_checks import ticket_of, tickets_of  # noqa: E402

FAKE_RUNNER = """import json, sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if spec.get("write_xml", True):
    rows = []
    for case in spec["cases"]:
        outcome = case.get("outcome", "failure")
        attrs = "".join(" %s=%s" % (key, quoteattr(case[key])) for key in ("message", "type") if case.get(key))
        inner = "" if outcome == "passed" else "<%s%s/>" % (outcome, attrs)
        rows.append('<testcase classname="acc" name=%s>%s</testcase>' % (quoteattr(case["name"]), inner))
    junit = Path(sys.argv[2])
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.write_text('<?xml version="1.0"?><testsuites><testsuite name="acc">' + "".join(rows) + "</testsuite></testsuites>", encoding="utf-8")
raise SystemExit(1)
"""
COMMAND = f'"{sys.executable}" fake_red.py {{file}} {{junit}}'
ACCEPTANCE = {"id": "c1", "text": "run one acceptance file", "source": "Makefile:3", "kind": "acceptance", "command": COMMAND}
GHERKIN = "Scenario Outline: add\n  When add <a> and <b>\n  Then the sum is <sum>\n  Examples:\n    | a | b | sum |\n    | 1 | 2 | 3 |"
# What each wrong way of failing leaves in the reasons.
BAD = {
    "R3": ({"cases": [{"name": "test_R3[R3-1]", "outcome": "passed"}, {"name": "test_R3[R3-2]", "message": "assert 1 == 3"}]}, "R3-1: 通った"),
    "R4": (
        {"cases": [{"name": "test_R4[R4-1]", "outcome": "error", "message": "ImportError: cannot import name 'add'"}, {"name": "test_R4[R4-2]", "message": "assert 0"}]},
        "R4-1: error で落ちた",
    ),
    "R5": (
        {"cases": [{"name": "test_R5[R5-1]", "message": "ModuleNotFoundError: No module named 'app'"}, {"name": "test_R5[R5-2]", "message": "assert 0"}]},
        "R5-1: 落ち方が assertion でも未実装でもない（ModuleNotFoundError）",
    ),
    "R6": ({"cases": [{"name": "test_R6[R6-1]", "message": "assert 0"}]}, "足りない id: R6-2"),
    "R7": ({"cases": [*[{"name": f"test_R7[R7-{n}]", "message": "assert 0"} for n in (1, 2)], {"name": "test_other[X9]", "message": "assert 0"}]}, "余計な id"),
    "R8": ({"write_xml": False, "cases": []}, "JUnit XML が書き出されていない"),
    "R9": ({"cases": [{"name": "test_R9[R9-1]", "outcome": "skipped"}, {"name": "test_R9[R9-2]", "message": "assert 0"}]}, "R9-1: 飛ばされた"),
}


def good(rid: str) -> dict:
    """Every case fails the right way: one with a plain assert, one from a stub that is not implemented yet."""
    return {"cases": [
        {"name": f"test_{rid}[{rid}-1]", "message": "assert 1 == 3"},
        {"name": f"test_{rid}[{rid}-2]", "type": "NotImplementedError", "message": "NotImplementedError"},
    ]}


def red_plan(normal: list[str]) -> dict:
    """Acceptance PBI-1 writes every requirement's tests; stub PBI-2 places app.add; build PBI-3 has no
    dependencies (so it sits in the first wave too); build PBI-4 needs PBI-1 and PBI-2. R2 is light-no-red."""
    files = [f"acceptance/{rid}.json" for rid in [*normal, "R2"]]
    acc = {**make_pbi("PBI-1", targets=files), "kind": "acceptance"}
    stub = {**make_pbi("PBI-2", targets=["app.py"]), "kind": "stub"}
    other = make_pbi("PBI-3", targets=["other.py"])
    build = make_pbi("PBI-4", depends_on=["PBI-1", "PBI-2"], targets=["app.py"])
    obj = make_plan([acc, stub, other, build])
    obj["interfaces"] = [{"module": "app", "name": "add"}]
    obj["scenarios"] = [
        {
            "id": rid,
            "text": f"要件 {rid}",
            "mode": "normal",
            "gherkin": GHERKIN,
            "cases": [{"id": f"{rid}-{n}", "values": {"a": n, "b": 2, "sum": n + 2}, "pbi": "PBI-4", "needs": []} for n in (1, 2)],
            "spare_count": 0,
        }
        for rid in normal
    ] + [{"id": "R2", "text": "依存を更新する", "mode": "light-no-red", "cases": [{"id": "R2-1", "values": {}, "pbi": "PBI-3"}], "spare_count": 0}]
    return obj


def start_to_planner(root: Path, commands: list[dict]) -> dict:
    init_git_repo(root)
    (root / "fake_red.py").write_text(FAKE_RUNNER, encoding="utf-8")
    _, goal, spec = init_workspace(root)
    code, out = run_cli("start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec))
    assert code == 0, out
    _, act = run_cli("next", "--root", str(root))
    write_result_for(act, {**VALID_FACTS, "commands": commands})
    code, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0, act
    code, act = run_cli("answer", "--root", str(root), "--choice", "skip")
    assert code == 0 and act["role"] == "planner", act
    return act


def approve_plan(root: Path, obj: dict, commands: list[dict] | None = None) -> dict:
    act = start_to_planner(root, commands if commands is not None else [UNIT, ACCEPTANCE])
    write_result_for(act, obj)
    code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0 and ask["kind"] == "plan_approval", ask
    code, act = run_cli("answer", "--root", str(root), "--choice", "approve")
    assert code == 0, act
    return act


def write_tests(root: Path, specs: dict[str, dict]) -> None:
    """What the fake acceptance worker writes: one acceptance test file per requirement."""
    for rid, spec in specs.items():
        path = root / "acceptance" / f"{rid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(spec), encoding="utf-8")


def finish_stage(root: Path, act: dict, specs: dict[str, dict]) -> dict:
    """Return the stage's launches with the tests written, and give back the next action (the Red check)."""
    write_tests(root, specs)
    files = {rid: f"acceptance/{rid}.json" for rid in specs}
    for launch in act["launches"] if act["action"] == "launches" else [act]:
        payload = {"status": "ok", "acceptance_files": files} if launch["pbi"] == "PBI-1" else {"status": "ok"}
        write_result_for(launch, payload)
    code, nxt = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0, nxt
    return nxt


def drive(root: Path) -> dict:
    code, out = run_cli("drive", "--root", str(root))
    assert code == 0, out
    return out["next"]


def saved(root: Path) -> dict:
    return state.load_state(root)


def red_records(root: Path) -> list[dict]:
    path = root / "docs" / "pbi" / "record.jsonl"
    return [e for e in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) if e.get("stage") == "red"]


def pass_review(root: Path, review: dict, verdicts: dict[str, tuple[str, str]] | None = None) -> dict:
    """Answer the planner's acceptance review: ok for every file its prompt asks about, except the verdicts given.
    Returns the next action."""
    assert review["role"] == "planner", review
    field = "Acceptance test files: "
    prompt = Path(review["prompt"]).read_text(encoding="utf-8")
    files = [item["file"] for item in json.loads(next(ln for ln in prompt.splitlines() if ln.startswith(field))[len(field):])]
    items = []
    for file in files:
        verdict, reason = (verdicts or {}).get(file, ("ok", ""))
        items.append({"file": file, "verdict": verdict, **({"reason": reason} if reason else {})})
    write_result_for(review, {"schema": "atelier.acceptance_review/1", "files": items})
    code, nxt = run_cli("done", "--root", str(root), "--launch-id", review["launch_id"])
    assert code == 0, nxt
    return nxt


def pbis_of(act: dict) -> list[str]:
    return [m["pbi"] for m in (act["launches"] if act["action"] == "launches" else [act])]


class JudgeGroupSharedFileTests(unittest.TestCase):
    def test_cases_of_a_requirement_outside_the_group_are_ignored(self) -> None:
        from atelier import acceptance

        cases = {"R1": ["R1-1"], "R2": ["R2-1"]}
        testcases = [
            {"name": "test_R1-1", "outcome": "failure", "type": "AssertionError", "message": "assert 1 == 2"},
            {"name": "test_R2-1", "outcome": "skipped", "type": "", "message": ""},
        ]
        verdicts = acceptance.judge_group({"file": "t.py", "requirements": ["R1"]}, cases, testcases, None)
        self.assertEqual([(v["requirement"], v["passed"]) for v in verdicts], [("R1", True)])

    def test_a_testcase_matching_no_plan_case_still_fails_the_group(self) -> None:
        from atelier import acceptance

        cases = {"R1": ["R1-1"], "R2": ["R2-1"]}
        testcases = [
            {"name": "test_R1-1", "outcome": "failure", "type": "AssertionError", "message": "assert 1 == 2"},
            {"name": "test_stray", "outcome": "passed", "type": "", "message": ""},
        ]
        verdicts = acceptance.judge_group({"file": "t.py", "requirements": ["R1"]}, cases, testcases, None)
        self.assertFalse(verdicts[0]["passed"])


class AcceptanceStageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_stage_builds_only_acceptance_and_stub_then_passes_red_and_builds(self) -> None:
        act = approve_plan(self.root, red_plan(["R1"]))
        self.assertEqual("building_acceptance", saved(self.root)["state"])
        # PBI-3 is in the first wave too, but only the acceptance and stub PBIs are built in this stage.
        self.assertEqual(["PBI-1", "PBI-2"], pbis_of(act))

        tickets = tickets_of(act)
        acc, stub = tickets["PBI-1"], tickets["PBI-2"]
        self.assertEqual("acceptance", acc["kind"])
        self.assertEqual(COMMAND, acc["acceptance_command"])
        self.assertEqual([{"module": "app", "name": "add"}], acc["interfaces"])
        normal, light = acc["scenarios"]
        self.assertEqual(("R1", "normal", GHERKIN), (normal["id"], normal["mode"], normal["gherkin"]))
        self.assertEqual({"id": "R1-1", "values": {"a": 1, "b": 2, "sum": 3}}, normal["cases"][0])
        self.assertEqual(("R2", [{"id": "R2-1", "test_name": "依存を更新する"}]), (light["id"], light["cases"]))
        self.assertNotIn("gherkin", light)
        self.assertEqual({"kind": "stub", "interfaces": [{"module": "app", "name": "add"}]}, {k: stub[k] for k in ("kind", "interfaces")})
        self.assertNotIn("scenarios", stub)
        prompt = Path(act["launches"][0]["prompt"]).read_text(encoding="utf-8")
        for text in ("## Acceptance and stub PBIs", "do not change any source file", "`acceptance_files`", "raise NotImplementedError"):
            self.assertIn(text, prompt)

        check = finish_stage(self.root, act, {"R1": good("R1"), "R2": {"cases": []}})
        self.assertEqual("check", check["action"])
        self.assertEqual("checking_red", saved(self.root)["state"])
        # One command per requirement file, with {file} and {junit} filled in; the light-no-red R2 is not run.
        junit = f"docs/pbi/results/{check['launch_id']}/1.xml"
        self.assertEqual([COMMAND.replace("{file}", "acceptance/R1.json").replace("{junit}", junit)], check["commands"])

        review = drive(self.root)
        self.assertEqual("reviewing_acceptance", saved(self.root)["state"])
        nxt = pass_review(self.root, review)
        run = saved(self.root)
        self.assertEqual("building", run["state"])
        self.assertEqual(["PBI-3", "PBI-4"], pbis_of(nxt))
        self.assertEqual(("passed", "skipped"), (run["red"]["R1"]["status"], run["red"]["R2"]["status"]))
        self.assertEqual({"PBI-1": "passed", "PBI-2": "passed"}, {p["id"]: p["status"] for p in run["pbis"][:2]})
        self.assertEqual([], run["unresolved"])
        (event,) = red_records(self.root)
        self.assertEqual(("PBI-1", "pass", [], True), (event["pbi"], event["result"], event["rules"], event["first_pass"]))
        self.assertIn("building_acceptance", [h["to"] for h in run["history"]])
        self.assertIn("checking_red", [h["to"] for h in run["history"]])

    def test_each_wrong_failure_fails_then_rebuilds_and_a_second_failure_is_unresolved(self) -> None:
        reqs = ["R1", *BAD]
        act = approve_plan(self.root, red_plan(reqs))
        check = finish_stage(self.root, act, {"R1": good("R1"), **{rid: spec for rid, (spec, _) in BAD.items()}, "R2": {"cases": []}})
        self.assertEqual(len(reqs), len(check["commands"]))

        # First Red check: every wrong way fails its requirement; the acceptance PBI is built again.
        rebuild = drive(self.root)
        run = saved(self.root)
        self.assertEqual("building_acceptance", run["state"])
        self.assertEqual(["PBI-1"], pbis_of(rebuild))
        # R1 passed, but its acceptance PBI is built again, so it is checked again too.
        self.assertEqual(("pending", [True]), (run["red"]["R1"]["status"], [h["passed"] for h in run["red"]["R1"]["history"]]))
        for rid, (_, reason) in BAD.items():
            with self.subTest(requirement=rid):
                entry = run["red"][rid]
                self.assertEqual(("failed", 1), (entry["status"], entry["failures"]))
                self.assertTrue(any(reason in r for r in entry["reasons"]), entry["reasons"])
        (event,) = red_records(self.root)
        self.assertEqual(("fail", ["red_check"]), (event["result"], event["rules"]))
        recorded = {v["requirement"]: v for v in event["red"]}
        self.assertTrue(recorded["R1"]["passed"])
        for rid, (_, reason) in BAD.items():
            self.assertTrue(any(reason in r for r in recorded[rid]["reasons"]), recorded[rid])
        ticket = ticket_of(rebuild)
        self.assertEqual(sorted(BAD), sorted(item["requirement"] for item in ticket["red_failure"]))

        # The rebuilt tests fail the right way, except R8's, which still writes no XML: R8 is left unresolved.
        write_tests(self.root, {rid: good(rid) for rid in reqs if rid != "R8"})
        write_result_for(rebuild, {"status": "ok"})
        code, check = run_cli("done", "--root", str(self.root), "--launch-id", rebuild["launch_id"])
        self.assertEqual((0, "check"), (code, check["action"]))
        nxt = pass_review(self.root, drive(self.root))
        run = saved(self.root)
        self.assertEqual("building", run["state"])
        self.assertEqual(["PBI-3", "PBI-4"], pbis_of(nxt))
        self.assertEqual({"R8": "unresolved", "R2": "skipped"}, {r: e["status"] for r, e in run["red"].items() if e["status"] != "passed"})
        (item,) = run["unresolved"]
        self.assertEqual(("red_check", "R8", ["PBI-1"], 2), (item["kind"], item["requirement"], item["pbis"], item["failures"]))
        self.assertIn("JUnit XML が書き出されていない", item["reason"])
        pbi4 = state.get_pbi(run, "PBI-4")
        self.assertEqual(["R8"], pbi4["as_light"])
        self.assertTrue(next(sc for sc in run["scenarios"] if sc["id"] == "R8")["no_lock"])
        self.assertNotIn("as_light", state.get_pbi(run, "PBI-3"))

        report = actions._write_report(self.root, run).read_text(encoding="utf-8")
        self.assertIn("## 未解決", report)
        self.assertIn("UR-1（要件 R8、受け入れの PBI PBI-1）", report)
        self.assertIn("- Red の確認: 合格 7 件、未解決 1 件、流さない（light-no-red） 1 件", report)

    def test_off_run_goes_straight_to_building(self) -> None:
        obj = make_plan([{**make_pbi("PBI-1"), "kind": "acceptance"}, make_pbi("PBI-2", targets=["b.py"])])
        act = approve_plan(self.root, obj, commands=[UNIT])
        run = saved(self.root)
        self.assertIs(False, run["acceptance_first"])
        self.assertEqual("building", run["state"])
        self.assertEqual(["PBI-1", "PBI-2"], pbis_of(act))
        self.assertFalse({"building_acceptance", "checking_red"} & {h["to"] for h in run["history"]})
        self.assertNotIn("scenarios", ticket_of(act["launches"][0]))

    def test_acceptance_stage_is_never_entered_without_the_toggle(self) -> None:
        for s in ("building_acceptance", "checking_red"):
            with self.subTest(state=s):
                run = state.new_run_state(Path("."), Path("goal.md"), None)
                run["state"] = s
                run["approvals"]["plan"] = True
                with self.assertRaises(flow.InvariantError):
                    flow.check_invariants(run)
                run["acceptance_first"] = True
                flow.check_invariants(run)

    def test_building_before_the_red_check_is_done_is_refused(self) -> None:
        run = state.new_run_state(Path("."), Path("goal.md"), None)
        run.update(state="building", acceptance_first=True, pbis=[{"id": "PBI-1", "kind": "acceptance", "status": "built"}])
        run["approvals"]["plan"] = True
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(run)

    def test_requirements_name_scenarios_of_one_acceptance_pbi(self) -> None:
        act = start_to_planner(self.root, [UNIT, ACCEPTANCE])
        obj = red_plan(["R1"])
        obj["pbis"][0]["requirements"] = ["R1", "R404"]
        obj["pbis"][1]["requirements"] = ["R1"]
        write_result_for(act, obj)
        code, out = run_cli("done", "--root", str(self.root), "--launch-id", act["launch_id"])
        self.assertEqual(2, code)
        self.assertIn("R404", out["error"])
        self.assertIn("PBI-2 の requirements は kind が acceptance の PBI にだけ書けます", out["error"])


if __name__ == "__main__":
    unittest.main()
