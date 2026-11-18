"""User decisions that the confidence filter, triage and G1 must not override."""

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

from atelier import guards, state  # noqa: E402
from tests.test_conductor import make_finding, reach_reviewing, run_cli, write_json, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo, reach_building_with, write_agents_and_template  # noqa: E402


def review_and_triage(tmp: Path, findings: list[dict], triage: list[dict] | None) -> dict:
    act = reach_reviewing(tmp)
    write_result_for(act, {"schema": "atelier.findings/1", "findings": findings})
    _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    if triage is not None:
        assert act["role"] == "planner", act
        write_result_for(act, {"schema": "atelier.triage/1", "items": triage})
        _, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    return act


class FixApprovalIncludesUserDecisionsTests(unittest.TestCase):
    def test_only_dropped_findings_still_reach_fix_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = review_and_triage(tmp, [make_finding("R2", confidence=75, files=["b.py"])], None)
            self.assertEqual(act["kind"], "fix_approval")
            self.assertIn("| R2 |", Path(act["draft"]).read_text(encoding="utf-8"))
            reply = write_json(tmp / "reply.json", {"choice": "approve", "include_ids": ["R2"]})
            code, out = run_cli("answer", "--root", str(tmp), "--reply", str(reply))
            self.assertEqual(code, 0, out)
            run = state.load_state(tmp)
            self.assertEqual(run["state"], "fixing")
            self.assertEqual([(f["id"], f["file"]) for f in run["fix_list"]], [("R2", "b.py")])

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

    def test_approved_symlink_keeps_its_own_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            (root / "docs" / "b.md").write_text("b", encoding="utf-8")
            link = root / "docs" / "current.md"
            try:
                link.symlink_to("spec.md")
            except OSError:
                self.skipTest("symlinks are not available")
            before = guards.snapshot(root, allowed_paths=["docs/pbi/"])
            link.unlink()
            link.symlink_to("b.md")
            approved = [guards.approved_edit(root, "docs/current.md")]
            self.assertEqual(approved[0], {"path": "docs/current.md", "digest": "link:b.md"})
            self.assertFalse([v for v in guards.compare(root, before, approved) if v["rule"] == "G1"])

    def test_edit_approved_while_an_external_role_runs_passes_g1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            routes = {f"L{i}": [{"runner": "codex", "model": "gpt-test", "effort": "low"}] for i in range(1, 6)}
            agents, tmpl = write_agents_and_template(root, routes=routes)
            pbis = [{"id": "PBI-1", "title": "Ext", "depends_on": [], "level": "L1", "reason": "small", "files": ["app.py"]}]
            act = reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)
            self.assertEqual(act["runner"], "codex")
            (root / "glossary.md").write_text("old", encoding="utf-8")
            helper = root / "caller_edits.py"
            helper.write_text(
                "\n".join([
                    "import json, subprocess, sys",
                    "from pathlib import Path",
                    f"Path({str(root / 'glossary.md')!r}).write_text('new')",
                    f"subprocess.run([sys.executable, {str(SKILLS_DIR / 'conductor.py')!r}, 'approve-edit', '--root', {str(root)!r},"
                    " '--path', 'glossary.md', '--reason', 'user approved'], check=True, stdout=subprocess.DEVNULL)",
                    f"Path({act['result']!r}).write_text(json.dumps({{'status': 'ok'}}))",
                    "Path(sys.argv[sys.argv.index('-o') + 1]).write_text('done')",
                    "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))",
                    "",
                ]),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"ATELIER_CODEX_CMD": json.dumps([sys.executable, str(helper)])}):
                code, out = run_cli("run", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, out)
            self.assertNotEqual(out.get("kind"), "guard", out)
            self.assertEqual([e["path"] for e in state.load_state(root)["approved_edits"]], ["glossary.md"])

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
