import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_stats
import common


def row(rid, ts, model="claude-haiku-5-5", inp=2, out=10, read=0, w5=0, w1=0):
    return {"type": "assistant", "requestId": rid, "timestamp": ts,
            "message": {"model": model, "usage": {
                "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                "cache_creation_input_tokens": w5 + w1,
                "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}}}


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def usd(price, **tokens):
    """単価 (USD / 100 万トークン) とトークン数から、read_agent と同じ式で費用を出す。"""
    return sum(tokens.get(k, 0) * (price.get(k) or 0) for k in agent_stats.TOKEN_KEYS) / 1_000_000


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


class CostTest(unittest.TestCase):
    """--cost の API 換算。home は一時ディレクトリを渡し、本物の home を読まない。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = self.root / "home"
        self.home.mkdir()
        self.session = self.root / "projects" / "proj" / "s1.jsonl"
        self.session.parent.mkdir(parents=True)
        self.sub = self.session.with_suffix("") / "subagents"
        self.sub.mkdir(parents=True)

    def write_session(self, main_rows, agents=()):
        write_jsonl(self.session, main_rows)
        for aid, desc, rows in agents:
            write_jsonl(self.sub / f"agent-{aid}.jsonl", rows)
            (self.sub / f"agent-{aid}.meta.json").write_text(json.dumps({"description": desc}), encoding="utf-8")

    def prices(self):
        return common.load_prices(self.home)

    def one_agent_session(self, main_model="claude-opus-5-5", sub_model="claude-haiku-5-5"):
        self.write_session(
            [row("m1", "2026-10-11T00:00:00Z", model=main_model,
                 inp=1000, out=2000, read=3000, w5=4000, w1=5000)],
            [("a1", "PBI-1 cost", [row("a1", "2026-10-11T00:01:00Z", model=sub_model,
                                      inp=100, out=200, read=300, w5=400, w1=500)])])

    def test_cost_is_split_into_orchestrator_and_subagents(self):
        self.one_agent_session()
        prices = self.prices()
        s = agent_stats.session_stats(self.session, prices)
        orch = usd(prices["models"]["claude-opus-5-5"],
                   input=1000, output=2000, cache_read=3000, cache_write_5m=4000, cache_write_1h=5000)
        sub = usd(prices["models"]["claude-haiku-5-5"],
                  input=100, output=200, cache_read=300, cache_write_5m=400, cache_write_1h=500)
        self.assertAlmostEqual(s["cost"]["orchestrator"]["usd"], orch)
        self.assertAlmostEqual(s["cost"]["subagents"]["usd"], sub)
        self.assertAlmostEqual(s["cost"]["total"]["usd"], orch + sub)
        self.assertEqual(s["cost"]["orchestrator"]["requests"], 1)
        self.assertEqual(s["cost"]["subagents"]["requests"], 1)
        text = agent_stats.summary(s)
        self.assertIn("設計者", text)
        self.assertIn("サブエージェント (合計)", text)
        self.assertIn("全体", text)
        self.assertIn(f"{orch + sub:.4f}", text)

    def test_home_prices_override_bundled_prices(self):
        self.one_agent_session()
        (self.home / "prices.json").write_text(json.dumps({"checked_at": "2026-10-20", "models": {
            "claude-haiku-5-5": {"input": 10, "output": 20, "cache_read": 1,
                                 "cache_write_5m": 2, "cache_write_1h": 3}}}), encoding="utf-8")
        prices = self.prices()
        s = agent_stats.session_stats(self.session, prices)
        overridden = usd(prices["models"]["claude-haiku-5-5"],
                         input=100, output=200, cache_read=300, cache_write_5m=400, cache_write_1h=500)
        bundled = usd(common.load_prices(self.root / "missing-home")["models"]["claude-haiku-5-5"],
                      input=100, output=200, cache_read=300, cache_write_5m=400, cache_write_1h=500)
        self.assertAlmostEqual(s["cost"]["subagents"]["usd"], overridden)
        self.assertNotAlmostEqual(s["cost"]["subagents"]["usd"], bundled)
        self.assertEqual(prices["home_checked_at"], "2026-10-20")
        self.assertEqual(s["cost"]["prices_checked_at"], prices["checked_at"])

    def test_unknown_price_is_counted_and_not_added(self):
        self.write_session(
            [row("m1", "2026-10-11T00:00:00Z", model="claude-opus-5-5", inp=1000, out=2000)],
            [("a1", "PBI-1 known", [row("a1", "2026-10-11T00:01:00Z", model="claude-haiku-5-5", inp=100, out=200)]),
             ("a2", "PBI-1 unknown", [row("a2", "2026-10-11T00:02:00Z", model="mystery-9", inp=900000, out=900000)])])
        prices = self.prices()
        s = agent_stats.session_stats(self.session, prices)
        orch = usd(prices["models"]["claude-opus-5-5"], input=1000, output=2000)
        known_sub = usd(prices["models"]["claude-haiku-5-5"], input=100, output=200)
        self.assertEqual(s["cost"]["subagents"]["unpriced"], 1)
        self.assertEqual(s["cost"]["total"]["unpriced"], 1)
        self.assertAlmostEqual(s["cost"]["subagents"]["usd"], known_sub)
        self.assertAlmostEqual(s["cost"]["total"]["usd"], orch + known_sub)
        self.assertIn("単価不明 1 件", agent_stats.summary(s))

    def test_without_cost_output_is_unchanged(self):
        self.one_agent_session()
        plain = agent_stats.session_stats(self.session)
        priced = agent_stats.session_stats(self.session, self.prices())
        priced.pop("cost")
        plain.pop("generated_at")
        priced.pop("generated_at")
        self.assertEqual(plain, priced)
        self.assertNotIn("cost", agent_stats.session_stats(self.session))
        self.assertNotIn("API 換算", agent_stats.summary(agent_stats.session_stats(self.session)))

    def test_main_json_includes_cost_and_text_shows_breakdown(self):
        self.one_agent_session()
        argv = ["agent_stats.py", "--cost", "--json", "--home", str(self.home)]
        with mock.patch.object(agent_stats, "latest_session", return_value=self.session), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            agent_stats.main()
        data = json.loads(out.getvalue())
        self.assertIn("cost", data)
        self.assertEqual(data["cost"]["total"]["requests"], 2)

        argv = ["agent_stats.py", "--cost", "--home", str(self.home)]
        with mock.patch.object(agent_stats, "latest_session", return_value=self.session), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            agent_stats.main()
        text = out.getvalue()
        self.assertIn("API 換算の費用", text)
        self.assertIn("| 設計者 |", text)
        self.assertIn("| サブエージェント (合計) |", text)
        self.assertIn("| 全体 |", text)


@unittest.skipUnless(os.name == "nt", "260 文字の上限は Windows だけ")
class LongPathTest(unittest.TestCase):
    """フォルダを選ばずに始めたセッションのように、会話記録のパスが 260 文字を超えても統計に入る。"""

    def setUp(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.root = root
        self.addCleanup(shutil.rmtree, "\\\\?\\" + str(root))
        self.project = "C--Users-alice-scratch-" + "x" * 200
        # 作るときだけ \\?\ を直接付ける (テスト対象の long_path を使わない)
        proj = Path("\\\\?\\" + str(root / self.project))
        sub = proj / "s1" / "subagents"
        sub.mkdir(parents=True)
        write_jsonl(proj / "s1.jsonl", [row("m1", "2026-10-11T00:00:00Z", model="claude-opus-5-5", w1=5000)])
        write_jsonl(sub / "agent-a1.jsonl", [row("a1", "2026-10-11T00:01:00Z", w5=100)])
        (sub / "agent-a1.meta.json").write_text(json.dumps({"description": "PBI-7 long path"}), encoding="utf-8")
        self.meta = root / self.project / "s1" / "subagents" / "agent-a1.meta.json"
        self.assertGreater(len(str(self.meta)), 260)

    def test_all_includes_long_path_agent(self):
        with mock.patch.object(agent_stats, "PROJECTS", self.root):
            paths = agent_stats.pbi_sessions()
        self.assertEqual(len(paths), 1)
        s = agent_stats.session_stats(paths[0])
        self.assertEqual(s["project_dir"], self.project)
        self.assertEqual(s["by_pbi"]["PBI-007"]["agents"], 1)
        self.assertEqual(s["totals"]["by_model"]["claude-haiku-5-5"]["cache_write_5m"], 100)

    def test_session_stats_accepts_plain_long_path(self):
        s = agent_stats.session_stats(self.root / self.project / "s1.jsonl")
        self.assertEqual(s["by_pbi"]["PBI-007"]["agents"], 1)

    def test_shown_strips_prefix(self):
        self.assertEqual(agent_stats.shown(agent_stats.long_path(self.meta)), str(self.meta))


if __name__ == "__main__":
    unittest.main()
