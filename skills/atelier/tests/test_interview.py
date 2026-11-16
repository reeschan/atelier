import json
import tempfile
import unittest
from pathlib import Path

from atelier import interview


class InterviewTests(unittest.TestCase):
    def _sample_facts(self) -> dict:
        return {
            "schema": "atelier.facts/1",
            "modules": [{"id": "F1", "text": "src/export.py handles exports", "source": "src/export.py:1"}],
            "layers": [],
            "likely_files": [],
            "tests": [],
            "conventions": [],
        }

    def _sample_tree(self) -> dict:
        return {
            "schema": "atelier.tree/1",
            "decided": [{"id": "Q0", "text": "対象は管理画面のエクスポート機能"}],
            "open": [
                {
                    "id": "Q1",
                    "question": "書き出し形式は PDF にしますか？",
                    "recommended": "PDF にする",
                    "depends_on": ["Q0"],
                },
                {
                    "id": "Q2",
                    "question": "保存先は端末のダウンロードにしますか？",
                    "recommended": "端末のダウンロードに入れる",
                    "depends_on": ["Q1"],
                },
                {
                    "id": "Q3",
                    "question": "失敗時は入力値を残しますか？",
                    "recommended": "入力値を残す",
                    "depends_on": [],
                },
            ],
        }

    def test_spec_sections_detects_present_and_missing_sections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            full_en = root / "full_en.md"
            full_en.write_text(
                "# Spec\n\n## Problem Statement\nNeed export.\n\n## Solution\nAdd PDF.\n\n"
                "## User Stories\n1. Export PDF.\n\n## Out of Scope\nCSV.\n",
                encoding="utf-8",
            )
            self.assertEqual([], interview.spec_sections(full_en))

            full_ja = root / "full_ja.md"
            full_ja.write_text(
                "# 仕様\n\n## 目的\n書き出し\n\n## 範囲\nPDF のみ\n\n"
                "## 受け入れ条件\nPDF が保存できる\n\n## 範囲外\nExcel\n",
                encoding="utf-8",
            )
            self.assertEqual([], interview.spec_sections(full_ja))

            partial = root / "partial.md"
            partial.write_text(
                "# Spec\n\n## Problem Statement\nNeed export.\n\n"
                "```markdown\n## Scope\nignored inside code block\n```\n\n"
                "## Out of Scope\nMust not count as Scope.\n",
                encoding="utf-8",
            )
            self.assertEqual(["範囲", "受け入れ条件"], interview.spec_sections(partial))

            self.assertEqual(
                ["目的", "範囲", "受け入れ条件", "範囲外"],
                interview.spec_sections(None),
            )
            self.assertEqual(
                ["目的", "範囲", "受け入れ条件", "範囲外"],
                interview.spec_sections(root / "does-not-exist.md"),
            )

    def test_entry_question_for_complete_incomplete_and_missing_spec(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            complete = root / "complete.md"
            complete.write_text(
                "## 目的\nA\n## 範囲\nB\n## 受け入れ条件\nC\n## 範囲外\nD\n",
                encoding="utf-8",
            )
            q_ok = interview.entry_question(complete)
            self.assertEqual("interview_or_skip", q_ok["kind"])
            self.assertTrue(q_ok["has_spec"])
            self.assertEqual([], q_ok["missing"])
            self.assertEqual("skip", q_ok["recommended"])
            self.assertEqual({"skip", "interview"}, {c["id"] for c in q_ok["choices"]})

            incomplete = root / "incomplete.md"
            incomplete.write_text("## 目的\nA\n## 範囲\nB\n", encoding="utf-8")
            q_inc = interview.entry_question(incomplete)
            self.assertTrue(q_inc["has_spec"])
            self.assertEqual(["受け入れ条件", "範囲外"], q_inc["missing"])
            self.assertEqual("interview", q_inc["recommended"])
            self.assertIn("受け入れ条件", q_inc["question"])
            self.assertIn("範囲外", q_inc["question"])
            self.assertEqual({"skip", "interview"}, {c["id"] for c in q_inc["choices"]})

            q_none = interview.entry_question(None)
            self.assertFalse(q_none["has_spec"])
            self.assertEqual("interview", q_none["recommended"])
            self.assertIn("仕様", q_none["question"])
            self.assertEqual(["interview"], [c["id"] for c in q_none["choices"]])

    def test_validate_tree_enforces_schema_single_line_and_dag(self) -> None:
        tree = self._sample_tree()
        self.assertEqual([], interview.validate_tree(tree))

        bad_schema = {**tree, "schema": "wrong"}
        self.assertTrue(interview.validate_tree(bad_schema))

        extra_root = {**tree, "answers": {"Q1": "leak"}}
        self.assertTrue(interview.validate_tree(extra_root))

        multiline_decided = {
            **tree,
            "decided": [{"id": "Q0", "text": "line1\nline2"}],
        }
        self.assertTrue(interview.validate_tree(multiline_decided))

        answer_in_open = {
            **tree,
            "open": [{**tree["open"][0], "answer": "forged"}],
        }
        self.assertTrue(interview.validate_tree(answer_in_open))

        missing_dep = {
            **tree,
            "open": [{**tree["open"][0], "depends_on": ["Q999"]}],
        }
        self.assertTrue(interview.validate_tree(missing_dep))

        cyclic = {
            "schema": "atelier.tree/1",
            "decided": [],
            "open": [
                {"id": "A", "question": "qA", "recommended": "rA", "depends_on": ["B"]},
                {"id": "B", "question": "qB", "recommended": "rB", "depends_on": ["A"]},
            ],
        }
        self.assertTrue(interview.validate_tree(cyclic))

    def test_frontier_and_apply_answers_use_only_user_text(self) -> None:
        tree = self._sample_tree()
        f1 = interview.frontier(tree)
        self.assertEqual(["Q1", "Q3"], [q["id"] for q in f1])

        updated = interview.apply_answers(tree, {"Q1": "PNG で保存する"})
        # Original tree is not mutated
        self.assertEqual(["Q1", "Q2", "Q3"], [q["id"] for q in tree["open"]])
        # Only Q1 moved to decided with exact user text (not recommended text)
        self.assertEqual(
            [
                {"id": "Q0", "text": "対象は管理画面のエクスポート機能"},
                {"id": "Q1", "text": "PNG で保存する"},
            ],
            updated["decided"],
        )
        # Q3 remained open (not auto-filled from recommended), Q2 is now in frontier
        self.assertEqual(["Q2", "Q3"], [q["id"] for q in updated["open"]])
        self.assertEqual(["Q2", "Q3"], [q["id"] for q in interview.frontier(updated)])

        # G6: interviewer update check refuses modified decided[]
        self.assertTrue(interview.validate_interviewer_tree_update(tree, updated))
        valid_interviewer_update = {
            **tree,
            "open": [
                *tree["open"],
                {"id": "Q4", "question": "権限確認は？", "recommended": "必要", "depends_on": ["Q3"]},
            ],
        }
        self.assertEqual([], interview.validate_interviewer_tree_update(tree, valid_interviewer_update))

        # Empty answer or unknown question ID is rejected
        with self.assertRaises(interview.InterviewError):
            interview.apply_answers(tree, {"Q1": "   "})
        with self.assertRaises(interview.InterviewError):
            interview.apply_answers(tree, {"UNKNOWN": "yes"})

    def test_interviewer_prompt_keeps_cacheable_order_and_minimal_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            facts_path = root / "docs" / "pbi" / "results" / "survey.json"
            facts_path.parent.mkdir(parents=True, exist_ok=True)
            facts_path.write_text(json.dumps(self._sample_facts(), ensure_ascii=False, indent=2), encoding="utf-8")

            secret_goal = root / "goal.txt"
            secret_goal.write_text("UNRELATED_GOAL_MARKER", encoding="utf-8")

            tree = self._sample_tree()
            interview.save_tree(root, tree)

            p1 = interview.interviewer_prompt(root, 1)
            body1 = p1.read_text(encoding="utf-8")

            # Must not leak unrelated files like goal.txt
            self.assertNotIn("UNRELATED_GOAL_MARKER", body1)

            # Strict order: interviewer.md steps -> fact sheet -> design tree
            steps_idx = body1.index("# Interviewer")
            facts_idx = body1.index("## Fact Sheet")
            tree_idx = body1.index("## Design Tree")
            self.assertLess(steps_idx, facts_idx)
            self.assertLess(facts_idx, tree_idx)

            # Round 2 after answering Q1 keeps the prefix up to ## Design Tree byte-identical
            tree2 = interview.apply_answers(tree, {"Q1": "PDF にする"})
            interview.save_tree(root, tree2)
            p2 = interview.interviewer_prompt(root, 2)
            body2 = p2.read_text(encoding="utf-8")

            prefix1 = body1[: body1.index("## Design Tree")]
            prefix2 = body2[: body2.index("## Design Tree")]
            self.assertEqual(prefix1, prefix2)
            self.assertIn("Round: 2", body2)

    def test_interviewer_md_and_to_spec_layout(self) -> None:
        prompt_doc = Path(__file__).resolve().parent.parent / "prompts" / "interviewer.md"
        text = prompt_doc.read_text(encoding="utf-8")
        for keyword in ("frontier", "recommended", "Japanese", "never", "Problem Statement", "Out of Scope", "Further Notes"):
            self.assertIn(keyword.lower(), text.lower())

        spec_ok = "\n".join(f"## {sec}\n内容\n" for sec in interview.TO_SPEC_SECTIONS)
        self.assertEqual([], interview.validate_to_spec(spec_ok))
        self.assertEqual(["Further Notes"], interview.validate_to_spec(spec_ok.replace("## Further Notes", "## Other")))

    def test_multiline_user_answer_collapses_to_one_line_and_format_questions(self) -> None:
        tree = self._sample_tree()
        updated = interview.apply_answers(tree, [{"id": "Q1", "text": "PDF で出す\nA4 縦向き"}])
        self.assertEqual("PDF で出す A4 縦向き", updated["decided"][-1]["text"])
        self.assertEqual([], interview.validate_tree(updated))

        q_text = interview.format_questions(interview.frontier(updated), round_num=2)
        self.assertIn("1. [Q2]", q_text)
        self.assertIn("2. [Q3]", q_text)
        self.assertIn("おすすめの答え", q_text)


if __name__ == "__main__":
    unittest.main()
