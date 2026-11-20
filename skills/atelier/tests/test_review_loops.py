"""Tests for fewer review loops: rule documents, checks from the repository, re-planning,
outside data, the review skill, skipped findings and the OMM cache question."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atelier import actions, flow, plan, survey
from tests.test_conductor import (
    VALID_FACTS,
    init_workspace,
    make_finding,
    make_valid_plan,
    run_cli,
    write_result_for,
)

RULES = "docs/rules.md"


def facts_with(**fields) -> dict:
    return {**VALID_FACTS, **fields}


def rule_facts() -> dict:
    return facts_with(
        rule_docs=[{"id": "r1", "text": "read before implementing", "source": "AGENTS.md:3", "path": RULES}],
        commands=[{"id": "c1", "text": "unit tests", "source": "package.json:5", "command": "npm test", "kind": "unit"}],
        review_skills=[{"id": "s1", "text": "full review", "source": "AGENTS.md:4", "name": "repo-review", "path": "skills/repo-review/SKILL.md"}],
    )


def start(tmp: Path, *extra: str) -> dict:
    root, goal, spec = init_workspace(tmp)
    (root / "docs").mkdir(exist_ok=True)
    (root / RULES).write_text("# Rules\n- R1: no ternaries\n", encoding="utf-8")
    code, out = run_cli("start", "--root", str(root), "--goal-file", str(goal), "--spec", str(spec), *extra)
    assert code == 0, out
    return out["next"]


def drive_to_plan_approval(tmp: Path, facts: dict, plan_obj: dict) -> dict:
    act = start(tmp)
    assert act["role"] == "surveyor", act
    write_result_for(act, facts)
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0, act
    code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
    assert code == 0 and act["role"] == "planner", act
    write_result_for(act, plan_obj)
    code, act = run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])
    assert code == 0 and act["kind"] == "plan_approval", act
    return act


def approve(tmp: Path) -> dict:
    code, act = run_cli("answer", "--root", str(tmp), "--choice", "approve")
    assert code == 0, act
    return act


def done(tmp: Path, act: dict, payload: dict | None) -> tuple[int, dict]:
    if payload is not None:
        write_result_for(act, payload)
    return run_cli("done", "--root", str(tmp), "--launch-id", act["launch_id"])


def run_state(tmp: Path) -> dict:
    return json.loads((tmp / "docs" / "pbi" / "run.json").read_text(encoding="utf-8"))


class RuleDocumentTests(unittest.TestCase):
    def test_rule_documents_reach_planner_worker_and_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = start(tmp)
            write_result_for(act, rule_facts())
            code, act = done(tmp, act, None)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
            rules_path = str((tmp / RULES).resolve())
            self.assertIn(rules_path, Path(act["prompt"]).read_text(encoding="utf-8"))
            code, act = done(tmp, act, make_valid_plan())
            act = approve(tmp)
            self.assertEqual("worker", act["role"])
            self.assertIn(rules_path, Path(act["prompt"]).read_text(encoding="utf-8"))
            code, act = done(tmp, act, {"status": "ok"})
            code, act = done(tmp, act, {"exit_code": 0})
            self.assertEqual("reviewer", act["role"])
            self.assertIn(rules_path, Path(act["prompt"]).read_text(encoding="utf-8"))

    def test_survey_with_missing_rule_document_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = start(tmp)
            facts = rule_facts()
            facts["rule_docs"][0]["path"] = "docs/missing.md"
            code, out = done(tmp, act, facts)
            self.assertEqual(2, code)
            self.assertIn("docs/missing.md", out["error"])

    def test_template_can_turn_rule_documents_off(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            template_file = tmp / "template.json"
            default = json.loads((Path(__file__).resolve().parent.parent / "template.default.json").read_text(encoding="utf-8"))
            default["reading"] = {"section.rule_docs": False}
            template_file.write_text(json.dumps(default), encoding="utf-8")
            act = start(tmp, "--template", str(template_file))
            write_result_for(act, rule_facts())
            done(tmp, act, None)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
            body = Path(act["prompt"]).read_text(encoding="utf-8")
            self.assertIn("Rule documents: none", body)


class CheckCommandTests(unittest.TestCase):
    def test_wave_fix_and_e2e_commands_come_from_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan_obj = make_valid_plan([
                {"id": "PBI-1", "title": "a", "checks": ["npm test -- a"], "targets": ["a.py"]},
                {"id": "PBI-2", "title": "b", "checks": ["npm test -- b"], "targets": ["b.py"]},
            ])
            plan_obj["checks"] = ["npm run type-check", "npm test -- a"]
            plan_obj["e2e"] = ["npm run e2e"]
            drive_to_plan_approval(tmp, VALID_FACTS, plan_obj)
            act = approve(tmp)
            self.assertEqual("launches", act["action"])  # a.py and b.py do not overlap: one launch batch
            code, act = done(tmp, act, {"status": "ok"})
            self.assertEqual("check", act["action"])
            self.assertEqual(["npm test -- a", "npm test -- b", "npm run type-check"], act["commands"])
            code, act = done(tmp, act, {"exit_code": 0})
            code, act = done(tmp, act, {"schema": "atelier.findings/1", "findings": [make_finding("F-1", files=["b.py"])]})
            code, act = done(tmp, act, {"schema": "atelier.triage/1", "items": [{"id": "F-1", "decision": "fix", "reason": "bug"}]})
            act = approve(tmp)
            self.assertIn('"npm test -- b"', Path(act["prompt"]).read_text(encoding="utf-8"))
            code, act = done(tmp, act, {"status": "ok"})
            self.assertEqual(["npm test -- b", "npm run type-check", "npm test -- a"], act["commands"])
            code, act = done(tmp, act, {"exit_code": 0})
            self.assertEqual(["npm run e2e"], act["commands"])

    def test_empty_e2e_is_skipped_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan_obj = make_valid_plan()
            plan_obj["e2e"] = []
            drive_to_plan_approval(tmp, VALID_FACTS, plan_obj)
            act = approve(tmp)
            code, act = done(tmp, act, {"status": "ok"})
            code, act = done(tmp, act, {"exit_code": 0})
            code, act = done(tmp, act, {"schema": "atelier.findings/1", "findings": []})
            self.assertEqual("done", act["action"], act)
            self.assertIn("E2E のコマンドが無い", Path(act["report"]).read_text(encoding="utf-8"))


class ReviewSkillTests(unittest.TestCase):
    def test_review_skill_runs_by_the_caller_and_feeds_triage(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan_obj = make_valid_plan()
            plan_obj["final_review"] = {"skill": "repo-review", "args": "full"}
            ask = drive_to_plan_approval(tmp, rule_facts(), plan_obj)
            self.assertIn("repo-review full", Path(ask["draft"]).read_text(encoding="utf-8"))
            act = approve(tmp)
            code, act = done(tmp, act, {"status": "ok"})
            code, act = done(tmp, act, {"exit_code": 0})
            self.assertEqual("review_skill", act["action"])
            self.assertEqual(("repo-review", "full"), (act["skill"], act["args"]))
            self.assertEqual([str((tmp / RULES).resolve())], act["rule_docs"])
            bad = {"schema": "atelier.findings/1", "findings": [make_finding("F-1", kind="rule")]}
            code, out = done(tmp, act, bad)
            self.assertEqual(2, code)
            self.assertIn("rule", out["error"])
            good = {"schema": "atelier.findings/1", "findings": [{**make_finding("F-1", kind="rule"), "rule": "docs/rules.md:2"}]}
            code, act = done(tmp, act, good)
            self.assertEqual(0, code, act)
            self.assertEqual("planner", act["role"])
            records = [json.loads(line) for line in (tmp / "docs" / "pbi" / "record.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(r.get("runner") == "caller" and r.get("model") == "skill:repo-review" for r in records))

    def test_plan_naming_an_unknown_review_skill_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = start(tmp)
            done(tmp, act, VALID_FACTS)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
            plan_obj = make_valid_plan()
            plan_obj["final_review"] = {"skill": "repo-review", "args": "full"}
            code, out = done(tmp, act, plan_obj)
            self.assertEqual(2, code)
            self.assertIn("review_skills", out["error"])


class SkippedFindingTests(unittest.TestCase):
    def test_dropped_and_skipped_findings_are_shown_and_can_be_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            drive_to_plan_approval(tmp, VALID_FACTS, make_valid_plan())
            act = approve(tmp)
            code, act = done(tmp, act, {"status": "ok"})
            code, act = done(tmp, act, {"exit_code": 0})
            findings = [
                make_finding("F-1", files=["a.py"]),
                make_finding("F-2", severity="minor", confidence=55, files=["b.py"]),
                make_finding("F-3", severity="minor", kind="style", files=["c.py"]),
                make_finding("F-4", files=["d.py"]),
            ]
            code, act = done(tmp, act, {"schema": "atelier.findings/1", "findings": findings})
            self.assertEqual(["F-1", "F-4"], [f["id"] for f in run_state(tmp)["findings"]])
            code, act = done(tmp, act, {"schema": "atelier.triage/1", "items": [
                {"id": "F-1", "decision": "fix", "reason": "bug"},
                {"id": "F-4", "decision": "skip", "reason": "out of scope"},
            ]})
            draft = Path(act["draft"]).read_text(encoding="utf-8")
            for fid, label in (("F-2", "確信度が 80 未満"), ("F-3", "書き方だけの指摘"), ("F-4", "見送ると仕分けた")):
                self.assertIn(f"| {fid} | {label} |", draft)
            reply = tmp / "reply.json"
            reply.write_text(json.dumps({"choice": "approve", "approved_ask_ids": ["F-9"]}), encoding="utf-8")
            code, out = run_cli("answer", "--root", str(tmp), "--reply", str(reply))
            self.assertEqual(2, code)
            reply.write_text(json.dumps({"choice": "approve", "approved_ask_ids": ["F-2", "F-4"]}), encoding="utf-8")
            code, act = run_cli("answer", "--root", str(tmp), "--reply", str(reply))
            self.assertEqual(0, code, act)
            self.assertEqual(["F-1", "F-2", "F-4"], sorted(f["id"] for f in run_state(tmp)["fix_list"]))


class ReplanTests(unittest.TestCase):
    def two_wave_plan(self) -> dict:
        return make_valid_plan([
            {"id": "PBI-1", "title": "first", "targets": ["a.py"]},
            {"id": "PBI-2", "title": "second", "depends_on": ["PBI-1"], "targets": ["b.py"]},
        ])

    def reach_broken_plan(self, tmp: Path) -> dict:
        drive_to_plan_approval(tmp, VALID_FACTS, self.two_wave_plan())
        act = approve(tmp)
        code, act = done(tmp, act, {"status": "ok"})
        code, act = done(tmp, act, {"exit_code": 0})
        self.assertEqual("PBI-2", act["pbi"])
        code, act = done(tmp, act, {"status": "plan_broken", "plan_broken": True, "reason": "an exhaustive table lives in c.py", "needed_files": ["c.py"]})
        self.assertEqual("plan_broken", act["kind"])
        return act

    def test_replan_rebuilds_unpassed_pbis_and_asks_for_approval_again(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ask = self.reach_broken_plan(tmp)
            self.assertEqual(["replan", "resume", "stop"], [c["id"] for c in ask["choices"]])
            self.assertIn("c.py", Path(ask["question"]).read_text(encoding="utf-8"))
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "replan")
            self.assertEqual("planner", act["role"])
            self.assertFalse(run_state(tmp)["approvals"]["plan"])
            body = Path(act["prompt"]).read_text(encoding="utf-8")
            self.assertIn("c.py", body)
            self.assertIn("Passed PBIs: PBI-1", body)

            changed = self.two_wave_plan()
            changed["pbis"][0]["targets"] = ["a.py", "z.py"]
            code, out = done(tmp, act, changed)
            self.assertEqual(2, code)
            self.assertIn("PBI-1", out["error"])

            fixed = self.two_wave_plan()
            fixed["pbis"][1]["targets"] = ["b.py", "c.py"]
            code, act = done(tmp, act, fixed)
            self.assertEqual("plan_approval", act["kind"])
            act = approve(tmp)
            self.assertEqual(("worker", "PBI-2"), (act["role"], act["pbi"]))
            pbis = {p["id"]: p for p in run_state(tmp)["pbis"]}
            self.assertEqual("passed", pbis["PBI-1"]["status"])
            self.assertEqual(["b.py", "c.py"], pbis["PBI-2"]["targets"])

    def test_same_pbi_retry_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self.reach_broken_plan(tmp)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "resume")
            self.assertEqual(("worker", "PBI-2"), (act["role"], act["pbi"]))


class DataPreparationTests(unittest.TestCase):
    def test_data_is_copied_before_the_worker_starts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            plan_obj = make_valid_plan()
            plan_obj["pbis"][0]["data_needs"] = [{"source": "D:/models/real.bin", "dest": "fixtures/real.bin", "why": "test with real data"}]
            ask = drive_to_plan_approval(tmp, VALID_FACTS, plan_obj)
            self.assertIn("fixtures/real.bin", Path(ask["draft"]).read_text(encoding="utf-8"))
            act = approve(tmp)
            self.assertEqual("prepare", act["action"])
            dest = Path(act["copies"][0]["dest"])
            self.assertEqual((tmp / "fixtures" / "real.bin").resolve(), dest)
            code, out = done(tmp, act, None)
            self.assertEqual(2, code)
            self.assertIn("real.bin", out["error"])
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"data")
            code, act = done(tmp, act, None)
            self.assertEqual(("launch", "worker"), (act["action"], act["role"]))
            self.assertIn("fixtures/real.bin", Path(act["prompt"]).read_text(encoding="utf-8"))

    def test_dest_outside_the_repository_is_refused(self) -> None:
        for dest in ("../real.bin", "/tmp/real.bin", "C:/real.bin"):
            with self.subTest(dest=dest):
                plan_obj = make_valid_plan()
                plan_obj["pbis"][0]["data_needs"] = [{"source": "D:/real.bin", "dest": dest, "why": "x"}]
                self.assertTrue(any("dest" in e for e in plan.validate_plan(plan_obj)))

    def test_building_before_data_is_in_place_breaks_a_never_rule(self) -> None:
        run = {
            "state": "building",
            "approvals": {"spec": True, "plan": True, "fix_list": False},
            "counters": {"auto_fix_count": 0, "review_count": 0, "e2e_failures": 0, "e2e_limit": 2, "e2e_continuing": False},
            "pbis": [{"id": "PBI-1", "status": "pending", "data_needs": [{"dest": "x"}]}],
            "data_ready": False,
        }
        with self.assertRaises(flow.InvariantError):
            flow.check_invariants(run)
        run["data_ready"] = True
        flow.check_invariants(run)


class OmmTests(unittest.TestCase):
    def test_missing_cache_asks_to_scan_then_surveys_with_the_new_scan(self) -> None:
        answers = iter([
            {"source": "miss", "folder": None, "reason": "no-candidate"},
            {"source": "cache-hit", "folder": "/cache/abc/.omm"},
        ])
        with tempfile.TemporaryDirectory() as d, patch.object(survey, "omm_context", side_effect=lambda *a, **k: next(answers)) as lookup:
            tmp = Path(d)
            act = start(tmp, "--omm-cache-dir", str(Path(d) / "cache"))
            self.assertEqual(("ask", "omm"), (act["action"], act["kind"]))
            self.assertEqual(str((Path(d) / "cache").resolve()), lookup.call_args.args[1])
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "scan")
            self.assertEqual("omm_scan", act["action"])
            code, act = done(tmp, act, None)
            self.assertEqual("surveyor", act["role"])
            self.assertIn("OMM source: cache-hit", Path(act["prompt"]).read_text(encoding="utf-8"))

    def test_skip_surveys_without_omm(self) -> None:
        with tempfile.TemporaryDirectory() as d, patch.object(survey, "omm_context", return_value={"source": "miss", "folder": None, "reason": "too-far"}):
            tmp = Path(d)
            start(tmp)
            code, act = run_cli("answer", "--root", str(tmp), "--choice", "skip")
            self.assertEqual("surveyor", act["role"])
            self.assertIn("OMM source: none", Path(act["prompt"]).read_text(encoding="utf-8"))

    def test_missing_tools_do_not_ask_and_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            act = start(tmp)
            self.assertEqual("surveyor", act["role"])
            self.assertEqual("tools", run_state(tmp)["omm"]["reason"])


def git_repo(tmp: Path, omm: bool, extra_commits: int = 0) -> str:
    """A real git repository in tmp; returns the commit that last changed .omm (or HEAD without one)."""

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
            cwd=tmp, check=True, capture_output=True, text=True, encoding="utf-8",
        )
        return proc.stdout.strip()

    git("init", "-q")
    (tmp / "app.txt").write_text("v0\n", encoding="utf-8", newline="\n")
    if omm:
        (tmp / ".omm").mkdir()
        (tmp / ".omm" / "model.json").write_text("{}\n", encoding="utf-8", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    omm_commit = git("rev-parse", "HEAD")
    for n in range(extra_commits):
        (tmp / "app.txt").write_text(f"v{n + 1}\n", encoding="utf-8", newline="\n")
        git("commit", "-q", "-am", f"change {n}")
    return omm_commit


class CommittedOmmFlowTests(unittest.TestCase):
    def test_committed_omm_goes_straight_to_the_surveyor_and_reports_the_changes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            commit = git_repo(tmp, omm=True, extra_commits=1)
            act = start(tmp)
            self.assertEqual("surveyor", act["role"], act)
            run = run_state(tmp)
            self.assertEqual("committed", run["omm"]["source"])
            self.assertEqual(commit, run["omm"]["commit"])
            self.assertFalse(run["omm_asked"])
            prompt = Path(act["prompt"]).read_text(encoding="utf-8")
            for head in ("OMM source: committed", "OMM folder: ", "OMM commit: ", "OMM changes: "):
                self.assertIn(head, prompt)
            self.assertIn("(1 files)", prompt)
            report = actions._write_report(tmp, run).read_text(encoding="utf-8")
            self.assertIn(f"- OMM: committed ({commit[:7]} から 1 ファイル変更)", report)

    def test_report_says_the_omm_is_up_to_date_with_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            git_repo(tmp, omm=True)
            act = start(tmp)
            self.assertEqual("surveyor", act["role"], act)
            run = run_state(tmp)
            self.assertEqual(0, run["omm"]["count"])
            self.assertIn("(0 files)", Path(act["prompt"]).read_text(encoding="utf-8"))
            report = actions._write_report(tmp, run).read_text(encoding="utf-8")
            self.assertIn("- OMM: committed (HEAD に追いついている)", report)

    def test_without_a_committed_omm_a_miss_still_asks(self) -> None:
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as tools:
            tmp = Path(d)
            git_repo(tmp, omm=False)
            script = Path(tools) / "cache.js"
            script.write_text("", encoding="utf-8")
            with patch.object(survey, "_cache_script", return_value=script), patch.object(
                survey.shutil, "which", return_value="/dummy"
            ), patch.object(survey, "_run", return_value=f"miss {outside} no-candidate"):
                act = start(tmp)
            self.assertEqual(("ask", "omm"), (act["action"], act["kind"]))
            self.assertTrue(run_state(tmp)["omm_asked"])
            self.assertEqual("miss", run_state(tmp)["omm"]["source"])

    def test_report_line_when_the_committed_copy_failed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            start(tmp)
            run = run_state(tmp)
            run["omm"] = {"source": "none", "folder": None, "reason": "committed-failed"}
            report = actions._write_report(tmp, run).read_text(encoding="utf-8")
            self.assertIn("- OMM: コミット済みの OMM の写しを用意できなかったため、OMM なしで調べた", report)
            run["omm"] = {"source": "cache-hit", "folder": "/x"}
            report = actions._write_report(tmp, run).read_text(encoding="utf-8")
            self.assertIn("- OMM: cache-hit", report)


class ShapeTests(unittest.TestCase):
    def test_plan_requires_evidence_and_plan_level_fields(self) -> None:
        good = make_valid_plan()
        self.assertEqual([], plan.validate_plan(good))
        for mutate, word in (
            (lambda p: p["pbis"][0].pop("evidence"), "evidence"),
            (lambda p: p["pbis"][0].update(evidence=["app.py"]), "evidence"),
            (lambda p: p["pbis"][0].pop("data_needs"), "data_needs"),
            (lambda p: p.pop("checks"), "checks"),
            (lambda p: p.pop("e2e"), "e2e"),
            (lambda p: p.pop("final_review"), "final_review"),
            (lambda p: p.update(final_review={"args": "full"}), "final_review"),
        ):
            with self.subTest(word=word):
                broken = json.loads(json.dumps(good))
                mutate(broken)
                self.assertTrue(any(word in e for e in plan.validate_plan(broken)))

    def test_facts_check_new_fields(self) -> None:
        base = {"schema": "atelier.facts/1", **{k: [] for k in survey.FIELDS}}
        self.assertEqual([], survey.validate_facts(base))
        for field, item in (
            ("commands", {"id": "c", "text": "t", "source": "a:1", "command": "x", "kind": "smoke"}),
            ("rule_docs", {"id": "r", "text": "t", "source": "a:1", "path": "../rules.md"}),
            ("review_skills", {"id": "s", "text": "t", "source": "a:1", "path": "x/SKILL.md"}),
        ):
            with self.subTest(field=field):
                self.assertTrue(survey.validate_facts({**base, field: [item]}))
        self.assertTrue(survey.validate_facts({k: v for k, v in base.items() if k != "rule_docs"}))

    def test_split_findings_and_commands(self) -> None:
        kept, dropped = plan.split_findings([
            make_finding("A"), make_finding("B", confidence=79), make_finding("C", kind="style"), make_finding("D", kind="rule"),
        ])
        self.assertEqual(["A", "D"], [f["id"] for f in kept])
        self.assertEqual([("B", "confidence"), ("C", "style")], [(f["id"], f["dropped_by"]) for f in dropped])
        pbis = [{"checks": ["u a"], "targets": ["a.py"]}, {"checks": ["u b"], "targets": ["b.py"]}]
        self.assertEqual(["u a", "u b", "t"], plan.wave_commands(pbis, ["t", "u a"]))
        self.assertEqual(["u b", "t"], plan.fix_commands(pbis, ["t"], ["./b.py"]))
        self.assertEqual(["u a", "u b"], plan.fix_commands(pbis, [], ["spec.md"]))

    def test_prompts_ask_for_the_new_work(self) -> None:
        prompts = Path(__file__).resolve().parent.parent / "prompts"
        self.assertIn("evidence", (prompts / "planner.md").read_text(encoding="utf-8"))
        self.assertIn("exhaustive", (prompts / "planner.md").read_text(encoding="utf-8"))
        self.assertIn("needed_files", (prompts / "worker.md").read_text(encoding="utf-8"))
        self.assertIn("Rule documents", (prompts / "reviewer.md").read_text(encoding="utf-8"))
        self.assertIn("existing mechanism", (prompts / "interviewer.md").read_text(encoding="utf-8"))
        surveyor = (prompts / "surveyor.md").read_text(encoding="utf-8")
        for field in ("rule_docs", "commands", "review_skills"):
            self.assertIn(field, surveyor)


if __name__ == "__main__":
    unittest.main()
