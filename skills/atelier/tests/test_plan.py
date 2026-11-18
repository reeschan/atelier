"""Seam tests for atelier.plan and planner/worker/reviewer prompts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atelier import plan


def _valid_pbi(pbi_id: str = "PBI-1", *, depends_on: list[str] | None = None, sensitive: bool = False) -> dict:
    return {
        "id": pbi_id,
        "title": f"Title {pbi_id}",
        "goal": f"Goal {pbi_id}",
        "level": "L2",
        "level_reason": "明確な変更で1時間以内",
        "sensitive_candidate": sensitive,
        "sensitive_reason": "認証トークンを扱う" if sensitive else "",
        "depends_on": list(depends_on or []),
        "targets": [f"src/{pbi_id.lower()}.py"],
        "do_not_touch": ["src/unrelated.py"],
        "checks": ["python run_tests.py"],
        "facts": ["F1"],
        "acceptance": [f"{pbi_id} passes tests"],
        "evidence": [f"src/{pbi_id.lower()}.py:1"],
        "data_needs": [],
    }


def _valid_plan() -> dict:
    return {
        "schema": "atelier.plan/1",
        "pbis": [_valid_pbi("PBI-1"), _valid_pbi("PBI-2", depends_on=["PBI-1"])],
        "checks": ["python -m unittest"],
        "e2e": [],
        "final_review": None,
        "open_questions": [],
    }


def _valid_finding(
    fid: str = "F-1",
    *,
    file: str = "src/a.py",
    files: list[str] | None = None,
    severity: str = "major",
    confidence: int = 90,
    kind: str = "correctness",
) -> dict:
    item = {
        "id": fid,
        "file": file,
        "line": 12,
        "severity": severity,
        "confidence": confidence,
        "kind": kind,
        "why": "null input raises KeyError",
        "fix": "return default when key is absent",
    }
    if files is not None:
        item["files"] = files
    return item


class PlanValidationTests(unittest.TestCase):
    def test_valid_plan_passes(self) -> None:
        self.assertEqual([], plan.validate_plan(_valid_plan()))
        with_q = _valid_plan()
        with_q["pbis"][0] = _valid_pbi("PBI-1", sensitive=True)
        with_q["open_questions"] = ["セッション期限の既定値"]
        self.assertEqual([], plan.validate_plan(with_q))

    def test_validate_plan_checks_schema_and_required_fields(self) -> None:
        self.assertTrue(plan.validate_plan([]))
        self.assertTrue(plan.validate_plan({"schema": "wrong", "pbis": [_valid_pbi()], "open_questions": []}))

        empty_pbis = _valid_plan()
        empty_pbis["pbis"] = []
        self.assertTrue(plan.validate_plan(empty_pbis))

        bad_level = _valid_plan()
        bad_level["pbis"][0]["level"] = "L6"
        self.assertTrue(plan.validate_plan(bad_level))

        no_level_reason = _valid_plan()
        no_level_reason["pbis"][0]["level_reason"] = "  "
        self.assertTrue(plan.validate_plan(no_level_reason))

        bad_sensitive_type = _valid_plan()
        bad_sensitive_type["pbis"][0]["sensitive_candidate"] = "yes"
        self.assertTrue(plan.validate_plan(bad_sensitive_type))

        missing_sensitive_reason = _valid_plan()
        missing_sensitive_reason["pbis"][0]["sensitive_candidate"] = True
        missing_sensitive_reason["pbis"][0]["sensitive_reason"] = ""
        self.assertTrue(plan.validate_plan(missing_sensitive_reason))

        no_checks = _valid_plan()
        no_checks["pbis"][0]["checks"] = []
        self.assertTrue(plan.validate_plan(no_checks))

        bad_facts = _valid_plan()
        bad_facts["pbis"][0]["facts"] = "F1"
        self.assertTrue(plan.validate_plan(bad_facts))

        no_targets = _valid_plan()
        no_targets["pbis"][0]["targets"] = []
        self.assertTrue(plan.validate_plan(no_targets))

        overlap = _valid_plan()
        overlap["pbis"][0]["do_not_touch"] = ["src/pbi-1.py"]
        self.assertTrue(plan.validate_plan(overlap))

    def test_validate_plan_checks_duplicate_ids_and_dependency_cycles(self) -> None:
        dup = _valid_plan()
        dup["pbis"][1]["id"] = "PBI-1"
        self.assertTrue(plan.validate_plan(dup))

        unknown_dep = _valid_plan()
        unknown_dep["pbis"][0]["depends_on"] = ["PBI-99"]
        self.assertTrue(plan.validate_plan(unknown_dep))

        cyclic = _valid_plan()
        cyclic["pbis"][0]["depends_on"] = ["PBI-2"]
        cyclic["pbis"][1]["depends_on"] = ["PBI-1"]
        self.assertTrue(plan.validate_plan(cyclic))

    def test_waves_orders_by_dependencies_and_rejects_cycles(self) -> None:
        obj = {
            "schema": "atelier.plan/1",
            "pbis": [
                _valid_pbi("PBI-1"),
                _valid_pbi("PBI-2"),
                _valid_pbi("PBI-3", depends_on=["PBI-1"]),
                _valid_pbi("PBI-4", depends_on=["PBI-2", "PBI-3"]),
            ],
            "open_questions": [],
        }
        result = plan.waves(obj)
        self.assertEqual(
            [[p["id"] for p in w] for w in result],
            [["PBI-1", "PBI-2"], ["PBI-3"], ["PBI-4"]],
        )

        cyclic = _valid_plan()
        cyclic["pbis"][0]["depends_on"] = ["PBI-2"]
        with self.assertRaises(ValueError):
            plan.waves(cyclic)


class FindingsAndTriageTests(unittest.TestCase):
    def test_validate_findings_and_filter(self) -> None:
        obj = {
            "schema": "atelier.findings/1",
            "findings": [
                _valid_finding("F-1", severity="critical", confidence=95, kind="correctness"),
                _valid_finding("F-2", severity="major", confidence=80, kind="spec"),
                _valid_finding("F-3", severity="major", confidence=79, kind="correctness"),
                _valid_finding("F-4", severity="minor", confidence=99, kind="style"),
                _valid_finding("F-5", severity="major", confidence=90, kind="style"),
            ],
        }
        self.assertEqual([], plan.validate_findings(obj))
        kept = plan.filter_findings(obj["findings"])
        self.assertEqual([f["id"] for f in kept], ["F-1", "F-2"])

    def test_validate_findings_rejects_bad_shapes(self) -> None:
        self.assertTrue(plan.validate_findings({"schema": "wrong", "findings": []}))

        bad_sev = {"schema": "atelier.findings/1", "findings": [_valid_finding("F-1", severity="blocker")]}
        self.assertTrue(plan.validate_findings(bad_sev))

        bad_conf = {"schema": "atelier.findings/1", "findings": [_valid_finding("F-1", confidence=101)]}
        self.assertTrue(plan.validate_findings(bad_conf))

        bool_line = _valid_finding("F-1")
        bool_line["line"] = True
        self.assertTrue(plan.validate_findings({"schema": "atelier.findings/1", "findings": [bool_line]}))

        bad_kind = {"schema": "atelier.findings/1", "findings": [_valid_finding("F-1", kind="nit")]}
        self.assertTrue(plan.validate_findings(bad_kind))

        dup_id = {"schema": "atelier.findings/1", "findings": [_valid_finding("F-1"), _valid_finding("F-1")]}
        self.assertTrue(plan.validate_findings(dup_id))

    def test_validate_triage(self) -> None:
        triage = {
            "schema": "atelier.triage/1",
            "items": [
                {"id": "F-1", "decision": "fix", "reason": "breaks acceptance criteria"},
                {"id": "F-2", "decision": "skip", "reason": "already handled in caller"},
                {"id": "F-3", "decision": "ask", "reason": "spec leaves edge case open"},
            ],
        }
        self.assertEqual([], plan.validate_triage(triage))
        self.assertEqual([], plan.validate_triage(triage, expected_ids=["F-1", "F-2", "F-3"]))
        self.assertTrue(plan.validate_triage(triage, expected_ids=["F-1", "F-2", "F-3", "F-4"]))

        bad_dec = {
            "schema": "atelier.triage/1",
            "items": [{"id": "F-1", "decision": "defer", "reason": "later"}],
        }
        self.assertTrue(plan.validate_triage(bad_dec))

        no_reason = {
            "schema": "atelier.triage/1",
            "items": [{"id": "F-1", "decision": "fix", "reason": ""}],
        }
        self.assertTrue(plan.validate_triage(no_reason))

    def test_fix_groups_are_file_disjoint_and_bounded(self) -> None:
        findings = [
            _valid_finding("F-1", file="src/a.py"),
            _valid_finding("F-2", file="./src/b.py", files=["src\\a.py"]),
            _valid_finding("F-3", file="src/c.py"),
            _valid_finding("F-4", file="src/d.py"),
            _valid_finding("F-5", file="src/e.py"),
        ]
        groups = plan.fix_groups(findings, max_workers=3)
        self.assertLessEqual(len(groups), 3)
        self.assertEqual(sorted(f["id"] for g in groups for f in g), ["F-1", "F-2", "F-3", "F-4", "F-5"])

        # F-1 and F-2 share src/a.py so they must land in the same group
        f1_group = next(i for i, g in enumerate(groups) if any(f["id"] == "F-1" for f in g))
        f2_group = next(i for i, g in enumerate(groups) if any(f["id"] == "F-2" for f in g))
        self.assertEqual(f1_group, f2_group)

        # No two groups share any normalized file
        group_files: list[set[str]] = []
        for g in groups:
            files = set()
            for item in g:
                for path in [item.get("file"), *(item.get("files") or [])]:
                    if path:
                        files.add(path.replace("\\", "/").removeprefix("./"))
            group_files.append(files)
        for i in range(len(group_files)):
            for j in range(i + 1, len(group_files)):
                self.assertEqual(set(), group_files[i] & group_files[j])

        self.assertEqual([], plan.fix_groups([]))
        with self.assertRaises(ValueError):
            plan.fix_groups(findings, max_workers=0)


class PromptContractTests(unittest.TestCase):
    def test_planner_prompt_modes_and_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spec = root / "spec.md"
            facts = root / "docs" / "pbi" / "results" / "survey.json"
            result = root / "docs" / "pbi" / "results" / "plan.json"

            p_plan = plan.planner_prompt(root, "plan", spec_path=spec, facts_path=facts, result_path=result)
            body = p_plan.read_text(encoding="utf-8")
            self.assertIn("atelier.plan/1", body)
            self.assertIn("open_questions", body)
            self.assertIn(str(spec.resolve()), body)
            self.assertIn(str(facts.resolve()), body)
            self.assertNotIn("grill", body.lower())

            p_revise = plan.planner_prompt(
                root,
                "revise",
                spec_path=spec,
                facts_path=facts,
                result_path=result,
                plan_path=root / "plan.json",
                request_text="Split PBI-1 into two",
            )
            rev_body = p_revise.read_text(encoding="utf-8")
            self.assertIn("revise", rev_body.lower())
            self.assertIn("Split PBI-1 into two", rev_body)

            p_triage = plan.planner_prompt(
                root,
                "triage",
                spec_path=spec,
                facts_path=facts,
                result_path=result,
                findings_path=root / "findings.json",
            )
            tri_body = p_triage.read_text(encoding="utf-8")
            self.assertIn("atelier.triage/1", tri_body)
            self.assertIn("fix", tri_body)
            self.assertIn("skip", tri_body)
            self.assertIn("ask", tri_body)

    def test_worker_prompt_has_only_g12_and_g13_rules(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = root / "docs" / "pbi" / "results" / "worker.json"
            p_pbi = plan.worker_prompt(root, mode="pbi", ticket=_valid_pbi(), result_path=result)
            body = p_pbi.read_text(encoding="utf-8")
            self.assertIn("G12", body)
            self.assertIn("G13", body)
            for forbidden_rule in ("G1 ", "G2 ", "G3 ", "G4 ", "G5 ", "G6 ", "G7 ", "G8 ", "G9 ", "G10", "G11", "G14"):
                self.assertNotIn(forbidden_rule, body)
            self.assertIn("PBI-1", body)

            p_fix = plan.worker_prompt(
                root,
                mode="fix",
                ticket={"id": "FIX-1", "items": [_valid_finding("F-1")], "checks": ["python run_tests.py"]},
                result_path=result,
            )
            fix_body = p_fix.read_text(encoding="utf-8")
            self.assertIn("FIX-1", fix_body)
            self.assertIn("F-1", fix_body)

    def test_reviewer_prompt_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spec = root / "spec.md"
            result = root / "docs" / "pbi" / "results" / "review.json"
            p_rev = plan.reviewer_prompt(root, spec_path=spec, result_path=result)
            body = p_rev.read_text(encoding="utf-8")
            self.assertIn("atelier.findings/1", body)
            self.assertIn("severity", body)
            self.assertIn("confidence", body)
            self.assertIn("style", body.lower())
            self.assertIn("never edit", body.lower())


if __name__ == "__main__":
    unittest.main()
