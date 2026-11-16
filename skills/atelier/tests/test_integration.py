"""Seam-1 integration tests for pages, interview, plan, record, guards, and a full fake-role run."""

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

from tests.test_conductor import VALID_FACTS, init_workspace, run_cli, start_run, write_json, write_result_for
from tests.test_flow_rules import init_git_repo, write_agents_and_template


TO_SPEC_BODY = (
    "# Spec\n\n"
    "## Problem Statement\nUsers need a clean export.\n\n"
    "## Solution\nAdd an exporter module.\n\n"
    "## User Stories\n1. Export data as JSON.\n\n"
    "## Implementation Decisions\nUse standard library json.\n\n"
    "## Testing Decisions\nRun unit tests.\n\n"
    "## Out of Scope\nCSV export.\n\n"
    "## Further Notes\nNone.\n"
)


def make_pbi(
    pid: str = "PBI-1",
    *,
    title: str | None = None,
    depends_on: list[str] | None = None,
    level: str = "L1",
    level_reason: str = "15分以内の小さな変更",
    targets: list[str] | None = None,
    do_not_touch: list[str] | None = None,
    sensitive_candidate: bool = False,
    sensitive_reason: str = "",
) -> dict:
    return {
        "id": pid,
        "title": title or f"Implement {pid}",
        "goal": f"Goal for {pid}",
        "level": level,
        "level_reason": level_reason,
        "sensitive_candidate": sensitive_candidate,
        "sensitive_reason": sensitive_reason,
        "depends_on": list(depends_on or []),
        "targets": list(targets or ["app.py"]),
        "do_not_touch": list(do_not_touch or []),
        "checks": ["python run_tests.py"],
        "facts": ["m1"],
        "acceptance": [f"{pid} passes checks"],
    }


def make_plan(pbis: list[dict] | None = None, open_questions: list[str] | None = None) -> dict:
    return {
        "schema": "atelier.plan/1",
        "pbis": pbis or [make_pbi()],
        "open_questions": list(open_questions or []),
    }


def make_finding(
    fid: str = "F-1",
    *,
    file: str = "app.py",
    files: list[str] | None = None,
    severity: str = "major",
    confidence: int = 95,
    kind: str = "correctness",
) -> dict:
    item = {
        "id": fid,
        "file": file,
        "line": 1,
        "severity": severity,
        "confidence": confidence,
        "kind": kind,
        "why": f"issue {fid}",
        "fix": f"fix {fid}",
    }
    if files is not None:
        item["files"] = files
    return item


def read_records(root: Path) -> list[dict]:
    path = root / "docs" / "pbi" / "record.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestIntegrationSeam1(unittest.TestCase):
    def test_pages_approval_renders_and_g10_refuses_corrupt_draft(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start_run(root, with_spec=False)
            _, act = run_cli("next", "--root", str(root))
            write_result_for(act, VALID_FACTS)
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--choice", "interview")

            # Complete interview with valid to-spec -> spec_approval rendered page
            spec_path = root / "docs" / "pbi" / "interview" / "spec.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(TO_SPEC_BODY, encoding="utf-8")
            write_result_for(act, {"done": True, "spec_path": str(spec_path)})
            code, spec_ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, spec_ask)
            self.assertEqual(spec_ask["kind"], "spec_approval")
            self.assertTrue(spec_ask["page"] and Path(spec_ask["page"]).is_file())
            self.assertTrue(spec_ask.get("summary"))
            self.assertTrue(Path(spec_ask["draft"]).is_file())

            code, act = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual(code, 0, act)

            # Corrupt PBI title triggers G10 refusal, never presents plan_approval
            bad_plan = make_plan([make_pbi("PBI-1", title="壊れ???た")])
            write_result_for(act, bad_plan)
            code, err = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            # Retry with clean plan -> plan_approval rendered page
            write_result_for(act, make_plan([make_pbi("PBI-1", title="正常なPBI")]))
            code, plan_ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, plan_ask)
            self.assertEqual(plan_ask["kind"], "plan_approval")
            self.assertTrue(plan_ask["page"] and Path(plan_ask["page"]).is_file())
            self.assertTrue(plan_ask.get("summary"))

            # Progress to fix_approval -> rendered page
            _, act = run_cli("answer", "--root", str(root), "--choice", "approve")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"schema": "atelier.findings/1", "findings": [make_finding("F-1")]})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {
                "schema": "atelier.triage/1",
                "items": [{"id": "F-1", "decision": "fix", "reason": "must fix"}],
            })
            code, fix_ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, fix_ask)
            self.assertEqual(fix_ask["kind"], "fix_approval")
            self.assertTrue(fix_ask["page"] and Path(fix_ask["page"]).is_file())
            self.assertTrue(fix_ask.get("summary"))

    def test_interview_wiring_enforces_tree_g6_and_to_spec(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start_run(root, with_spec=False)
            _, act = run_cli("next", "--root", str(root))
            write_result_for(act, VALID_FACTS)
            _, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(ask["kind"], "interview_or_skip")
            self.assertEqual([c["id"] for c in ask["choices"]], ["interview"])

            # Cannot skip without spec
            code, err = run_cli("answer", "--root", str(root), "--choice", "skip")
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            _, act = run_cli("answer", "--root", str(root), "--choice", "interview")
            self.assertEqual(act["role"], "interviewer")
            prompt_text = Path(act["prompt"]).read_text(encoding="utf-8")
            self.assertLess(prompt_text.index("## Fact Sheet"), prompt_text.index("## Design Tree"))

            # Interviewer tries to alter decided[] (G6 violation) -> refused
            tree_path = root / "docs" / "pbi" / "interview" / "tree.json"
            write_json(tree_path, {
                "schema": "atelier.tree/1",
                "decided": [{"id": "Q0", "text": "forged"}],
                "open": [],
            })
            write_result_for(act, {"done": False})
            code, err = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 2)
            self.assertIn("G6", err["error"])

            # Valid tree update with frontier question Q1
            write_json(tree_path, {
                "schema": "atelier.tree/1",
                "decided": [],
                "open": [
                    {"id": "Q1", "question": "出力形式は？", "recommended": "JSON", "depends_on": []},
                ],
            })
            q_file = root / "docs" / "pbi" / "interview" / "round-1-questions.md"
            q_file.parent.mkdir(parents=True, exist_ok=True)
            q_file.write_text("1. [Q1] 出力形式は？\n   - おすすめの答え: JSON\n", encoding="utf-8")
            write_result_for(act, {"done": False, "question_file": str(q_file), "frontier_ids": ["Q1"]})
            code, round_ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, round_ask)
            self.assertEqual(round_ask["kind"], "interview_round")
            self.assertIn("出力形式は？", Path(round_ask["question"]).read_text(encoding="utf-8"))

            # User answer moves Q1 to decided with user text (NOT recommended "JSON")
            code, act2 = run_cli("answer", "--root", str(root), "--text", "1. YAML にする")
            self.assertEqual(code, 0, act2)
            saved_tree = json.loads(tree_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_tree["decided"], [{"id": "Q1", "text": "YAML にする"}])
            self.assertEqual(saved_tree["open"], [])

            # Missing to-spec sections refused
            bad_spec = root / "docs" / "pbi" / "interview" / "spec.md"
            bad_spec.write_text("# Incomplete\n## Problem Statement\nOnly one.\n", encoding="utf-8")
            write_result_for(act2, {"done": True, "spec_path": str(bad_spec)})
            code, err = run_cli("done", "--root", str(root), "--launch-id", act2["launch_id"])
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            # Complete to-spec accepted -> awaiting_spec_approval
            bad_spec.write_text(TO_SPEC_BODY, encoding="utf-8")
            code, spec_ask = run_cli("done", "--root", str(root), "--launch-id", act2["launch_id"])
            self.assertEqual(code, 0, spec_ask)
            self.assertEqual(spec_ask["kind"], "spec_approval")

    def test_plan_validation_waves_prompts_triage_and_fix_groups(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start_run(root)
            _, act = run_cli("next", "--root", str(root))
            write_result_for(act, VALID_FACTS)
            run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--choice", "skip")
            self.assertEqual(act["role"], "planner")
            planner_p = Path(act["prompt"]).read_text(encoding="utf-8")
            self.assertIn("Mode: plan", planner_p)
            self.assertNotIn("tree.json", planner_p)

            # Invalid plan refused
            write_result_for(act, {"schema": "atelier.plan/1", "pbis": [], "open_questions": []})
            code, err = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            # Plan with PBI-2 listed before PBI-1 even though PBI-2 depends on PBI-1 -> waves() orders PBI-1 first
            plan_v1 = make_plan([
                make_pbi("PBI-2", depends_on=["PBI-1"], targets=["b.py"]),
                make_pbi("PBI-1", depends_on=[], targets=["a.py"]),
            ])
            write_result_for(act, plan_v1)
            _, ask = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(ask["kind"], "plan_approval")

            # Revise -> Mode: revise with user request
            _, rev_act = run_cli("answer", "--root", str(root), "--choice", "revise")
            rev_prompt = Path(rev_act["prompt"]).read_text(encoding="utf-8")
            self.assertIn("Mode: revise", rev_prompt)
            self.assertIn("Current plan:", rev_prompt)

            write_result_for(rev_act, plan_v1)
            _, ask2 = run_cli("done", "--root", str(root), "--launch-id", rev_act["launch_id"])
            self.assertEqual(ask2["kind"], "plan_approval")
            _, w1 = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual(w1["pbi"], "PBI-1")
            self.assertIn("Mode: pbi", Path(w1["prompt"]).read_text(encoding="utf-8"))
            self.assertIn("[G12]", Path(w1["prompt"]).read_text(encoding="utf-8"))
            self.assertIn("[G13]", Path(w1["prompt"]).read_text(encoding="utf-8"))

            write_result_for(w1, {"status": "ok"})
            _, chk1 = run_cli("done", "--root", str(root), "--launch-id", w1["launch_id"])
            write_result_for(chk1, {"exit_code": 0})
            _, w2 = run_cli("done", "--root", str(root), "--launch-id", chk1["launch_id"])
            self.assertEqual(w2["pbi"], "PBI-2")
            write_result_for(w2, {"status": "ok"})
            _, chk2 = run_cli("done", "--root", str(root), "--launch-id", w2["launch_id"])
            write_result_for(chk2, {"exit_code": 0})
            _, rev = run_cli("done", "--root", str(root), "--launch-id", chk2["launch_id"])
            self.assertEqual(rev["role"], "reviewer")
            self.assertIn("Approved spec:", Path(rev["prompt"]).read_text(encoding="utf-8"))

            # Reviewer returns 5 findings: F-1..F-4 kept, F-5 dropped (confidence < 80)
            write_result_for(rev, {
                "schema": "atelier.findings/1",
                "findings": [
                    make_finding("F-1", file="a.py"),
                    make_finding("F-2", file="b.py"),
                    make_finding("F-3", file="c.py"),
                    make_finding("F-4", file="d.py"),
                    make_finding("F-5", file="e.py", confidence=70),
                ],
            })
            _, tri = run_cli("done", "--root", str(root), "--launch-id", rev["launch_id"])
            self.assertEqual(tri["role"], "planner")
            self.assertIn("Mode: triage", Path(tri["prompt"]).read_text(encoding="utf-8"))

            # Missing finding ID in triage refused
            write_result_for(tri, {
                "schema": "atelier.triage/1",
                "items": [{"id": "F-1", "decision": "fix", "reason": "fix"}],
            })
            code, err = run_cli("done", "--root", str(root), "--launch-id", tri["launch_id"])
            self.assertEqual(code, 2)
            self.assertIn("error", err)

            # Valid triage for all 4 kept findings -> fix_approval -> approve -> max 3 fix_group workers
            write_result_for(tri, {
                "schema": "atelier.triage/1",
                "items": [
                    {"id": "F-1", "decision": "fix", "reason": "r1"},
                    {"id": "F-2", "decision": "fix", "reason": "r2"},
                    {"id": "F-3", "decision": "fix", "reason": "r3"},
                    {"id": "F-4", "decision": "fix", "reason": "r4"},
                ],
            })
            _, fix_ask = run_cli("done", "--root", str(root), "--launch-id", tri["launch_id"])
            self.assertEqual(fix_ask["kind"], "fix_approval")
            _, fx1 = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual(fx1["action"], "launch")
            self.assertEqual(fx1["role"], "worker")
            self.assertIn("Mode: fix", Path(fx1["prompt"]).read_text(encoding="utf-8"))

            write_result_for(fx1, {"status": "ok"})
            _, fx2 = run_cli("done", "--root", str(root), "--launch-id", fx1["launch_id"])
            self.assertEqual(fx2["action"], "launch")
            write_result_for(fx2, {"status": "ok"})
            _, fx3 = run_cli("done", "--root", str(root), "--launch-id", fx2["launch_id"])
            self.assertEqual(fx3["action"], "launch")
            write_result_for(fx3, {"status": "ok"})
            # After 3rd worker, must move to fix check (never > 3 workers)
            _, fix_chk = run_cli("done", "--root", str(root), "--launch-id", fx3["launch_id"])
            self.assertEqual(fix_chk["action"], "check")

    def test_record_jsonl_and_report_md_with_reading_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents, tmpl = write_agents_and_template(root)
            tdata = json.loads(tmpl.read_text(encoding="utf-8"))
            tdata["reading"] = {"G12": False}
            write_json(tmpl, tdata)

            _, goal, spec = init_workspace(root)
            run_cli("start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec), "--agents", str(agents), "--template", str(tmpl))
            _, act = run_cli("next", "--root", str(root))
            write_result_for(act, {**VALID_FACTS, "tokens": {"input": 10, "output": 5, "total": 15}, "cost": 0.01})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--choice", "skip")
            write_result_for(act, make_plan([make_pbi("PBI-1")]))
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, w1 = run_cli("answer", "--root", str(root), "--choice", "approve")

            # G12 was toggled off by template.reading["G12"] = False
            w1_prompt = Path(w1["prompt"]).read_text(encoding="utf-8")
            self.assertNotIn("[G12]", w1_prompt)
            self.assertIn("[G13]", w1_prompt)

            write_result_for(w1, {"status": "ok"})  # unknown tokens/cost
            _, chk1 = run_cli("done", "--root", str(root), "--launch-id", w1["launch_id"])
            write_result_for(chk1, {"exit_code": 1, "failed_pbis": ["PBI-1"], "rules": ["G13"]})
            _, af1 = run_cli("done", "--root", str(root), "--launch-id", chk1["launch_id"])
            write_result_for(af1, {"status": "ok", "fixed_rules": ["G13"]})
            _, chk2 = run_cli("done", "--root", str(root), "--launch-id", af1["launch_id"])
            write_result_for(chk2, {"exit_code": 0})
            _, rev = run_cli("done", "--root", str(root), "--launch-id", chk2["launch_id"])
            write_result_for(rev, {"schema": "atelier.findings/1", "findings": []})
            _, e2e = run_cli("done", "--root", str(root), "--launch-id", rev["launch_id"])
            write_result_for(e2e, {"exit_code": 0})
            code, done_act = run_cli("done", "--root", str(root), "--launch-id", e2e["launch_id"])
            self.assertEqual(code, 0, done_act)
            self.assertEqual(done_act["action"], "done")
            self.assertTrue(done_act["report"].endswith("REPORT.md"))
            report_text = Path(done_act["report"]).read_text(encoding="utf-8")
            self.assertIn("# 記録の集計", report_text)
            self.assertIn("不明", report_text)
            self.assertNotIn("効いた", report_text)

            recs = read_records(root)
            launches = [r for r in recs if r["type"] == "launch"]
            checks = [r for r in recs if r["type"] == "check"]
            fixes = [r for r in recs if r["type"] == "fix"]
            self.assertTrue(any(r["role"] == "worker" and "G13" in r["read"] and "G12" not in r["read"] for r in launches))
            self.assertTrue(any(r["tokens"] == "unknown" and r["cost"] == "unknown" for r in launches))
            self.assertTrue(any(r["stage"] == "build" and r["first_pass"] is True and r["result"] == "fail" for r in checks))
            self.assertTrue(any(r["stage"] == "build" and r["first_pass"] is False and r["result"] == "pass" for r in checks))
            self.assertTrue(any(r["rule"] == "G13" and r["method"] == "auto" for r in fixes))

    def test_guards_g3_g4_g5_g11_g12(self) -> None:
        # 1. G3 unapproved external send & G4 secret in prompt before writing for external runner
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            routes = {f"L{i}": [{"runner": "codex", "model": "gpt-test", "effort": "low"}] for i in range(1, 6)}
            agents, tmpl = write_agents_and_template(root, routes=routes)
            _, goal, spec = init_workspace(root)
            run_cli("start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec), "--agents", str(agents), "--template", str(tmpl))
            _, act = run_cli("next", "--root", str(root))
            write_result_for(act, VALID_FACTS)
            run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--choice", "skip")

            # Inject secret into PBI title -> G4 refuses external launch BEFORE prompt is written
            secret_tok = "sk-" + "a" * 32
            write_result_for(act, make_plan([make_pbi("PBI-1", title=f"Fix {secret_tok}", targets=["app.py"])]))
            _, ask_p = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(ask_p["kind"], "guard" if ask_p["kind"] == "guard" else "plan_approval")
            code, g4_ask = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual(code, 0, g4_ask)
            self.assertEqual(g4_ask["action"], "ask")
            self.assertEqual(g4_ask["kind"], "guard")
            q_text = Path(g4_ask["question"]).read_text(encoding="utf-8")
            self.assertIn("G4", q_text)
            self.assertNotIn(secret_tok, q_text)
            self.assertEqual(list((root / "docs" / "pbi" / "prompts").glob("worker-*.md")), [])

            # G3: remove approved plan_sends for PBI-1 and verify external launch is refused with asking_guard
            run_file = root / "docs" / "pbi" / "run.json"
            run_data = json.loads(run_file.read_text(encoding="utf-8"))
            run_data["pbis"][0]["title"] = "Clean title"
            run_data["approvals"]["plan_sends"] = {}
            run_file.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
            code, g3_ask = run_cli("answer", "--root", str(root), "--choice", "resume")
            self.assertEqual(code, 0, g3_ask)
            self.assertEqual(g3_ask["kind"], "guard")
            self.assertIn("G3", Path(g3_ask["question"]).read_text(encoding="utf-8"))

        # 2. G11 forbidden_args & G12 destructive_changes + G5 install_attempts
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            routes = {f"L{i}": [{"runner": "codex", "model": "gpt-test", "effort": "low"}] for i in range(1, 6)}
            agents, tmpl = write_agents_and_template(root, routes=routes)
            _, goal, spec = init_workspace(root)
            comp = root / "components.json"
            write_json(comp, {"codex": {"forbidden_args": ["--json"]}})
            run_cli(
                "start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec),
                "--agents", str(agents), "--template", str(tmpl), "--components", str(comp),
            )
            _, act = run_cli("next", "--root", str(root))
            write_result_for(act, VALID_FACTS)
            run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--choice", "skip")
            write_result_for(act, make_plan([make_pbi("PBI-1", targets=["app.py", "migrations/001.sql"])]))
            run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            code, g11_ask = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual(code, 0, g11_ask)
            self.assertEqual(g11_ask["kind"], "guard")
            self.assertIn("G11", Path(g11_ask["question"]).read_text(encoding="utf-8"))

            # Clear forbidden_args and test G12 destructive_changes on agent worker
            run_file = root / "docs" / "pbi" / "run.json"
            rd = json.loads(run_file.read_text(encoding="utf-8"))
            rd["components"] = {}
            rd["pbis"][0]["route"] = [{"runner": "agent", "model": "haiku", "effort": None}]
            run_file.write_text(json.dumps(rd, ensure_ascii=False, indent=2), encoding="utf-8")

            _, w1 = run_cli("answer", "--root", str(root), "--choice", "resume")
            self.assertEqual(w1["action"], "launch")
            (root / "migrations").mkdir()
            (root / "migrations" / "001.sql").write_text("CREATE TABLE t(id int);", encoding="utf-8")
            write_result_for(w1, {"status": "ok"})
            code, g12_ask = run_cli("done", "--root", str(root), "--launch-id", w1["launch_id"])
            self.assertEqual(code, 0, g12_ask)
            self.assertEqual(g12_ask["kind"], "guard")
            self.assertIn("G12", Path(g12_ask["question"]).read_text(encoding="utf-8"))

            # Approving G12 lets the run proceed without looping
            code, chk = run_cli("answer", "--root", str(root), "--choice", "resume")
            self.assertEqual(code, 0, chk)
            self.assertEqual(chk["action"], "launch")

        # 3. G4 secret in record line refused without writing secret to record.jsonl
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start_run(root)
            _, act = run_cli("next", "--root", str(root))
            secret_tok = "ghp_" + "B" * 36
            write_result_for(act, VALID_FACTS)
            run_file = root / "docs" / "pbi" / "run.json"
            rd = json.loads(run_file.read_text(encoding="utf-8"))
            rd["pending_action"]["model"] = secret_tok
            run_file.write_text(json.dumps(rd, ensure_ascii=False, indent=2), encoding="utf-8")
            code, g4_rec = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(code, 0, g4_rec)
            self.assertEqual(g4_rec["kind"], "guard")
            rec_path = root / "docs" / "pbi" / "record.jsonl"
            if rec_path.exists():
                self.assertNotIn(secret_tok, rec_path.read_text(encoding="utf-8"))

    def test_full_fake_role_run_visits_every_state_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            agents, tmpl = write_agents_and_template(root, limits={"e2e_rounds": 1})
            _, goal, _ = init_workspace(root, with_spec=False)

            visited: list[str] = []

            def record_state() -> str:
                _, st = run_cli("status", "--root", str(root))
                visited.append(st["state"])
                return st["state"]

            # 1. surveying
            _, out = run_cli("start", "--root", str(root), "--goal-file", str(goal), "--agents", str(agents), "--template", str(tmpl))
            record_state()
            _, act = run_cli("next", "--root", str(root))
            write_result_for(act, VALID_FACTS)
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])

            # 2. asking_interview -> 3. interviewing (round + revise)
            self.assertEqual(record_state(), "asking_interview")
            _, act = run_cli("answer", "--root", str(root), "--choice", "interview")
            self.assertEqual(record_state(), "interviewing")

            tree_path = root / "docs" / "pbi" / "interview" / "tree.json"
            write_json(tree_path, {
                "schema": "atelier.tree/1",
                "decided": [],
                "open": [{"id": "Q1", "question": "保存形式は？", "recommended": "JSON", "depends_on": []}],
            })
            q_file = root / "docs" / "pbi" / "interview" / "round-1-questions.md"
            q_file.parent.mkdir(parents=True, exist_ok=True)
            q_file.write_text("1. [Q1] 保存形式は？\n   - おすすめの答え: JSON\n", encoding="utf-8")
            write_result_for(act, {"done": False, "question_file": str(q_file), "frontier_ids": ["Q1"]})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--text", "1. JSON")

            spec_path = root / "docs" / "pbi" / "interview" / "spec.md"
            spec_path.write_text(TO_SPEC_BODY, encoding="utf-8")
            write_result_for(act, {"done": True, "spec_path": str(spec_path)})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])

            # 4. awaiting_spec_approval (revise -> approve)
            self.assertEqual(record_state(), "awaiting_spec_approval")
            _, act = run_cli("answer", "--root", str(root), "--choice", "revise")
            write_result_for(act, {"done": True, "spec_path": str(spec_path)})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--choice", "approve")

            # 5. planning -> 6. awaiting_plan_approval (revise -> approve)
            self.assertEqual(record_state(), "planning")
            plan_obj = make_plan([
                make_pbi("PBI-1", depends_on=[], targets=["app.py"]),
                make_pbi("PBI-2", depends_on=["PBI-1"], targets=["app.py"]),
            ])
            write_result_for(act, plan_obj)
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(record_state(), "awaiting_plan_approval")
            _, act = run_cli("answer", "--root", str(root), "--choice", "revise")
            write_result_for(act, plan_obj)
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            _, act = run_cli("answer", "--root", str(root), "--choice", "approve")

            # 7. building (PBI-1) -> 8. checking (fail #1) -> 9. auto_fixing -> checking (fail #2) -> 10. asking_rebuild
            self.assertEqual(record_state(), "building")
            (root / "app.py").write_text("x = 2\n", encoding="utf-8")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(record_state(), "checking")

            write_result_for(act, {"exit_code": 1, "failed_pbis": ["PBI-1"], "output": "fail 1"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(record_state(), "auto_fixing")

            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 1, "failed_pbis": ["PBI-1"], "output": "fail 2"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(record_state(), "asking_rebuild")

            _, act = run_cli("answer", "--root", str(root), "--choice", "retry_up")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])

            # Wave 2: PBI-2
            self.assertEqual(act["pbi"], "PBI-2")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])

            # 11. reviewing -> 12. triaging -> 13. awaiting_fix_approval -> 14. fixing
            self.assertEqual(record_state(), "reviewing")
            write_result_for(act, {"schema": "atelier.findings/1", "findings": [make_finding("F-1")]})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(record_state(), "triaging")

            write_result_for(act, {
                "schema": "atelier.triage/1",
                "items": [{"id": "F-1", "decision": "fix", "reason": "fix bug"}],
            })
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(record_state(), "awaiting_fix_approval")

            _, act = run_cli("answer", "--root", str(root), "--choice", "approve")
            self.assertEqual(record_state(), "fixing")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])

            # 15. running_e2e -> fail #1 (<= limit 1 -> fixing) -> fail #2 (> limit 1 -> 16. asking_e2e_limit)
            self.assertEqual(record_state(), "running_e2e")
            write_result_for(act, {"exit_code": 1})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])

            write_result_for(act, {"exit_code": 1})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(record_state(), "asking_e2e_limit")

            # continue -> 1 fix -> e2e pass -> 17. reporting -> 18. done
            _, act = run_cli("answer", "--root", str(root), "--choice", "continue")
            write_result_for(act, {"status": "ok"})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            write_result_for(act, {"exit_code": 0})
            _, done_act = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
            self.assertEqual(done_act["action"], "done")
            self.assertEqual(record_state(), "done")

            _, final_st = run_cli("status", "--root", str(root))
            history_states = {h["from"] for h in final_st["history"]} | {h["to"] for h in final_st["history"]}
            expected_diagram_states = {
                "surveying", "asking_interview", "interviewing", "awaiting_spec_approval",
                "planning", "awaiting_plan_approval", "building", "checking", "auto_fixing",
                "asking_rebuild", "reviewing", "triaging", "awaiting_fix_approval", "fixing",
                "running_e2e", "asking_e2e_limit", "reporting", "done",
            }
            self.assertTrue(expected_diagram_states <= history_states, expected_diagram_states - history_states)
            self.assertTrue(Path(done_act["report"]).is_file())
            self.assertTrue((root / "docs" / "pbi" / "REPORT.md").is_file())


if __name__ == "__main__":
    unittest.main()
