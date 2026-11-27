"""The retrospective list: its kinds and fields, the one list after the final review and the one triage over it.

Seam 1: the run is driven through the conductor's CLI with fake role results. A run with review findings, a worker's
objection, an unresolved failed check and a plan return gets one list after the review, written as JSON outside the
work tree (under the git common dir, or the run folder outside git), with its place and counts in run.json. The
planner's triage reads that list and must sort every item once; the fix approval page shows each item's fields.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import plan, retro, state  # noqa: E402
from tests.test_conductor import make_finding, reach_building, run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo, reach_building_with  # noqa: E402

OBJECTION = {
    "case": "R1-2",
    "what": "the total should round down",
    "why": "the example rounds the other way",
    "state": "red",
    "interpretation": "round half up, as the spec's text says",
}
STOP = {"exception": 3, "reason": "the exporter needs a schema"}
# The wave check fails only on the plan's check, which no PBI owns: it is left unresolved and the wave passes.
UNOWNED_FAILURE = {
    "exit_code": 1,
    "commands": [{"command": "python -m unittest", "exit_code": 1}],
    "output": "$ python -m unittest\nimport error in the harness\n",
}
FINDINGS = [
    make_finding("F-1", confidence=95, files=["app.py"]),
    make_finding("F-2", severity="minor", confidence=50, files=["app.py"]),
    make_finding("F-3", severity="minor", confidence=90, kind="style", files=["app.py"]),
]


def done(root: Path, act: dict, payload: dict) -> tuple[int, dict]:
    write_result_for(act, payload)
    return run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])


def reach_review_with_every_kind(root: Path, act: dict) -> dict:
    """From PBI-1's launch: an objection and a stop (gone on with), then a wave check no PBI owns; returns the review."""
    code, ask = done(root, act, {"status": "ok", "objections": [OBJECTION], "stop": STOP})
    assert code == 0 and ask["kind"] == "plan_return", ask
    code, act = run_cli("answer", "--root", str(root), "--choice", "continue")
    assert code == 0 and act["action"] == "check", act
    code, act = done(root, act, UNOWNED_FAILURE)
    assert code == 0 and act["role"] == "reviewer", act
    return act


def review_then_e2e(root: Path, review: dict, findings: list[dict]) -> tuple[int, dict]:
    """Return the review's findings, then pass the E2E that runs after it; returns the next action (the triage)."""
    code, act = done(root, review, {"schema": "atelier.findings/1", "findings": findings})
    assert code == 0 and act["action"] == "check", act
    return done(root, act, {"exit_code": 0})


def git_common_dir(root: Path) -> Path:
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=True)
    common = Path(out.stdout.strip())
    return (common if common.is_absolute() else root / common).resolve()


class KindsAndFieldsTests(unittest.TestCase):
    def test_the_kinds_and_the_fields_are_defined(self) -> None:
        self.assertEqual(
            (
                "review", "objection", "unresolved", "plan_return", "requirement_gap", "unit_gap", "e2e_failure",
                "unlock_request", "light_review",
            ),
            retro.KINDS,
        )
        self.assertEqual(
            (
                "id", "kind", "what", "why", "tried", "state", "interpretation", "options", "recommendation", "affects",
                "source",
            ),
            retro.FIELDS,
        )
        for kind in retro.KINDS:
            with self.subTest(kind=kind):
                item = retro.make_item(
                    kind, "X-1", what="w", why="y", tried="t", state="s", interpretation="i", options=["a", "b"],
                    recommendation="a", affects_pbis=["PBI-1"], affects_files=["app.py"], source={"record": "r"},
                )
                self.assertEqual(list(retro.FIELDS), list(item)[: len(retro.FIELDS)])
                self.assertEqual({"pbis": ["PBI-1"], "files": ["app.py"]}, item["affects"])
                self.assertEqual([], retro.validate_items([item]))
                self.assertTrue(retro.KIND_LABELS[kind])

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(retro.RetroError):
            retro.make_item("mutation", "X-1", what="w", why="y")
        good = retro.make_item("review", "X-1", what="w", why="y")
        self.assertTrue(retro.validate_items([{**good, "kind": "mutation"}]))
        self.assertTrue(retro.validate_items([good, dict(good)]))  # the same id twice
        for field in ("what", "options", "affects", "source"):
            with self.subTest(field=field):
                self.assertTrue(retro.validate_items([{k: v for k, v in good.items() if k != field}]))

    def test_the_confidence_and_style_filter_applies_to_review_items_only(self) -> None:
        run = {"pbis": [{"id": "PBI-1", "targets": ["app.py"]}]}
        items = [retro._review_item(run, f) for f in FINDINGS]
        items += [
            retro.make_item(kind, f"{kind}-1", what="w", why="y")
            for kind in retro.KINDS if kind != "review"
        ]
        kept, dropped = retro.split(items)
        self.assertEqual(["F-1", *[f"{k}-1" for k in retro.KINDS if k != "review"]], [i["id"] for i in kept])
        self.assertEqual([("F-2", "confidence"), ("F-3", "style")], [(i["id"], i["dropped_by"]) for i in dropped])
        f1 = kept[0]
        self.assertEqual(("review", "correctness", 95, "app.py"), (f1["kind"], f1["category"], f1["confidence"], f1["file"]))
        self.assertEqual({"pbis": ["PBI-1"], "files": ["app.py"]}, f1["affects"])
        self.assertEqual("最後の見直し", f1["source"]["record"])

    def test_ids_are_unique_across_kinds(self) -> None:
        run = {"pbis": [], "plan_returns": [{"id": "PR-1", "decision": "continue", "stops": []}]}
        kept, _ = retro.collect(run, [make_finding("PR-1")])
        self.assertEqual(["PR-1", "plan_return:PR-1"], [i["id"] for i in kept])


class OneListAfterTheReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        init_git_repo(self.root)
        act = reach_building_with(self.root, [{"id": "PBI-1", "title": "Export", "targets": ["app.py"]}])
        self.review = reach_review_with_every_kind(self.root, act)

    def test_the_records_become_one_list_outside_the_work_tree(self) -> None:
        code, tri = review_then_e2e(self.root, self.review, FINDINGS)
        self.assertEqual(0, code, tri)
        self.assertEqual(("launch", "planner"), (tri["action"], tri["role"]))
        run = state.load_state(self.root)
        place = Path(run["retro"]["path"])
        self.assertEqual(git_common_dir(self.root) / "atelier" / run["run_id"] / "retro.json", place)
        self.assertEqual({"path": str(place), "count": 4, "dropped": 2, "kinds": {
            "review": 1, "objection": 1, "unresolved": 1, "plan_return": 1,
        }}, run["retro"])
        # Not in the work tree: git sees no new file, and the run folder has no copy.
        status = subprocess.run(["git", "-C", str(self.root), "status", "--porcelain"], capture_output=True, text=True, check=True)
        self.assertNotIn("retro", status.stdout)
        self.assertFalse((state.run_dir(self.root) / "retro.json").exists())

        body = json.loads(place.read_text(encoding="utf-8"))
        self.assertEqual(retro.SCHEMA, body["schema"])
        self.assertEqual(
            [("F-1", "review"), ("OB-1", "objection"), ("UR-1", "unresolved"), ("PR-1", "plan_return")],
            [(i["id"], i["kind"]) for i in body["items"]],
        )
        self.assertEqual([("F-2", "confidence"), ("F-3", "style")], [(i["id"], i["dropped_by"]) for i in body["dropped"]])
        self.assertEqual([], retro.validate_items(body["items"] + body["dropped"]))
        by_id = {i["id"]: i for i in body["items"]}
        objection = by_id["OB-1"]
        self.assertNotIn("confidence", objection)
        self.assertEqual(OBJECTION["interpretation"], objection["interpretation"])
        self.assertIn(OBJECTION["what"], objection["what"])
        self.assertEqual({"pbis": ["PBI-1"], "files": ["app.py"]}, objection["affects"])
        self.assertEqual(OBJECTION, objection["source"]["objection"])
        unresolved = by_id["UR-1"]
        self.assertIn("python -m unittest", unresolved["what"])
        self.assertEqual("check_failed", unresolved["source"]["unresolved"]["kind"])
        plan_return = by_id["PR-1"]
        self.assertIn("例外 3", plan_return["what"])
        self.assertEqual(STOP["reason"], plan_return["why"])
        self.assertIn("異議として続ける", plan_return["tried"])
        self.assertEqual(["PBI-1"], plan_return["affects"]["pbis"])
        for item in body["items"]:
            for field in ("what", "why", "tried", "state", "interpretation", "recommendation"):
                self.assertTrue(item[field].strip(), (item["id"], field))
            self.assertTrue(item["options"], item["id"])

    def test_triage_reads_the_list_and_must_sort_every_item_once(self) -> None:
        _, tri = review_then_e2e(self.root, self.review, FINDINGS)
        prompt = Path(tri["prompt"]).read_text(encoding="utf-8")
        place = state.load_state(self.root)["retro"]["path"]
        self.assertIn("Mode: triage", prompt)
        self.assertIn(f"Retrospective list: {Path(place).resolve()}", prompt)
        self.assertNotIn("Findings:", prompt)
        every = [
            {"id": "F-1", "decision": "fix", "reason": "a defect against the spec"},
            {"id": "OB-1", "decision": "ask", "reason": "changes how R1 is read"},
            {"id": "UR-1", "decision": "fix", "reason": "the harness import is clear"},
            {"id": "PR-1", "decision": "skip", "reason": "went on as an objection"},
        ]
        for items in (every[:3], every + [every[0]], every + [{"id": "F-2", "decision": "fix", "reason": "r"}]):
            with self.subTest(ids=[i["id"] for i in items]):
                code, out = done(self.root, tri, {"schema": "atelier.triage/1", "items": items})
                self.assertEqual(2, code, out)
                self.assertEqual("triaging", state.load_state(self.root)["state"])
        code, ask = done(self.root, tri, {"schema": "atelier.triage/1", "items": every})
        self.assertEqual(0, code, ask)
        self.assertEqual(("ask", "fix_approval"), (ask["action"], ask["kind"]))
        run = state.load_state(self.root)
        self.assertEqual(["F-1", "UR-1"], [f["id"] for f in run["fix_list"]])
        self.assertEqual(["OB-1"], [f["id"] for f in run["triage_ask_items"]])
        self.assertEqual(["PR-1"], [f["id"] for f in run["triage_skipped"]])
        self.assertEqual(("review", "unresolved"), tuple(f["kind"] for f in run["fix_list"]))

        draft = Path(ask["draft"]).read_text(encoding="utf-8")
        section = draft.split("## 直す一覧", 1)[1].split("\n## ", 1)[0]
        for text in (
            "**F-1: fix F-1**", "| 種類 | 見直しの指摘 |", "| なぜ | why F-1 |", "| 影響する PBI | PBI-1 |",
            "| 仕分けの理由 | a defect against the spec |",
            "**UR-1: ", "| 種類 | 未解決 |", "| 何が | wave の検査のコマンド python -m unittest |",
            "| 今の状態 | 未解決（持ち主の PBI が決まらない） |",
        ):
            self.assertIn(text, section)
        for label in ("種類", "何が", "なぜ", "試したこと", "今の状態", "仮の解釈", "選択肢", "推奨", "影響する PBI"):
            self.assertEqual(2, section.count(f"| {label} |"), label)
        asks = draft.split("## 聞く件の中身", 1)[1].split("\n## ", 1)[0]
        self.assertIn("| 種類 | 異議 |", asks)
        self.assertIn(f"| 仮の解釈 | {OBJECTION['interpretation']} |", asks)
        self.assertIn("OB-1（異議）", draft.split("## 決めること", 1)[1])
        candidates = draft.split("## 見送りの候補", 1)[1]
        for row in ("| PR-1 | 見送ると仕分けた（途中の戻り） |", "| F-2 | 確信度が 80 未満 |", "| F-3 | 書き方だけの指摘 |"):
            self.assertIn(row, candidates)

        # Approving builds fix groups from the files the items affect.
        code, act = run_cli("answer", "--root", str(self.root), "--choice", "approve")
        self.assertEqual(0, code, act)
        self.assertEqual("fixing", state.load_state(self.root)["state"])


class OutsideGitTests(unittest.TestCase):
    def test_outside_git_the_list_goes_to_the_run_folder_and_an_objection_alone_is_triaged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = reach_building(root)
            code, act = done(root, act, {"status": "ok", "objections": [{**OBJECTION, "state": "green"}]})
            self.assertEqual("check", act["action"], act)
            code, act = done(root, act, {"exit_code": 0})
            code, tri = review_then_e2e(root, act, [])
            self.assertEqual(("launch", "planner"), (tri["action"], tri["role"]))
            run = state.load_state(root)
            self.assertEqual((state.run_dir(root) / "retro.json").resolve(), Path(run["retro"]["path"]))
            self.assertEqual(1, run["retro"]["count"])
            self.assertEqual(["OB-1"], [i["id"] for i in retro.load_items(run)])

    def test_an_empty_list_without_carry_overs_skips_the_fix_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = reach_building(root)
            code, act = done(root, act, {"status": "ok"})
            code, act = done(root, act, {"exit_code": 0})
            code, act = done(root, act, {"schema": "atelier.findings/1", "findings": []})
            self.assertEqual(0, code, act)
            self.assertEqual("check", act["action"], act)
            run = state.load_state(root)
            self.assertEqual("running_e2e", run["state"])
            self.assertEqual((0, 0), (run["retro"]["count"], run["retro"]["dropped"]))
            code, act = done(root, act, {"exit_code": 0})
            self.assertEqual(0, code, act)
            self.assertEqual("done", act["action"], act)
            # No planner is launched to sort an empty list, and the user is not asked.
            moves = [(h["from"], h["event"], h["to"]) for h in state.load_state(root)["history"]]
            self.assertEqual(
                [
                    ("running_e2e", "e2e_done", "triaging"),
                    ("triaging", "triaged", "awaiting_fix_approval"),
                    ("awaiting_fix_approval", "nothing_to_fix", "reporting"),
                    ("reporting", "report_written", "done"),
                ],
                moves[-4:],
            )


class LightReviewTests(unittest.TestCase):
    def test_a_light_requirement_and_one_left_unresolved_are_on_the_list_after_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = reach_building_with(root, [{"id": "PBI-1", "title": "Export", "targets": ["app.py"]}])
            # The plan's scenarios: R1 is light, R2 is normal but its acceptance test was left unresolved (run as light).
            run = state.load_state(root)
            run["scenarios"] = [
                {"id": "R1", "text": "依存を更新する", "mode": "light",
                 "cases": [{"id": "R1-1", "test_name": "test_updates_deps", "pbi": "PBI-1"}]},
                {"id": "R2", "text": "合計を出す", "mode": "normal",
                 "cases": [{"id": "R2-1", "values": {"a": 1}, "pbi": "PBI-1"}]},
                {"id": "R3", "text": "書き出す", "mode": "normal",
                 "cases": [{"id": "R3-1", "values": {}, "pbi": "PBI-1"}]},
            ]
            run["red"] = {"R1": {"status": "passed"}, "R2": {"status": "unresolved"}, "R3": {"status": "passed"}}
            run["unresolved"] = [{
                "id": "UR-1", "kind": "red_check", "requirement": "R2", "pbis": [], "cases": ["R2-1"],
                "as_light_pbis": ["PBI-1"], "failures": 2, "reason": "the test fails before the call",
            }]
            run["pbis"][0]["as_light"] = ["R2"]
            state.save_state(root, run)
            code, act = done(root, act, {"status": "ok"})
            self.assertEqual("check", act["action"], act)
            code, act = done(root, act, {"exit_code": 0})
            self.assertEqual("reviewer", act["role"], act)
            code, tri = review_then_e2e(root, act, [])
            self.assertEqual(("launch", "planner"), (tri["action"], tri["role"]), tri)

            run = state.load_state(root)
            body = json.loads(Path(run["retro"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual([], retro.validate_items(body["items"]))
            self.assertEqual(
                [("UR-1", "unresolved"), ("LR-1", "light_review"), ("LR-2", "light_review")],
                [(i["id"], i["kind"]) for i in body["items"]],
            )
            self.assertEqual(2, run["retro"]["kinds"]["light_review"])
            light, left = body["items"][1:]
            self.assertEqual(("R1", "light", False), (light["requirement"], light["mode"], light["unresolved"]))
            self.assertIn("R1", light["what"])
            self.assertIn("light", light["what"])
            self.assertIn("light", light["why"])
            self.assertEqual((["R1-1"], ["test_updates_deps"]), (light["cases"], light["test_names"]))
            self.assertEqual({"pbis": ["PBI-1"], "files": ["app.py"]}, light["affects"])
            self.assertEqual({"record": "run.json の scenarios の R1", "requirement": "R1", "mode": "light",
                              "unresolved": []}, light["source"])
            self.assertEqual(("R2", "normal", True), (left["requirement"], left["mode"], left["unresolved"]))
            self.assertIn("未解決", left["why"])
            self.assertIn("the test fails before the call", left["why"])
            self.assertEqual(["R2-1"], left["cases"])
            self.assertEqual(["UR-1"], left["source"]["unresolved"])
            self.assertEqual({"pbis": ["PBI-1"], "files": ["app.py"]}, left["affects"])
            for item in (light, left):
                for field in ("what", "why", "tried", "state", "recommendation"):
                    self.assertTrue(item[field].strip(), (item["id"], field))


class PlannerPromptTests(unittest.TestCase):
    def test_the_triage_section_names_the_list_and_how_each_kind_is_sorted(self) -> None:
        text = (SKILLS_DIR / "prompts" / "planner.md").read_text(encoding="utf-8")
        section = text.split("## Mode: `triage`", 1)[1]
        for phrase in (
            "retrospective list", "`Retrospective list`", "atelier.retro/1", "exactly once",
            "change how a requirement is read", "A requirement gap", "A unit gap", "Equivalent",
        ):
            self.assertIn(phrase, section)
        for kind in retro.KINDS:
            self.assertIn(f"`{kind}`", section)

    def test_the_prompt_names_the_list_and_keeps_the_older_argument(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for key in ("retro_path", "findings_path"):
                with self.subTest(key=key):
                    out = plan.planner_prompt(
                        root, "triage", spec_path=root / "spec.md", facts_path=root / "facts.json",
                        result_path=root / "r.json", out_path=root / f"{key}.md", **{key: root / "retro.json"},
                    )
                    self.assertIn(f"Retrospective list: {(root / 'retro.json').resolve()}", out.read_text(encoding="utf-8"))

    def test_fix_groups_use_the_files_an_item_affects(self) -> None:
        a = retro.make_item("objection", "OB-1", what="w", why="y", affects_files=["a.py"])
        b = retro.make_item("unresolved", "UR-1", what="w", why="y", affects_files=["a.py", "b.py"])
        c = retro.make_item("plan_return", "PR-1", what="w", why="y", affects_files=["c.py"])
        self.assertEqual([["OB-1", "UR-1"], ["PR-1"]], [[i["id"] for i in g] for g in plan.fix_groups([a, b, c])])


if __name__ == "__main__":
    unittest.main()
