import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_stats
import common

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "agent_stats.py"
EXAMPLES = HERE / "examples" / "stats"


def run_cli(*args, cwd=None):
    """agent_stats.py を CLI として外から動かす (内部の関数は呼ばない)。"""
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8",
                          env=dict(os.environ, PYTHONUTF8="1"))


def example_run():
    """見本の run.json。"""
    return json.loads((EXAMPLES / "run.json").read_text(encoding="utf-8"))


def example_session_id():
    """見本の projects にある会話記録の id。"""
    return next((EXAMPLES / "projects").glob("*/*.jsonl")).stem


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

    def test_advisor_calls_are_priced_and_added_to_the_total(self):
        main = row("m1", "2026-10-11T00:00:00Z", model="claude-sonnet-5-5", inp=1000, out=2000)
        main["message"]["usage"]["iterations"] = [
            {"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 300, "output_tokens": 400}]
        sub = row("a1", "2026-10-11T00:01:00Z", inp=100, out=200)
        sub["message"]["content"] = [{"type": "advisor_tool_result", "tool_use_id": "t1"}]
        self.write_session([main], [("a1", "PBI-1 advisor", [sub])])
        (self.home / "prices.json").write_text(json.dumps({"checked_at": "2026-10-20", "models": {}}),
                                               encoding="utf-8")
        prices = self.prices()
        s = agent_stats.session_stats(self.session, prices)
        advisor = usd(prices["models"]["claude-opus-5-5"], input=300, output=400)
        orch = usd(prices["models"]["claude-sonnet-5-5"], input=1000, output=2000)
        sub_usd = usd(prices["models"]["claude-haiku-5-5"], input=100, output=200)
        # 使用量つきの 1 件は opus の単価で換算し、使用量の記録が無いサブエージェントの 1 件は単価不明に数える
        self.assertAlmostEqual(s["cost"]["advisor"]["usd"], advisor)
        self.assertEqual(s["cost"]["advisor"]["requests"], 2)
        self.assertEqual(s["cost"]["advisor"]["unpriced"], 1)
        self.assertAlmostEqual(s["cost"]["total"]["usd"], orch + sub_usd + advisor)
        self.assertEqual(s["cost"]["total"]["unpriced"], 1)
        self.assertEqual(s["cost"]["home_prices_checked_at"], "2026-10-20")
        self.assertIn("advisor", agent_stats.summary(s).split("API 換算")[1])

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
        r"""--all の出力 (project_dir) に \\?\ の接頭辞が残らない。"""
        r = run_cli("--all", "--projects", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.project, r.stdout)
        self.assertNotIn("\\\\?\\", r.stdout)


class CtxCliTest(unittest.TestCase):
    """--ctx の 3 つの入口 (--agent・--session・見つからないとき) を、CLI の出力と終了コードで確かめる。"""

    def setUp(self):
        self.projects = EXAMPLES / "projects"
        self.prefix = "ATELIER." + example_run()["run_id"][:8] + "."

    def write_agent_session(self, projects, folder, sid, aid, w1):
        """projects/<folder>/ に 1 セッションと 1 サブエージェントを作り、agent の jsonl を返す。"""
        sub = projects / folder / sid / "subagents"
        sub.mkdir(parents=True)
        write_jsonl(projects / folder / f"{sid}.jsonl", [row("main", "2026-11-10T00:00:00Z", w1=1)])
        jl = sub / f"agent-{aid}.jsonl"
        write_jsonl(jl, [row(aid, "2026-11-10T00:00:01Z", inp=10, w1=w1)])
        (sub / f"agent-{aid}.meta.json").write_text(
            json.dumps({"description": f"{self.prefix}{aid}"}), encoding="utf-8")
        return jl

    def test_ctx_agent_flat_subagents_and_meta(self):
        """subagents/ に平らに置いた agent-*.jsonl と meta.json から、description の先頭で選んで測る。"""
        r = run_cli("--ctx", "--agent", self.prefix, "--projects", self.projects)
        self.assertEqual(r.returncode, 0, r.stderr)
        # 見本の 1 リクエストは input 10 + cache_read 100 + 書き 0
        self.assertEqual(r.stdout.strip(), "context=110 peak=110 requests=1")

    def test_ctx_session(self):
        """--ctx --session は会話記録の id で測り、--all や --agent との同時指定は使い方の誤り。"""
        sid = example_session_id()
        r = run_cli("--ctx", "--session", sid, "--projects", self.projects)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "context=110 peak=110 requests=1")
        for extra in (["--all"], ["--session", sid, "--agent", self.prefix]):
            bad = run_cli("--ctx", *extra, "--projects", self.projects)
            self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)

    def test_ctx_unknown_exit_3(self):
        """見つからない --agent と --session は context=unknown を出して終了コード 3。"""
        for args in (["--agent", "NOPE"], ["--session", "missing-session"]):
            r = run_cli("--ctx", *args, "--projects", self.projects)
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertEqual(r.stdout.strip(), "context=unknown")

    def test_ctx_agent_in_other_project_folder(self):
        """--agent は今のディレクトリではなく --projects の下の全プロジェクトから、いちばん新しいものを選ぶ。"""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        projects = root / "projects"
        older = self.write_agent_session(projects, "C--old", "11111111-1111-4111-8111-111111111111", "old", w1=1000)
        newer = self.write_agent_session(projects, "C--new", "22222222-2222-4222-8222-222222222222", "new", w1=7000)
        os.utime(older, (1_700_000_000, 1_700_000_000))
        os.utime(newer, (1_700_000_100, 1_700_000_100))
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        r = run_cli("--ctx", "--agent", self.prefix, "--projects", projects, cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stderr)
        # 新しい方 (input 10 + 書き 1h 7000) を選ぶ
        self.assertEqual(r.stdout.strip(), "context=7010 peak=7010 requests=1")

    def test_agent_flag_requires_ctx(self):
        """--agent を --ctx なしで渡すと使い方の誤り (終了コード 2)。"""
        r = run_cli("--agent", self.prefix, "--projects", self.projects)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)


class RunRolesCliTest(unittest.TestCase):
    """--out <dir> --run <run.json> が役割ごとの数と費用を <dir>/roles.json に書くことを CLI で確かめる。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.home = self.dir / "home"
        self.home.mkdir()
        # 見本の claude-sonnet-4-5 は同梱の単価表に無いので、一時 home の単価表で与える
        (self.home / "prices.json").write_text(json.dumps({"checked_at": "2026-12-01", "models": {
            "claude-sonnet-4-5": {"input": 3, "output": 15, "cache_read": 0.3,
                                  "cache_write_5m": 3.75, "cache_write_1h": 6}}}), encoding="utf-8")
        self.per_request = (10 * 3 + 20 * 15 + 100 * 0.3) / 1_000_000  # 見本の 1 リクエストの費用

    def run_roles(self, projects, run, out):
        return run_cli("--out", out, "--run", run, "--projects", projects, "--home", self.home)

    def roles(self, out):
        return json.loads((out / "roles.json").read_text(encoding="utf-8"))

    def add_agent(self, sub, aid, desc):
        """見本と同じ 1 リクエストのサブエージェントを、description を変えて足す。"""
        write_jsonl(sub / f"agent-{aid}.jsonl",
                    [row(aid, "2026-11-10T00:00:09Z", model="claude-sonnet-4-5", inp=10, out=20, read=100)])
        (sub / f"agent-{aid}.meta.json").write_text(json.dumps({"description": desc}), encoding="utf-8")

    def test_out_run_splits_by_role(self):
        """見本の 4 役割を数え、役割の決め方 (TEST/.review/.security/.design/PBI-/その他) を結果で固定する。"""
        out = self.dir / "out"
        r = self.run_roles(EXAMPLES / "projects", EXAMPLES / "run.json", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.roles(out)
        self.assertEqual(data["run_id"], example_run()["run_id"])
        roles = data["roles"]
        for name in ("caller", "designer", "worker", "reviewer"):
            self.assertEqual(roles[name]["requests"], 1, name)
            self.assertEqual(roles[name]["tokens"], 130, name)
            self.assertAlmostEqual(roles[name]["usd"], self.per_request, places=9, msg=name)
        for name in ("tester", "other"):
            self.assertEqual(roles[name]["requests"], 0, name)

        # 役割の決め方を、見本に agent を足した結果から確かめる (description はこの run の印を先頭に置く)
        stats = self.dir / "stats"
        shutil.copytree(EXAMPLES, stats)
        sub = next((stats / "projects").glob("*/*/subagents"))
        prefix = "ATELIER." + example_run()["run_id"][:8] + "."
        for aid, desc in [("t1", f"{prefix}TEST PBI-902 verify"), ("r2", f"{prefix}PBI-902.security review"),
                          ("r3", f"{prefix}PBI-902.design review"), ("r4", f"{prefix}REVIEW PBI-902"),
                          ("p2", f"{prefix}PBI-902 build"), ("o1", f"{prefix}note 調査メモ")]:
            self.add_agent(sub, aid, desc)
        out2 = self.dir / "out2"
        r2 = self.run_roles(stats / "projects", stats / "run.json", out2)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        roles = self.roles(out2)["roles"]
        self.assertEqual({k: v["requests"] for k, v in roles.items()},
                         {"caller": 1, "designer": 1, "worker": 2, "reviewer": 4, "tester": 1, "other": 1})
        self.assertAlmostEqual(roles["tester"]["usd"], self.per_request, places=9)

    def test_out_run_ignores_another_runs_subagents(self):
        """同じセッションに別の run の設計者とその子、前の run の呼び出し側のリクエストがあっても数えない。"""
        stats = self.dir / "stats"
        shutil.copytree(EXAMPLES, stats)
        main = next((stats / "projects").glob("*/*.jsonl"))
        write_jsonl(main, [row("req-old", "2026-11-09T23:00:00Z", model="claude-sonnet-4-5", inp=10, out=20, read=100),
                           json.loads(main.read_text(encoding="utf-8"))])
        sub = next((stats / "projects").glob("*/*/subagents"))
        # 別の run (start --force の前の run など) の設計者と、その子 (この run の印を持たない)
        self.add_agent(sub, "x1", "ATELIER.ffffffff.1 design")
        self.add_agent(sub, "x2", "PBI-902 build")
        (sub / "agent-x2.meta.json").write_text(
            json.dumps({"description": "PBI-902 build", "parentAgentId": "x1"}), encoding="utf-8")
        # この run の設計者 (d1) の子は、印を持たなくても数える
        self.add_agent(sub, "n1", "note nested")
        (sub / "agent-n1.meta.json").write_text(
            json.dumps({"description": "note nested", "parentAgentId": "d1"}), encoding="utf-8")

        out = self.dir / "out"
        r = self.run_roles(stats / "projects", stats / "run.json", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        roles = self.roles(out)["roles"]
        self.assertEqual({k: v["requests"] for k, v in roles.items()},
                         {"caller": 1, "designer": 1, "worker": 1, "reviewer": 1, "tester": 0, "other": 1})

    def test_out_run_session_id_handling(self):
        """history の session_id が無い・数値・見つからない・重なっても落ちず、同じセッションは 1 度だけ数える。"""
        run = example_run()
        sid = example_session_id()
        run["history"] = [
            {"seq": 1, "session_id": None},
            {"seq": 2, "session_id": 90210},
            {"seq": 3, "session_id": "missing-session"},
            "壊れた行",
            {"seq": 4, "session_id": sid},
            {"seq": 5, "session_id": sid},
        ]
        run_path = self.dir / "run.json"
        run_path.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        out = self.dir / "out"
        r = self.run_roles(EXAMPLES / "projects", run_path, out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        roles = self.roles(out)["roles"]
        # 実在する 1 セッション (メイン) + 設計者の agent = designer 2 件。重なった history の行は増やさない
        self.assertEqual(roles["designer"]["requests"], 2)
        self.assertEqual(roles["caller"]["requests"], 0)
        self.assertEqual(roles["worker"]["requests"], 1)
        self.assertEqual(roles["reviewer"]["requests"], 1)


    def test_out_run_cuts_at_finished_or_aborted(self):
        """run.json に finished_at か aborted_at があれば、呼び出し側のリクエストと選んだ子をその時刻までで切る。"""
        for key in ("finished_at", "aborted_at"):
            with self.subTest(key=key):
                stats = self.dir / f"stats-{key}"
                shutil.copytree(EXAMPLES, stats)
                main = next((stats / "projects").glob("*/*.jsonl"))
                late = row("req-late", "2026-11-11T00:00:00Z", model="claude-sonnet-4-5", inp=10, out=20, read=100)
                write_jsonl(main, [json.loads(l) for l in main.read_text(encoding="utf-8").splitlines() if l.strip()]
                            + [late])
                sub = next((stats / "projects").glob("*/*/subagents"))
                prefix = "ATELIER." + example_run()["run_id"][:8] + "."
                write_jsonl(sub / "agent-late.jsonl", [row("late", "2026-11-11T00:00:00Z", model="claude-sonnet-4-5")])
                (sub / "agent-late.meta.json").write_text(
                    json.dumps({"description": f"{prefix}PBI-903 build"}), encoding="utf-8")
                run = example_run()
                run[key] = "2026-11-10T00:01:00Z"
                (stats / "run.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
                out = self.dir / f"out-{key}"
                r = self.run_roles(stats / "projects", stats / "run.json", out)
                self.assertEqual(r.returncode, 0, r.stderr)
                roles = self.roles(out)["roles"]
                self.assertEqual({k: v["requests"] for k, v in roles.items()},
                                 {"caller": 1, "designer": 1, "worker": 1, "reviewer": 1, "tester": 0, "other": 0})

    def test_out_run_fallback_only_for_metas_without_spawn_depth(self):
        """時刻だけで拾う救済は spawnDepth の無い旧形式だけ。spawnDepth があって子孫でないものは数えない。"""
        stats = self.dir / "stats"
        shutil.copytree(EXAMPLES, stats)
        sub = next((stats / "projects").glob("*/*/subagents"))
        self.add_agent(sub, "old1", "旧形式 note")
        self.add_agent(sub, "new1", "Explore unrelated repo")
        (sub / "agent-new1.meta.json").write_text(
            json.dumps({"description": "Explore unrelated repo", "spawnDepth": 1}), encoding="utf-8")
        self.add_agent(sub, "new2", "PBI-777 build other project")
        (sub / "agent-new2.meta.json").write_text(
            json.dumps({"description": "PBI-777 build other project", "spawnDepth": 1}), encoding="utf-8")
        self.add_agent(sub, "kid", "note child")
        (sub / "agent-kid.meta.json").write_text(
            json.dumps({"description": "note child", "spawnDepth": 2, "parentAgentId": "d1"}), encoding="utf-8")
        out = self.dir / "out"
        r = self.run_roles(stats / "projects", stats / "run.json", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        roles = self.roles(out)["roles"]
        # 旧形式 old1 と、設計者の子 kid の 2 件が other。worker は見本の 1 件のまま
        self.assertEqual({k: v["requests"] for k, v in roles.items()},
                         {"caller": 1, "designer": 1, "worker": 1, "reviewer": 1, "tester": 0, "other": 2})


class EpisodeCostTest(unittest.TestCase):
    """episode_cost: 1 つの出番 (run の seq) の役割ごとの数と費用。"""

    RUN_ID = "5a1e0c3d-9b7f-4c2a-8e1d-0f3b6a2c4d5e"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.projects = self.root / "projects"
        patcher = mock.patch.object(agent_stats, "PROJECTS", self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prices = {"models": {
            "claude-haiku-5-5": {"input": 1, "output": 5, "cache_read": 0.1,
                                 "cache_write_5m": 1.25, "cache_write_1h": 2},
            "claude-sonnet-4-5": {"input": 3, "output": 15, "cache_read": 0.3,
                                  "cache_write_5m": 3.75, "cache_write_1h": 6}}}

    def session(self, sid, rows, folder="proj"):
        """projects/<folder>/<sid>.jsonl を書き、そのパスを返す。"""
        path = self.projects / folder / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(path, rows)
        return path

    def agent(self, main, aid, desc, rows, **meta):
        """会話記録の subagents/ に 1 体を足す。meta の値 (parentAgentId など) はそのまま書く。"""
        sub = main.with_suffix("") / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        write_jsonl(sub / f"agent-{aid}.jsonl", rows)
        (sub / f"agent-{aid}.meta.json").write_text(
            json.dumps({"description": desc, **meta}), encoding="utf-8")

    def run(self, history, run_id=None):
        return {"run_id": run_id or self.RUN_ID, "history": history}

    def price(self, model="claude-haiku-5-5"):
        return self.prices["models"][model]

    def test_headless_episode_counts_session_and_subagents(self):
        """history の該当行に session_id があれば、そのセッションと subagents/ を役割ごとに数える。"""
        sid = "11111111-1111-4111-8111-111111111111"
        main = self.session(sid, [row("m1", "2026-11-10T00:00:00Z",
                                      inp=10, out=20, read=100, w5=1000, w1=2000)])
        self.agent(main, "w1", "PBI-1 build", [row("w1", "2026-11-10T00:01:00Z",
                                                   inp=1, out=2, read=3, w5=40, w1=50)])
        self.agent(main, "t1", "TEST PBI-1 verify", [row("t1", "2026-11-10T00:02:00Z",
                                                         inp=4, out=5, read=6, w5=7, w1=8)])
        run = self.run([{"seq": 1, "session_id": sid}, {"seq": 2, "session_id": "other"}])
        data = agent_stats.episode_cost(run, 1, self.prices)
        self.assertEqual(data["method"], "headless")
        p = self.price()
        # cache_write は 5m と 1h の合計
        self.assertEqual(data["roles"]["designer"], {
            "requests": 1, "input": 10, "output": 20, "cache_read": 100, "cache_write": 3000,
            "usd": usd(p, input=10, output=20, cache_read=100, cache_write_5m=1000, cache_write_1h=2000)})
        self.assertEqual(data["roles"]["worker"], {
            "requests": 1, "input": 1, "output": 2, "cache_read": 3, "cache_write": 90,
            "usd": usd(p, input=1, output=2, cache_read=3, cache_write_5m=40, cache_write_1h=50)})
        self.assertEqual(data["roles"]["tester"], {
            "requests": 1, "input": 4, "output": 5, "cache_read": 6, "cache_write": 15,
            "usd": usd(p, input=4, output=5, cache_read=6, cache_write_5m=7, cache_write_1h=8)})
        self.assertEqual(set(data["roles"]), {"designer", "worker", "tester"})

    def test_agent_episode_splits_designer_and_descendants(self):
        """agent の出番は、その seq の設計者と子孫だけを役割に分け、呼び出し側は数えない。"""
        sid = "22222222-2222-4222-8222-222222222222"
        main = self.session(sid, [row("caller", "2026-11-10T00:00:00Z", inp=999, out=999, read=999)])
        p = self.price()
        self.agent(main, "d2", f"ATELIER.{self.RUN_ID[:8]}.2 plan",
                   [row("d2", "2026-11-10T00:00:01Z", inp=10, out=20, read=100)])
        self.agent(main, "w1", "PBI-1 build", [row("w1", "2026-11-10T00:00:02Z", inp=1, out=2, read=3)],
                   parentAgentId="d2")
        self.agent(main, "r1", "PBI-1.review build", [row("r1", "2026-11-10T00:00:03Z", inp=4, out=5, read=6)],
                   parentAgentId="d2")
        self.agent(main, "t1", "TEST PBI-1 verify", [row("t1", "2026-11-10T00:00:04Z", inp=7, out=8, read=9)],
                   parentAgentId="w1")
        self.agent(main, "o1", "note research", [row("o1", "2026-11-10T00:00:05Z", inp=11, out=12, read=13)],
                   parentAgentId="d2")
        # 同じセッションの別の出番 (seq 1) の設計者と、その子。seq 2 には数えない
        self.agent(main, "d1", f"ATELIER.{self.RUN_ID[:8]}.1 design",
                   [row("d1", "2026-11-09T00:00:00Z", inp=100, out=100, read=100)])
        self.agent(main, "x1", "PBI-9 build", [row("x1", "2026-11-09T00:00:01Z", inp=100, out=100, read=100)],
                   parentAgentId="d1")
        data = agent_stats.episode_cost(self.run([{"seq": 2}]), 2, self.prices)
        self.assertEqual(data["method"], "transcript")
        self.assertEqual({r: v["requests"] for r, v in data["roles"].items()},
                         {"designer": 1, "worker": 1, "reviewer": 1, "tester": 1, "other": 1})
        self.assertEqual(data["roles"]["designer"], {
            "requests": 1, "input": 10, "output": 20, "cache_read": 100, "cache_write": 0,
            "usd": usd(p, input=10, output=20, cache_read=100)})
        self.assertEqual(data["roles"]["tester"]["input"], 7)
        # 子が worker の子 (tester) でも、呼び出し側 (main) のリクエストは数えない
        self.assertEqual(sum(v["requests"] for v in data["roles"].values()), 5)

    def test_unmeasured_when_neither_transcript_nor_designer(self):
        """session_id の会話記録も設計者の meta も無ければ unmeasured。"""
        other = "33333333-3333-4333-8333-333333333333"
        self.session(other, [row("m1", "2026-11-10T00:00:00Z")])
        for run, seq in [(self.run([]), 1),
                         (self.run([{"seq": 1}]), 1),
                         (self.run([{"seq": 1, "session_id": "missing-session"}]), 1),
                         (self.run([{"seq": 2, "session_id": other}]), 1),  # 別の seq の行は見ない
                         (self.run([{"seq": 1, "session_id": 90210}]), 1)]:
            with self.subTest(seq=seq):
                self.assertEqual(agent_stats.episode_cost(run, seq, self.prices),
                                 {"method": "unmeasured", "roles": {}})
        self.assertEqual(agent_stats.episode_cost({"run_id": self.RUN_ID}, 1, self.prices),
                         {"method": "unmeasured", "roles": {}})

    def test_role_with_unpriced_model_has_null_usd(self):
        """単価の分からないモデルが混ざった役割だけ usd を null にし、ほかの役割は換算する。"""
        sid = "44444444-4444-4444-8444-444444444444"
        main = self.session(sid, [row("m1", "2026-11-10T00:00:00Z", inp=10, out=20, read=100)])
        self.agent(main, "w1", "PBI-1 build", [
            row("k1", "2026-11-10T00:01:00Z", inp=10, out=20),
            row("k2", "2026-11-10T00:01:01Z", model="mystery-9", inp=30, out=40)])
        data = agent_stats.episode_cost(self.run([{"seq": 1, "session_id": sid}]), 1, self.prices)
        worker = data["roles"]["worker"]
        self.assertIsNone(worker["usd"])
        self.assertEqual((worker["requests"], worker["input"], worker["output"]), (2, 40, 60))
        self.assertIsInstance(data["roles"]["designer"]["usd"], float)

    def test_prices_default_to_the_shared_table(self):
        """prices を省くと --cost と同じ単価表 (同梱 + home) で換算する。"""
        sid = "55555555-5555-4555-8555-555555555555"
        self.session(sid, [row("m1", "2026-11-10T00:00:00Z", inp=10, out=20, read=100)])
        with mock.patch.object(agent_stats.common, "load_prices", return_value=self.prices):
            data = agent_stats.episode_cost(self.run([{"seq": 1, "session_id": sid}]), 1)
        self.assertIsInstance(data["roles"]["designer"]["usd"], float)
        self.assertGreater(data["roles"]["designer"]["usd"], 0)


if __name__ == "__main__":
    unittest.main()
