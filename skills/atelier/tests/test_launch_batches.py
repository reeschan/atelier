"""Launch batches: the PBIs of a wave (and the fix groups) whose files do not overlap start together.

Seams agreed in docs/specs/parallel-launch-batches.md: the conductor's command line, the guard comparison,
and the overlap check of targets.
"""

from __future__ import annotations

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

from atelier import guards, plan, state, template
from tests.test_conductor import init_workspace, make_finding, make_valid_plan, run_cli, write_json, write_result_for
from tests.test_flow_rules import VALID_FACTS, init_git_repo, reach_building_with, write_agents_and_template


def commit_files(root: Path, names: list[str]) -> None:
    """Add files to the repository's first state, so a worker edits existing files."""
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", *names], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "files"], check=True)


def pbi(pid: str, targets: list[str], **extra) -> dict:
    return {"id": pid, "title": pid, "depends_on": [], "level": "L1", "reason": "small", "targets": targets, **extra}


def agent_batch_run(root: Path, pbis: list[dict], limits: dict | None = None, routes: dict | None = None) -> dict:
    init_git_repo(root)
    commit_files(root, ["a.py", "b.py", "c.py", "shared.py"])
    agents, tmpl = write_agents_and_template(root, limits=limits, routes=routes)
    return reach_building_with(root, pbis, agents_path=agents, template_path=tmpl)


def load_run(root: Path) -> dict:
    return state.load_state(root)


def write_fake_cli(root: Path, *, sleep: float = 0.0, slow_marker: str = "") -> Path:
    """An external CLI stand-in that writes an ok result, the times it ran, and prints usage.

    A launch whose result path contains slow_marker sleeps far past any test timeout.
    """
    script = root / "fake_cli.py"
    script.write_text(
        "import json, re, sys, time\n"
        "from pathlib import Path\n"
        "task = sys.stdin.read()\n"
        "result = re.search(r'Write the result to (.+)\\.', task).group(1)\n"
        "start = time.time()\n"
        f"time.sleep(30 if {slow_marker!r} and {slow_marker!r} in result else {sleep})\n"
        "Path(result + '.times').write_text(json.dumps([start, time.time()]), encoding='utf-8')\n"
        "Path(result).write_text(json.dumps({'status': 'ok'}), encoding='utf-8')\n"
        "if '-o' in sys.argv:\n"
        "    Path(sys.argv[sys.argv.index('-o') + 1]).write_text('done', encoding='utf-8')\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2}}))\n",
        encoding="utf-8",
    )
    return script


def external_batch_run(root: Path, pbis: list[dict]) -> dict:
    init_git_repo(root)
    commit_files(root, ["a.py", "b.py", "c.py"])
    routes = {f"L{i}": [{"runner": "codex", "model": "gpt-test", "effort": "low"}] for i in range(1, 6)}
    agents, tmpl = write_agents_and_template(root, routes=routes)
    _, goal, spec = init_workspace(root)
    code, out = run_cli("start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec),
                        "--agents", str(agents), "--template", str(tmpl))
    assert code == 0, out
    _, act = run_cli("next", "--root", str(root))
    write_result_for(act, VALID_FACTS)
    _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    _, act = run_cli("answer", "--root", str(root), "--choice", "skip")
    plan_obj = {**make_valid_plan(pbis), "checks": [], "e2e": []}
    write_result_for(act, plan_obj)
    _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    write_json(root / "plan_sends.json", {"choice": "approve", "sends": {p["id"]: ["codex"] for p in pbis}})
    code, act = run_cli("answer", "--root", str(root), "--reply", str(root / "plan_sends.json"))
    assert code == 0, act
    return act


class OverlapTests(unittest.TestCase):
    def test_paths_directories_and_globs(self) -> None:
        self.assertTrue(plan.paths_overlap("src/a.ts", "src/a.ts"))
        self.assertTrue(plan.paths_overlap("src/", "src/a.ts"))
        self.assertTrue(plan.paths_overlap("./src/a.ts", "src\\a.ts"))
        self.assertFalse(plan.paths_overlap("src/a.ts", "src/b.ts"))
        self.assertFalse(plan.paths_overlap("src/a", "src/ab.ts"))
        self.assertTrue(plan.paths_overlap("src/*.ts", "src/b.ts"))
        self.assertTrue(plan.paths_overlap("src/foo*.ts", "src/foobar.ts"))
        self.assertFalse(plan.paths_overlap("src/foo*.ts", "src/fo.ts"))
        self.assertTrue(plan.paths_overlap("*", "anything.py"))
        self.assertTrue(plan.targets_overlap(["a.py", "lib/"], ["lib/x.py"]))
        self.assertFalse(plan.targets_overlap(["a.py"], ["b.py", "c.py"]))


class ParallelLimitTests(unittest.TestCase):
    def test_parallel_defaults_to_three_and_must_be_a_positive_integer(self) -> None:
        base = json.loads((SKILLS_DIR / "template.default.json").read_text(encoding="utf-8"))
        del base["limits"]["parallel"]
        self.assertEqual(template.validate_template(base)["limits"]["parallel"], 3)
        for bad in (0, -1, 1.5, "2", True):
            broken = json.loads(json.dumps(base))
            broken["limits"]["parallel"] = bad
            with self.assertRaises(template.TemplateError):
                template.validate_template(broken)
        self.assertEqual(template.parallel_limit({"limits": {}}), 3)


class GuardCompareTests(unittest.TestCase):
    def test_compare_leaves_out_the_other_launches_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            commit_files(root, ["a.py", "b.py"])
            before = guards.snapshot(root, allowed_paths=["a.py"])
            (root / "a.py").write_text("changed\n", encoding="utf-8")
            (root / "b.py").write_text("changed by the other launch\n", encoding="utf-8")
            self.assertTrue(any(v["rule"] == "G1" for v in guards.compare(root, before)))
            self.assertEqual(guards.compare(root, before, ignore=["b.py"]), [])


class AgentBatchTests(unittest.TestCase):
    def test_a_wave_starts_launches_whose_targets_do_not_overlap_together(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"]), pbi("PBI-3", ["a.py"])])
            self.assertEqual(act["action"], "launches")
            self.assertEqual(act["runner"], "agent")
            self.assertEqual([m["pbi"] for m in act["launches"]], ["PBI-1", "PBI-2"])
            self.assertTrue(all(m["batch"] == act["launch_id"] for m in act["launches"]))
            prompt = Path(act["launches"][0]["prompt"]).read_text(encoding="utf-8")
            self.assertIn("running_alongside", prompt)
            self.assertIn("b.py", prompt)

            (root / "a.py").write_text("one\n", encoding="utf-8")
            (root / "b.py").write_text("two\n", encoding="utf-8")
            for member in act["launches"]:
                write_result_for(member, {"status": "ok", "tokens": {"input": 3, "output": 1}})
            code, nxt = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, nxt)
            # PBI-3 overlaps PBI-1, so it starts on its own after the batch; the wave is checked once after it.
            self.assertEqual(nxt["action"], "launch")
            self.assertEqual(nxt["pbi"], "PBI-3")
            run = load_run(root)
            self.assertEqual(state.get_pbi(run, "PBI-1")["status"], "built")
            self.assertEqual(state.get_pbi(run, "PBI-2")["status"], "built")
            self.assertEqual(run["batches"], [{"id": act["launch_id"], "members": ["PBI-1", "PBI-2"]}])

            write_result_for(nxt, {"status": "ok"})
            code, check = run_cli("done", "--root", str(root), "--launch-id", nxt["launch_id"])
            self.assertEqual(check["action"], "check")

            records = [json.loads(line) for line in (root / "docs" / "pbi" / "record.jsonl").read_text(encoding="utf-8").splitlines()]
            batched = [r for r in records if r["type"] == "launch" and r.get("batch") == act["launch_id"]]
            self.assertEqual(sorted(r["pbi"] for r in batched), ["PBI-1", "PBI-2"])

    def test_parallel_one_keeps_single_launches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"])], limits={"parallel": 1})
            self.assertEqual(act["action"], "launch")
            self.assertEqual(act["pbi"], "PBI-1")

    def test_a_guard_violation_holds_back_only_its_launch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"], do_not_touch=["shared.py"]), pbi("PBI-2", ["b.py"])])
            (root / "a.py").write_text("one\n", encoding="utf-8")
            (root / "shared.py").write_text("touched\n", encoding="utf-8")
            (root / "b.py").write_text("two\n", encoding="utf-8")
            write_result_for(act["launches"][0], {"status": "ok", "outside_edits": [{"file": "shared.py", "why": "x"}]})
            write_result_for(act["launches"][1], {"status": "ok"})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, ask)
            self.assertEqual(ask["kind"], "guard")
            run = load_run(root)
            self.assertEqual(state.get_pbi(run, "PBI-1")["status"], "pending")
            self.assertEqual(state.get_pbi(run, "PBI-2")["status"], "built")
            issue = run["guard_issue"]
            self.assertEqual([e["pbi"] for e in issue["launches"]], ["PBI-1"])
            self.assertEqual(issue["pbi"], "PBI-1")

            code, again = run_cli("answer", "--root", str(root), "--choice", "retry")
            self.assertEqual(again["action"], "launch")
            self.assertEqual(again["pbi"], "PBI-1")

    def test_two_violations_make_one_question_without_the_next_route_step(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            two_steps = {f"L{i}": [{"runner": "agent", "model": "haiku", "effort": None},
                                   {"runner": "agent", "model": "sonnet", "effort": None}] for i in range(1, 6)}
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"])], routes=two_steps)
            (root / "new_one.py").write_text("added\n", encoding="utf-8")
            for member in act["launches"]:
                write_result_for(member, {"status": "ok"})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(ask["kind"], "guard")
            self.assertNotIn("next_step", [c["id"] for c in ask["choices"]])
            issue = load_run(root)["guard_issue"]
            self.assertEqual(sorted(e["pbi"] for e in issue["launches"]), ["PBI-1", "PBI-2"])

    def test_one_file_reported_by_two_launches_is_a_clash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"])])
            (root / "shared.py").write_text("both\n", encoding="utf-8")
            for member in act["launches"]:
                write_result_for(member, {"status": "ok", "outside_edits": [{"file": "shared.py", "why": "lint"}]})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(ask["kind"], "guard")
            self.assertIn("changed by two launches running together: shared.py", load_run(root)["guard_issue"]["detail"])

    def test_an_outside_edit_reported_by_one_launch_is_that_launchs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"], do_not_touch=["shared.py"])])
            (root / "shared.py").write_text("lint fix\n", encoding="utf-8")
            write_result_for(act["launches"][0], {"status": "ok", "outside_edits": [{"file": "shared.py", "why": "lint"}]})
            write_result_for(act["launches"][1], {"status": "ok"})
            code, nxt = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(nxt["action"], "check", nxt)
            self.assertEqual(load_run(root)["outside_edits"], [{"by": "PBI-1", "file": "shared.py", "why": "lint"}])

    def test_a_second_stop_goes_back_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"]), pbi("PBI-3", ["c.py"])])
            write_result_for(act["launches"][0], {"status": "plan_broken", "plan_broken": True, "reason": "x"})
            write_result_for(act["launches"][1], {"status": "ok"})
            write_result_for(act["launches"][2], {"status": "plan_broken", "plan_broken": True, "reason": "y"})
            code, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(ask["kind"], "plan_broken")
            run = load_run(root)
            self.assertEqual(run["pending_plan_change"]["pbi"], "PBI-1")
            self.assertEqual(state.get_pbi(run, "PBI-2")["status"], "built")
            self.assertEqual(state.get_pbi(run, "PBI-3")["status"], "pending")

    def test_the_budget_is_checked_once_for_the_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            commit_files(root, ["a.py", "b.py"])
            agents, tmpl = write_agents_and_template(root)
            with patch("atelier.actions._budget_would_exceed", side_effect=lambda run, texts: isinstance(texts, list) and len(texts) == 2):
                act = reach_building_with(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"])], agents_path=agents, template_path=tmpl)
            self.assertEqual(act["kind"], "budget")
            run = load_run(root)
            self.assertEqual([p["status"] for p in run["pbis"]], ["pending", "pending"])
            code, act = run_cli("answer", "--root", str(root), "--choice", "resume")
            self.assertEqual(act["action"], "launches")
            self.assertEqual(len(act["launches"]), 2)


class MixedRunnerTests(unittest.TestCase):
    def test_external_and_agent_launches_do_not_share_a_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            routes = {f"L{i}": [{"runner": "agent", "model": "sonnet", "effort": None}] for i in range(1, 6)}
            routes["L1"] = [{"runner": "codex", "model": "gpt-test", "effort": "low"}]
            pbis = [pbi("PBI-1", ["a.py"]), {**pbi("PBI-2", ["b.py"]), "level": "L3"}, pbi("PBI-3", ["c.py"])]
            act = agent_batch_run(root, pbis, routes=routes)
            if act.get("action") == "ask":
                self.assertEqual(act["kind"], "guard")  # codex sends are not approved in this run
                return
            self.assertEqual(act["action"], "launches")
            self.assertEqual(act["runner"], "external")
            self.assertEqual([m["pbi"] for m in act["launches"]], ["PBI-1", "PBI-3"])


class ExternalBatchTests(unittest.TestCase):
    def test_drive_runs_an_external_batch_at_the_same_time_then_checks_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = external_batch_run(root, [pbi("PBI-1", ["a.py"], checks=["python -c \"pass\""]),
                                            pbi("PBI-2", ["b.py"], checks=["python -c \"pass\""])])
            self.assertEqual(act["action"], "launches", act)
            self.assertEqual(act["runner"], "external")
            script = write_fake_cli(root, sleep=1.5)
            with patch.dict(os.environ, {"ATELIER_CODEX_CMD": json.dumps([sys.executable, str(script)])}):
                code, out = run_cli("drive", "--root", str(root))
            self.assertEqual(code, 0, out)
            kinds = [step["action"] for step in out["drove"]]
            self.assertEqual(kinds[:2], ["launches", "check"])
            self.assertEqual(kinds.count("check"), 1)
            spans = [json.loads(Path(m["result"] + ".times").read_text(encoding="utf-8")) for m in act["launches"]]
            self.assertLess(max(s[0] for s in spans), min(s[1] for s in spans), "the two launches did not overlap")

    def test_a_launch_past_its_time_is_unusable_and_the_others_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = external_batch_run(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"])])
            script = write_fake_cli(root, slow_marker=act["launches"][1]["launch_id"])
            with patch.dict(os.environ, {"ATELIER_CODEX_CMD": json.dumps([sys.executable, str(script)])}):
                code, ask = run_cli("run", "--root", str(root), "--launch-id", act["launch_id"], "--timeout", "5")
            self.assertEqual(code, 0, ask)
            self.assertEqual(ask["kind"], "guard")
            run = load_run(root)
            self.assertEqual(state.get_pbi(run, "PBI-1")["status"], "built", run["guard_issue"])
            self.assertEqual(state.get_pbi(run, "PBI-2")["status"], "pending")
            self.assertIn("timed out", run["guard_issue"]["unusable_reason"] or "")


class FixStageTests(unittest.TestCase):
    def test_fix_groups_start_together_and_a_group_without_files_goes_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"])])
            (root / "a.py").write_text("one\n", encoding="utf-8")
            write_result_for(act, {"status": "ok"})
            run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            run = load_run(root)
            run["state"] = "awaiting_fix_approval"
            run["pending_action"] = None
            run["pbis"][0]["status"] = "passed"
            run["counters"]["review_count"] = 1
            run["fix_list"] = [
                {**make_finding("F1"), "file": "a.py", "decision": "fix"},
                {**make_finding("F2"), "file": "b.py", "decision": "fix"},
                {**make_finding("F3"), "file": None, "decision": "fix"},
            ]
            state.save_state(root, run)
            run_cli("next", "--root", str(root))
            code, act = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual(code, 0, act)
            self.assertEqual(act["action"], "launches")
            self.assertEqual([m["fix_group"] for m in act["launches"]], [0, 1])
            (root / "a.py").write_text("fixed a\n", encoding="utf-8")
            (root / "b.py").write_text("fixed b\n", encoding="utf-8")
            for member in act["launches"]:
                write_result_for(member, {"status": "ok"})
            code, nxt = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(nxt["action"], "launch", nxt)
            self.assertEqual(nxt["fix_group"], 2)
            write_result_for(nxt, {"status": "ok"})
            code, check = run_cli("done", "--root", str(root), "--launch-id", nxt["launch_id"])
            self.assertEqual(check["action"], "check")
            self.assertEqual(load_run(root)["fix_groups_done"], [0, 1, 2])

    def test_an_unlock_group_starts_first_and_alone_then_the_other_groups_start_together(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"])])
            write_result_for(act, {"status": "ok"})
            run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            run = load_run(root)
            run["state"] = "awaiting_fix_approval"
            run["pending_action"] = None
            run["pbis"][0]["status"] = "passed"
            run["counters"]["review_count"] = 1
            # The unlock (as the triage resolved it) sits between two findings whose files overlap nothing.
            opened = {"cases": ["R1-2"], "fixed": ["R1-2"], "added": [], "fix": "scenario", "requirement": "R1", "file": "acc/R1.json"}
            run["fix_list"] = [
                {**make_finding("F1"), "file": "a.py", "decision": "fix"},
                {**make_finding("OB-1"), "file": None, "decision": "ask", "unlock": opened},
                {**make_finding("F2"), "file": "b.py", "decision": "fix"},
            ]
            state.save_state(root, run)
            run_cli("next", "--root", str(root))
            code, act = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual(code, 0, act)
            # The unlock's group goes first and on its own: its lines are checked before any group changes the code.
            self.assertEqual((act["action"], act["fix_group"]), ("launch", 0))
            self.assertEqual([[i["id"] for i in g] for g in load_run(root)["fix_groups"]], [["OB-1"], ["F1"], ["F2"]])
            write_result_for(act, {"status": "ok"})
            code, nxt = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            # This run has no lock, so the unlock's lines cannot be checked: the unlock is left over, and then the other
            # groups start together.
            self.assertEqual(nxt["action"], "launches", nxt)
            self.assertEqual([m["fix_group"] for m in nxt["launches"]], [1, 2])
            run = load_run(root)
            self.assertEqual([(lo["kind"], lo["items"]) for lo in run["leftovers"]], [("unlock_failed", ["OB-1"])])
            self.assertEqual(run["unlock_checks"], [])


class OldRunTests(unittest.TestCase):
    def test_a_run_saved_with_fix_group_index_keeps_its_finished_groups(self) -> None:
        run = {"fix_groups": [[{"id": "F1"}], [{"id": "F2"}], [{"id": "F3"}]], "fix_group_index": 1}
        self.assertEqual(state.fix_groups_left(run), [1, 2])
        state.mark_fix_group_done(run, 1)
        self.assertEqual(state.fix_groups_left(run), [2])
        self.assertEqual(run["fix_groups_done"], [0, 1])
        self.assertNotIn("fix_group_index", run)


class ReportTests(unittest.TestCase):
    def test_the_report_lists_launch_batches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act = agent_batch_run(root, [pbi("PBI-1", ["a.py"]), pbi("PBI-2", ["b.py"])])
            for member in act["launches"]:
                write_result_for(member, {"status": "ok"})
            run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            run = load_run(root)
            run["state"] = "reporting"
            run["pending_action"] = None
            state.save_state(root, run)
            code, done = run_cli("next", "--root", str(root))
            report = Path(done["report"]).read_text(encoding="utf-8")
            self.assertIn("## 一斉起動（同時に動いた起動）", report)
            self.assertIn(f"- {act['launch_id']}: PBI-1、PBI-2", report)


if __name__ == "__main__":
    unittest.main()
