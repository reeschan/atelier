import json
import tempfile
import unittest
from pathlib import Path

import agent_stats


def row(rid, ts, model="claude-haiku-5-5", inp=2, out=10, read=0, w5=0, w1=0):
    return {"type": "assistant", "requestId": rid, "timestamp": ts,
            "message": {"model": model, "usage": {
                "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                "cache_creation_input_tokens": w5 + w1,
                "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}}}


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def analyze(self, rows):
        p = self.dir / "a.jsonl"
        write_jsonl(p, rows)
        return agent_stats.analyze(agent_stats.load_requests(p))

    def test_same_request_id_counted_once_using_last_row(self):
        a = self.analyze([row("r1", "2026-10-11T00:00:00Z", out=2, w5=1000),
                          row("r1", "2026-10-11T00:00:01Z", out=300, w5=1000)])
        self.assertEqual(a["requests"], 1)
        self.assertEqual(a["tokens"]["output"], 300)
        self.assertEqual(a["tokens"]["cache_write_5m"], 1000)

    def test_rebuild_after_5m_gap_is_avoidable_with_1h(self):
        a = self.analyze([row("r1", "2026-10-11T00:00:00Z", w5=10000),
                          row("r2", "2026-10-11T00:01:00Z", read=10000, w5=500),
                          row("r3", "2026-10-11T00:11:00Z", read=0, w5=10600)])
        self.assertEqual(a["rebuild"]["count"], 1)
        self.assertEqual(a["rebuild"]["ttl_expired"], 1)
        self.assertEqual(a["rebuild"]["avoidable_with_1h"], 1)
        self.assertEqual(a["rebuild"]["avoidable_with_1h_tokens"], 10600)
        self.assertEqual(a["gaps"], {"le_5m": 1, "5m_1h": 1, "gt_1h": 0})

    def test_10m_gap_with_1h_ttl_is_not_ttl_expiry(self):
        a = self.analyze([row("r1", "2026-10-11T00:00:00Z", w1=10000),
                          row("r2", "2026-10-11T00:10:00Z", read=0, w1=10000)])
        self.assertEqual(a["rebuild"]["count"], 1)
        self.assertEqual(a["rebuild"]["other"], 1)
        self.assertEqual(a["rebuild"]["avoidable_with_1h"], 0)

    def test_normal_growth_is_not_rebuild(self):
        a = self.analyze([row("r1", "2026-10-11T00:00:00Z", w5=10000),
                          row("r2", "2026-10-11T00:20:00Z", read=10000, w5=2000)])
        self.assertEqual(a["rebuild"]["count"], 0)
        self.assertEqual(a["cold_start_write"], 10000)

    def test_by_model_split(self):
        a = self.analyze([row("r1", "2026-10-11T00:00:00Z", model="claude-opus-5-5", out=5),
                          row("r2", "2026-10-11T00:00:10Z", model="claude-sonnet-5-5", out=7)])
        self.assertEqual(a["by_model"]["claude-opus-5-5"]["output"], 5)
        self.assertEqual(a["by_model"]["claude-sonnet-5-5"]["output"], 7)

    def test_context_sizes(self):
        a = self.analyze([row("r1", "2026-10-11T00:00:00Z", inp=1, w1=10000),
                          row("r2", "2026-10-11T00:01:00Z", inp=1, read=10000, w1=30000),
                          row("r3", "2026-10-11T00:02:00Z", inp=1, read=20000, w1=100)])
        self.assertEqual(a["peak_context"], 40001)
        self.assertEqual(a["last_context"], 20101)


class SessionTest(unittest.TestCase):
    def test_session_groups_agents_by_pbi(self):
        root = Path(tempfile.mkdtemp())
        main = root / "s1.jsonl"
        write_jsonl(main, [row("m1", "2026-10-11T00:00:00Z", model="claude-opus-5-5", w1=5000)])
        sub = root / "s1" / "subagents"
        sub.mkdir(parents=True)
        for aid, desc in [("a1", "PBI-3 first try"), ("a2", "PBI-003 rework"), ("a3", "research")]:
            write_jsonl(sub / f"agent-{aid}.jsonl", [row(aid, "2026-10-11T00:01:00Z", w5=100)])
            (sub / f"agent-{aid}.meta.json").write_text(json.dumps({"description": desc}), encoding="utf-8")
        s = agent_stats.session_stats(main)
        self.assertEqual(s["by_pbi"]["PBI-003"]["agents"], 2)
        self.assertEqual(s["by_pbi"]["(PBI 以外)"]["agents"], 1)
        self.assertEqual(s["totals"]["by_model"]["claude-haiku-5-5"]["cache_write_5m"], 300)
        self.assertEqual(s["totals"]["by_model"]["claude-opus-5-5"]["cache_write_1h"], 5000)
        text = agent_stats.summary(s)
        self.assertIn("エージェント別の読みとターン数", text)
        self.assertIn("| オーケストレーター | claude-opus-5-5 | 1 |", text)

    def test_project_dir_for(self):
        p = agent_stats.project_dir_for("C:/Users/alice/dev/my-app")
        self.assertEqual(p.name, "C--Users-alice-dev-my-app")


if __name__ == "__main__":
    unittest.main()
