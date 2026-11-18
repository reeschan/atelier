"""User decisions that the confidence filter, triage and G1 must not override."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import guards, plan, state  # noqa: E402
from tests.test_conductor import make_finding, reach_reviewing, run_cli, write_json, write_result_for  # noqa: E402


def review_and_triage(tmp: Path, findings: list[dict], triage: list[dict] | None) -> dict:
    act = reach_reviewing(tmp)
    write_result_for(act, {"schema": "atelier.findings/1", "findings": findings})
    _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    if triage is not None:
        assert act["role"] == "planner", act
        write_result_for(act, {"schema": "atelier.triage/1", "items": triage})
        _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    return act


class SplitFindingsTests(unittest.TestCase):
    def test_held_findings_are_the_complement_of_kept(self) -> None:
        findings = [
            make_finding("R1", confidence=90),
            make_finding("R2", confidence=75),
            make_finding("R3", confidence=95, kind="style"),
        ]
        kept, held = plan.split_findings(findings)
        self.assertEqual([f["id"] for f in kept], ["R1"])
        self.assertEqual([f["id"] for f in held], ["R2", "R3"])
        self.assertEqual(plan.filter_findings(findings), kept)


class FixApprovalIncludesUserDecisionsTests(unittest.TestCase):
    def test_low_confidence_finding_the_user_includes_is_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = review_and_triage(
                tmp,
                [make_finding("R1", confidence=90, files=["a.py"]), make_finding("R2", confidence=75, files=["b.py"])],
                [{"id": "R1", "decision": "fix", "reason": "spec"}],
            )
            self.assertEqual(act["kind"], "fix_approval")
            self.assertTrue(any("R2" in line and "確度 75" in line for line in act["summary"]), act["summary"])

            reply = write_json(tmp / "reply.json", {"choice": "approve", "include_ids": ["R2"]})
            code, act = run_cli("answer", "--root", str(tmp), "--reply", str(reply))
            self.assertEqual(code, 0, act)
            run = state.load_state(tmp)
            self.assertEqual([f["id"] for f in run["fix_list"]], ["R1", "R2"])
            r2 = run["fix_list"][1]
            self.assertEqual((r2["decision"], r2["file"]), ("fix", "b.py"))
            self.assertEqual(run["state"], "fixing")

    def test_finding_triage_skipped_can_be_included(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = review_and_triage(
                tmp,
                [make_finding("R1", confidence=90)],
                [{"id": "R1", "decision": "skip", "reason": "acceptable"}],
            )
            self.assertTrue(any("R1（仕分けで見送り）" in line for line in act["summary"]), act["summary"])
            reply = write_json(tmp / "reply.json", {"choice": "approve", "include_ids": ["R1"]})
            run_cli("answer", "--root", str(tmp), "--reply", str(reply))
            run = state.load_state(tmp)
            self.assertEqual([f["id"] for f in run["fix_list"]], ["R1"])
            self.assertEqual(run["state"], "fixing")

    def test_only_held_findings_still_reach_fix_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = review_and_triage(tmp, [make_finding("R2", confidence=75)], None)
            self.assertEqual(act["kind"], "fix_approval")
            reply = write_json(tmp / "reply.json", {"choice": "approve", "include_ids": ["R2"]})
            run_cli("answer", "--root", str(tmp), "--reply", str(reply))
            self.assertEqual(state.load_state(tmp)["state"], "fixing")

    def test_unknown_or_contradicting_include_ids_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            review_and_triage(
                tmp,
                [make_finding("R1", confidence=90)],
                [{"id": "R1", "decision": "fix", "reason": "spec"}],
            )
            for body in ({"choice": "approve", "include_ids": ["R9"]}, {"choice": "nothing_to_fix", "include_ids": ["R1"]}):
                reply = write_json(tmp / "reply.json", body)
                code, out = run_cli("answer", "--root", str(tmp), "--reply", str(reply))
                self.assertNotEqual(code, 0, out)
            self.assertEqual(state.load_state(tmp)["state"], "awaiting_fix_approval")

    def test_no_findings_at_all_skips_fix_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = review_and_triage(tmp, [], None)
            self.assertEqual(act["action"], "check")
            self.assertEqual(state.load_state(tmp)["state"], "running_e2e")


class ApprovedEditTests(unittest.TestCase):
    def _repo(self, td: str) -> Path:
        root = Path(td)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "docs").mkdir()
        (root / "docs" / "spec.md").write_text("old", encoding="utf-8")
        return root

    def test_user_approved_edit_passes_g1_while_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            before = guards.snapshot(root, allowed_paths=["docs/pbi/"])
            (root / "docs" / "spec.md").write_text("new", encoding="utf-8")
            approved = [guards.approved_edit(root, "docs/spec.md")]
            self.assertEqual(approved[0]["path"], "docs/spec.md")
            self.assertFalse([v for v in guards.compare(root, before, approved) if v["rule"] == "G1"])

            (root / "docs" / "spec.md").write_text("changed again by the role", encoding="utf-8")
            self.assertTrue([v for v in guards.compare(root, before, approved) if v["rule"] == "G1"])

    def test_unapproved_edit_still_stops(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            before = guards.snapshot(root, allowed_paths=["docs/pbi/"])
            (root / "docs" / "spec.md").write_text("new", encoding="utf-8")
            self.assertTrue([v for v in guards.compare(root, before) if v["rule"] == "G1"])

    def test_approve_edit_command_records_and_refuses_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            act = reach_reviewing(Path(td))
            del act
            root = Path(td)
            (root / "glossary.md").write_text("term", encoding="utf-8")
            code, out = run_cli(
                "approve-edit", "--root", str(root), "--path", "spec.md", "--path", str(root / "glossary.md"),
                "--reason", "ユーザーが R4 を仕様に合わせると決めた",
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["approved_edits"], ["spec.md", "glossary.md"])
            edits = state.load_state(root)["approved_edits"]
            self.assertEqual({e["path"] for e in edits}, {"spec.md", "glossary.md"})
            self.assertTrue(all(e["reason"] and e["digest"] for e in edits))

            with tempfile.TemporaryDirectory() as other:
                outside = Path(other) / "x.md"
                outside.write_text("x", encoding="utf-8")
                code, out = run_cli("approve-edit", "--root", str(root), "--path", str(outside), "--reason", "r")
                self.assertNotEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
