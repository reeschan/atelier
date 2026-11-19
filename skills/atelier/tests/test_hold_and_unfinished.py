"""Holding a failed PBI lets the PBIs that depend on it go on; a run with unfinished PBIs is not reported as done."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import eol, state  # noqa: E402
from tests.test_conductor import reach_building, run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo, reach_building_with  # noqa: E402

# Two PBIs in the first wave, then a chain that needs both: the shape where the first PBI adds
# something that only the third one uses.
CHAIN = [
    {"id": "PBI-1", "title": "Add helper", "depends_on": [], "checks": ["check-1"]},
    {"id": "PBI-2", "title": "Add type", "depends_on": [], "checks": ["check-2"]},
    {"id": "PBI-3", "title": "Use both", "depends_on": ["PBI-1", "PBI-2"], "checks": ["check-3"]},
    {"id": "PBI-4", "title": "Wire up", "depends_on": ["PBI-3"], "checks": ["check-4"]},
    {"id": "PBI-5", "title": "Show it", "depends_on": ["PBI-4"], "checks": ["check-5"]},
]


def done(tmp: Path, act: dict, payload: dict) -> dict:
    write_result_for(act, payload)
    code, nxt = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0, nxt
    return nxt


def fail_first_wave_twice(tmp: Path) -> dict:
    """Build the first wave, fail its check, auto-fix once, fail again: the rebuild question."""
    act = reach_building(tmp, pbis=CHAIN)
    act = done(tmp, act, {"status": "ok"})
    act = done(tmp, act, {"status": "ok"})
    assert act["action"] == "check", act
    act = done(tmp, act, {"exit_code": 1, "output": "unused export: helper"})
    assert act["role"] == "worker", act
    act = done(tmp, act, {"status": "ok"})
    act = done(tmp, act, {"exit_code": 1, "output": "unused export: helper"})
    assert act["kind"] == "rebuild", act
    return act


class HoldContinuesDependentsTests(unittest.TestCase):
    def test_question_names_the_failed_and_waiting_pbis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ask = fail_first_wave_twice(tmp)
            self.assertEqual([c["id"] for c in ask["choices"]], ["retry", "retry_up", "hold", "hold_stop"])
            body = Path(ask["question"]).read_text(encoding="utf-8")
            self.assertIn("PBI-1（Add helper）", body)
            self.assertIn("PBI-3、PBI-4、PBI-5", body)
            self.assertIn("unused export: helper", body)

    def test_hold_builds_the_dependents_and_passes_the_held_pbis_with_them(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fail_first_wave_twice(tmp)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "hold")
            self.assertEqual(code, 0, act)
            self.assertEqual((act["action"], act["pbi"]), ("launch", "PBI-3"))
            run = state.load_state(tmp)
            by_id = {p["id"]: p for p in run["pbis"]}
            self.assertEqual(by_id["PBI-1"]["status"], "held")
            self.assertEqual(by_id["PBI-3"]["waived_deps"], ["PBI-1", "PBI-2"])

            act = done(tmp, act, {"status": "ok"})
            self.assertEqual(act["action"], "check")
            for cmd in ("check-1", "check-2", "check-3"):
                self.assertIn(cmd, act["commands"])

            act = done(tmp, act, {"exit_code": 0})
            self.assertEqual(act["pbi"], "PBI-4")
            statuses = state.pbi_statuses(state.load_state(tmp))
            self.assertEqual([statuses[f"PBI-{i}"] for i in (1, 2, 3)], ["passed"] * 3)

            act = done(tmp, act, {"status": "ok"})
            self.assertNotIn("check-1", act["commands"])

    def test_held_pbis_stay_held_when_the_dependent_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fail_first_wave_twice(tmp)
            _, act = run_cli("answer", "--root", str(tmp), "--choice", "hold")
            act = done(tmp, act, {"status": "ok"})
            done(tmp, act, {"exit_code": 1, "failed_pbis": ["PBI-3"]})
            statuses = state.pbi_statuses(state.load_state(tmp))
            self.assertEqual((statuses["PBI-1"], statuses["PBI-3"]), ("held", "failed"))

    def test_held_pbis_held_twice_in_a_row_are_checked_again_together(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fail_first_wave_twice(tmp)
            _, act = run_cli("answer", "--root", str(tmp), "--choice", "hold")
            act = done(tmp, act, {"status": "ok"})
            act = done(tmp, act, {"exit_code": 1, "failed_pbis": ["PBI-3"]})
            act = done(tmp, act, {"status": "ok"})
            ask = done(tmp, act, {"exit_code": 1, "failed_pbis": ["PBI-3"]})
            self.assertEqual(ask["kind"], "rebuild")
            _, act = run_cli("answer", "--root", str(tmp), "--choice", "hold")
            self.assertEqual(act["pbi"], "PBI-4")

            act = done(tmp, act, {"status": "ok"})
            for cmd in ("check-1", "check-2", "check-3", "check-4"):
                self.assertIn(cmd, act["commands"])
            done(tmp, act, {"exit_code": 0})
            statuses = state.pbi_statuses(state.load_state(tmp))
            self.assertEqual([statuses[f"PBI-{i}"] for i in (1, 2, 3, 4)], ["passed"] * 4)

    def test_hold_stop_reports_unfinished_instead_of_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fail_first_wave_twice(tmp)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "hold_stop")
            self.assertEqual(code, 0, act)
            self.assertEqual(act["action"], "done")
            self.assertFalse(act["complete"])
            self.assertEqual(act["unfinished_pbis"], ["PBI-1", "PBI-2", "PBI-3", "PBI-4", "PBI-5"])
            report = Path(act["report"]).read_text(encoding="utf-8")
            self.assertIn("- 状態: 未完了（終わっていない PBI が 5 件ある）", report)
            self.assertNotIn("- 状態: done", report)
            self.assertIn("- PBI-1（Add helper）: 保留", report)
            self.assertIn("- PBI-3（Use both）: 未着手（PBI-1、PBI-2 を待っていて始まっていない）", report)
            handoff = Path(act["handoff"]).read_text(encoding="utf-8")
            self.assertIn("- 終わっていない PBI: PBI-1、PBI-2、PBI-3、PBI-4、PBI-5", handoff)

    def test_hold_without_dependents_offers_one_hold(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            act = reach_building(tmp)
            act = done(tmp, act, {"status": "ok"})
            act = done(tmp, act, {"exit_code": 1})
            act = done(tmp, act, {"status": "ok"})
            ask = done(tmp, act, {"exit_code": 1})
            self.assertEqual([c["id"] for c in ask["choices"]], ["retry", "retry_up", "hold"])

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
