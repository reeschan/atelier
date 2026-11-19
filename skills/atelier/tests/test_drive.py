"""Seam-1 tests for `conductor.py drive`: it does the actions that need no model and stops for the caller."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import actions, state
from tests.test_conductor import VALID_FACTS, init_workspace, run_cli, write_json, write_result_for
from tests.test_flow_rules import init_git_repo
from tests.test_integration import make_pbi, make_plan


def write_fake_codex(root: Path) -> Path:
    """A codex stand-in outside PATH that writes an ok result and prints usage."""
    tool_dir = root / "outside_path"
    tool_dir.mkdir()
    worker_script = root / "ext_worker.py"
    worker_script.write_text(
        "import json, re, sys; from pathlib import Path\n"
        "task = sys.stdin.read()\n"
        "m = re.search(r'Write the result to (.+)\\.', task)\n"
        "if m:\n"
        "    Path(m.group(1)).write_text(json.dumps({'status': 'ok'}), encoding='utf-8')\n"
        "if '-o' in sys.argv:\n"
        "    Path(sys.argv[sys.argv.index('-o') + 1]).write_text('done', encoding='utf-8')\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2}}))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        cli_path = tool_dir / "codex-custom.cmd"
        cli_path.write_text(f'@echo off\n"{sys.executable}" "{worker_script}" %*\n', encoding="utf-8")
    else:
        cli_path = tool_dir / "codex-custom"
        cli_path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{worker_script}" "$@"\n', encoding="utf-8")
        cli_path.chmod(0o755)
    return cli_path


def check_command(root: Path, name: str, exit_code: int, marker: str) -> str:
    """A check command that prints a marker and exits with the given code."""
    script = root / f"{name}.py"
    script.write_text(f"print({marker!r})\nraise SystemExit({exit_code})\n", encoding="utf-8")
    return f'"{sys.executable}" {script.name}'


def start_external_run(root: Path, check_cmd: str) -> dict:
    """Start a run whose workers go to an external runner, and return the first worker launch."""
    init_git_repo(root)
    cli_path = write_fake_codex(root)
    agents = root / "agents.json"
    write_json(agents, {"runners": {"agent": {"enabled": True}, "codex": {"enabled": True, "path": str(cli_path)}}})
    base = json.loads((SKILLS_DIR / "template.default.json").read_text(encoding="utf-8"))
    base["routes"] = {f"L{i}": [{"runner": "codex", "model": "gpt-test", "effort": "low"}] for i in range(1, 6)}
    base["limits"]["auto_fix"] = 1
    tmpl = root / "template.json"
    write_json(tmpl, base)

    _, goal, spec = init_workspace(root)
    code, out = run_cli(
        "start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec),
        "--agents", str(agents), "--template", str(tmpl),
    )
    assert code == 0, out
    _, act = run_cli("next", "--root", str(root))
    write_result_for(act, VALID_FACTS)
    _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    _, act = run_cli("answer", "--root", str(root), "--choice", "skip")
    pbi = {**make_pbi("PBI-1", targets=["app.py"]), "checks": [check_cmd]}
    write_result_for(act, {**make_plan([pbi]), "checks": [], "e2e": []})
    _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    _, w1 = run_cli("answer", "--root", str(root), "--choice", "approve")
    assert w1["action"] == "launch" and w1["runner"] == "codex", w1
    return w1


class DriveTests(unittest.TestCase):
    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ATELIER_CODEX_CMD"}
        self._env = patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_drive_runs_external_launch_and_check_then_stops_at_agent_launch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start_external_run(root, check_command(root, "ok", 0, "checks-passed"))

            code, out = run_cli("drive", "--root", str(root))

            self.assertEqual(code, 0, out)
            kinds = [step["action"] for step in out["drove"]]
            self.assertEqual(kinds[:2], ["launch", "check"])
            self.assertEqual(out["drove"][1]["exit_code"], 0)
            nxt = out["next"]
            self.assertEqual(nxt["action"], "launch")
            self.assertEqual(nxt["runner"], "agent")
            self.assertEqual(nxt["role"], "reviewer")

    def test_drive_writes_failed_check_output_and_stops_at_the_caller(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start_external_run(root, check_command(root, "fail", 3, "check-failed-marker"))

            code, out = run_cli("drive", "--root", str(root))

            self.assertEqual(code, 0, out)
            checks = [step for step in out["drove"] if step["action"] == "check"]
            self.assertTrue(checks)
            self.assertEqual(checks[0]["exit_code"], 3)
            result = json.loads((state.run_dir(root) / "results" / f"{checks[0]['launch_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(result["exit_code"], 3)
            self.assertIn("check-failed-marker", result["output"])
            # The only PBI's failure is carried over to the final review, which needs the caller.
            self.assertEqual((out["next"]["action"], out["next"]["role"]), ("launch", "reviewer"))

    def test_drive_done_completes_an_agent_launch_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            _, goal, spec = init_workspace(root)
            code, out = run_cli("start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec))
            self.assertEqual(code, 0, out)
            survey = out["next"]
            self.assertEqual(survey["runner"], "agent")
            write_result_for(survey, VALID_FACTS)

            code, out = run_cli("drive", "--root", str(root), "--done", survey["launch_id"])

            self.assertEqual(code, 0, out)
            self.assertEqual(out["drove"], [{"action": "done", "launch_id": survey["launch_id"]}])
            self.assertEqual(out["next"]["action"], "ask")

    def test_drive_stops_at_once_when_the_caller_is_needed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            _, goal, spec = init_workspace(root)
            run_cli("start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec))

            code, out = run_cli("drive", "--root", str(root))

            self.assertEqual(code, 0, out)
            self.assertEqual(out["drove"], [])
            self.assertEqual(out["next"]["action"], "launch")
            self.assertEqual(out["next"]["role"], "surveyor")


class HandoffTests(unittest.TestCase):
    def test_handoff_names_branch_base_spec_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            _, goal, spec = init_workspace(root)
            run = {
                "run_id": "run-test",
                "goal_file": str(goal),
                "spec_file": str(spec),
                "base_commit": "abc123",
            }
            report = state.run_dir(root) / "REPORT.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("# report\n", encoding="utf-8")

            path = actions._write_handoff(root, run, report)

            text = path.read_text(encoding="utf-8")
            self.assertEqual(path.name, "HANDOFF.md")
            self.assertIn(str(spec), text)
            self.assertIn(str(report.resolve()), text)
            self.assertIn("git diff abc123", text)
            branch = actions._git_branch(root)
            self.assertTrue(branch)
            self.assertIn(f"ブランチ: {branch}", text)


if __name__ == "__main__":
    unittest.main()
