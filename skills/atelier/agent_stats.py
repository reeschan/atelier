"""Claude Code の会話記録から、オーケストレーターとサブエージェントのトークン・キャッシュ統計を出す。

会話記録 (~/.claude/projects/<project>/<session>.jsonl と <session>/subagents/agent-*.jsonl) を読むだけで、
何も書き換えない。サブエージェントの PBI は、Agent ツールの description の先頭の "PBI-<番号>" から取る。

使い方:
  python agent_stats.py                       # 今動いているセッション (最後に更新された会話記録)
  python agent_stats.py --session <id>        # セッションを指定
  python agent_stats.py --all                 # PBI のサブエージェントを含む全セッション
  python agent_stats.py --out docs/pbi/stats  # <out>/<session>.json に書く (同じセッションは上書き)
  python agent_stats.py --json                # 要約の代わりに JSON を標準出力へ
  python agent_stats.py --ctx                 # 今のディレクトリのセッションの、最後のリクエストの文脈の大きさを 1 行で出す
  python agent_stats.py --ctx --session <id>  # そのセッションの会話記録の文脈の大きさ (headless の設計者用。無ければ context=unknown、終了コード 3)
  python agent_stats.py --ctx --agent <説明の先頭>  # description がそれで始まる最新のサブエージェントの文脈の大きさ (無ければ context=unknown、終了コード 3)
  python agent_stats.py --out <dir> --run <run.json>  # 設計者のセッションを集め、役割ごとの数と費用を <dir>/roles.json に書く
  python agent_stats.py --projects <dir>      # 会話記録の根を差し替える (既定 ~/.claude/projects。どのモードでも効く)
  python agent_stats.py --cost                # 設計者・サブエージェント・advisor の費用を API 換算 (USD) で出す
  python agent_stats.py --cost --home <dir>   # 単価表 (prices.json) を置いた home を指定する

API 換算は参考値で、請求額ではない。
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import common

PROJECTS = Path.home() / ".claude" / "projects"
TTL_5M = 300
TTL_1H = 3600
# 直前のリクエストまでにキャッシュされた量に対して、読めた量がこの割合を下回ったらキャッシュの作り直しとみなす
REBUILD_RATIO = 0.5
PBI_RE = re.compile(r"\bPBI-(\d+)", re.IGNORECASE)
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

# ほかのスクリプトとテストが使っている名前は、common の関数を指す別名として残す
long_path = common.long_path
shown = common.shown
project_dir_for = common.project_dir_for


def read_meta(path):
    """サブエージェントの meta.json を読む。読めなければ警告を出して {} を返す。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"meta を読めない: {shown(path)} ({type(e).__name__})", file=sys.stderr)
        return {}


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_requests(path):
    """jsonl から API リクエスト単位の usage を取り出す。同じ requestId の行は最後のものを使う。"""
    by_id = {}
    order = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = row.get("message")
            if row.get("type") != "assistant" or not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            model = msg.get("model")
            if not usage or not model or model.startswith("<"):
                continue
            rid = row.get("requestId") or row.get("uuid")
            if rid not in by_id:
                order.append(rid)
            cc = usage.get("cache_creation") or {}
            w5 = cc.get("ephemeral_5m_input_tokens")
            w1 = cc.get("ephemeral_1h_input_tokens")
            if w5 is None and w1 is None:
                w5, w1 = usage.get("cache_creation_input_tokens", 0), 0
            by_id[rid] = {
                "ts": parse_ts(row["timestamp"]),
                "model": model,
                "input": usage.get("input_tokens", 0) or 0,
                "output": usage.get("output_tokens", 0) or 0,
                "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                "cache_write_5m": w5 or 0,
                "cache_write_1h": w1 or 0,
            }
    reqs = [by_id[r] for r in order]
    reqs.sort(key=lambda r: r["ts"])
    return reqs


def load_advisor(path):
    """jsonl から advisor (サーバー側の advisor ツール) の呼び出しを取り出す。

    advisor の使用量は、呼び出したリクエストの usage.iterations に type "advisor_message" で入り、
    usage の最上位 (メインのモデルの分) には含まれない。同じ requestId の行は同じ usage を繰り返すので 1 回だけ数える。
    サブエージェントの会話記録には iterations が残らない (2026-10、Claude Code 2.1.296)。その場合は呼び出しの回数だけを
    content の advisor_tool_result から数え、トークンは 0 として扱う (費用は /usage かヘッドレスの modelUsage で見る)。
    """
    by_id = {}
    results = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = row.get("message")
            if not isinstance(msg, dict):
                continue
            for b in msg.get("content") if isinstance(msg.get("content"), list) else []:
                if isinstance(b, dict) and b.get("type") == "advisor_tool_result":
                    results.add(b.get("tool_use_id"))
            if row.get("type") != "assistant":
                continue
            its = (msg.get("usage") or {}).get("iterations") or []
            calls = [it for it in its if isinstance(it, dict) and it.get("type") == "advisor_message"]
            if not calls:
                continue
            rid = row.get("requestId") or row.get("uuid")
            by_id[rid] = [{
                "ts": parse_ts(row["timestamp"]),
                "model": it.get("model") or "advisor",
                "input": it.get("input_tokens", 0) or 0,
                "output": it.get("output_tokens", 0) or 0,
                "cache_read": it.get("cache_read_input_tokens", 0) or 0,
                "cache_write_5m": (it.get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0) or 0,
                "cache_write_1h": (it.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0,
            } for it in calls]
    calls = sorted((c for cs in by_id.values() for c in cs), key=lambda c: c["ts"])
    # 使用量の残っていない呼び出し (サブエージェント) は、回数だけを足す
    for _ in range(max(0, len(results) - len(calls))):
        calls.append({"ts": 0, "model": "advisor (使用量の記録なし)", **{k: 0 for k in TOKEN_KEYS}})
    return calls


def advisor_summary(calls):
    out = {"calls": len(calls), "by_model": {}}
    for c in calls:
        m = out["by_model"].setdefault(c["model"], {"requests": 0, **{k: 0 for k in TOKEN_KEYS}})
        m["requests"] += 1
        for k in TOKEN_KEYS:
            m[k] += c[k]
    return out


def context(r):
    """そのリクエストで送った文脈の大きさ (入力 + キャッシュ読み + キャッシュ書き)。"""
    return r["input"] + r["cache_read"] + r["cache_write_5m"] + r["cache_write_1h"]


def analyze(reqs):
    """リクエスト列からトークン合計・キャッシュの作り直し・リクエスト間隔の分布を出す。"""
    tokens = {k: 0 for k in TOKEN_KEYS}
    by_model = {}
    gaps = {"le_5m": 0, "5m_1h": 0, "gt_1h": 0}
    rebuild = {
        "count": 0, "tokens": 0,
        "ttl_expired": 0, "ttl_expired_tokens": 0,
        # 5 分 TTL で切れたが 1 時間以内だったもの = 1 時間 TTL なら防げた作り直し
        "avoidable_with_1h": 0, "avoidable_with_1h_tokens": 0,
        "other": 0, "other_tokens": 0,
    }
    events = []
    prev = None
    peak = 0
    # 一度でも 1 時間 TTL で書いていたら、その会話は 1 時間 TTL で動いているとみなす
    uses_1h = False
    for r in reqs:
        m = by_model.setdefault(r["model"], {"requests": 0, **{k: 0 for k in TOKEN_KEYS}})
        m["requests"] += 1
        for k in TOKEN_KEYS:
            tokens[k] += r[k]
            m[k] += r[k]
        written = r["cache_write_5m"] + r["cache_write_1h"]
        if prev is not None:
            gap = r["ts"] - prev["ts"]
            gaps["le_5m" if gap <= TTL_5M else "5m_1h" if gap <= TTL_1H else "gt_1h"] += 1
            prev_cached = prev["cache_read"] + prev["cache_write_5m"] + prev["cache_write_1h"]
            if written > 0 and prev_cached > 0 and r["cache_read"] < prev_cached * REBUILD_RATIO:
                ttl = TTL_1H if uses_1h else TTL_5M
                rebuild["count"] += 1
                rebuild["tokens"] += written
                if gap > ttl:
                    cause = "ttl_expired"
                    if ttl == TTL_5M and gap <= TTL_1H:
                        rebuild["avoidable_with_1h"] += 1
                        rebuild["avoidable_with_1h_tokens"] += written
                else:
                    cause = "other"
                rebuild[cause] += 1
                rebuild[cause + "_tokens"] += written
                events.append({"gap_sec": round(gap), "tokens": written, "cause": cause,
                               "model": r["model"], "prev_model": prev["model"]})
        uses_1h = uses_1h or r["cache_write_1h"] > 0
        peak = max(peak, context(r))
        prev = r
    first = reqs[0] if reqs else None
    return {
        "requests": len(reqs),
        "started": datetime.fromtimestamp(reqs[0]["ts"]).isoformat(timespec="seconds") if reqs else None,
        "ended": datetime.fromtimestamp(reqs[-1]["ts"]).isoformat(timespec="seconds") if reqs else None,
        "duration_sec": round(reqs[-1]["ts"] - reqs[0]["ts"]) if reqs else 0,
        "tokens": tokens,
        "by_model": by_model,
        "peak_context": peak,
        "last_context": context(reqs[-1]) if reqs else 0,
        "cold_start_write": (first["cache_write_5m"] + first["cache_write_1h"]) if first else 0,
        "cache_hit_ratio": round(tokens["cache_read"] / max(1, tokens["cache_read"] + tokens["input"]
                                 + tokens["cache_write_5m"] + tokens["cache_write_1h"]), 4),
        "gaps": gaps,
        "rebuild": rebuild,
        "rebuild_events": events,
    }


def cost_of(reqs, prices):
    """リクエスト列の API 換算の費用 (USD)。単価の分からないリクエストは件数だけを数え、費用には足さない。"""
    usd, unpriced = 0.0, 0
    for r in reqs:
        price = common.price_for(prices, r["model"])
        if price is None:
            unpriced += 1
            continue
        usd += sum(r[k] * (price.get(k) or 0) for k in TOKEN_KEYS)
    return {"usd": usd / 1_000_000, "requests": len(reqs), "unpriced": unpriced}


def find_session(session_id):
    hits = list(long_path(PROJECTS).glob(f"*/{session_id}.jsonl"))
    if not hits:
        sys.exit(f"セッション {session_id} の会話記録が見つからない")
    return hits[0]


def cwd_session():
    """今のディレクトリのプロジェクトで最後に更新された会話記録。無ければ全体で最後のもの。"""
    files = list(project_dir_for(Path.cwd()).glob("*.jsonl"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else latest_session()


def latest_session():
    files = list(long_path(PROJECTS).glob("*/*.jsonl"))
    if not files:
        sys.exit("会話記録が見つからない")
    return max(files, key=lambda p: p.stat().st_mtime)


def find_session_quiet(session_id):
    """find_session と同じ探し方で、見つからなければ None を返す (終了しない)。"""
    hits = list(long_path(PROJECTS).glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def agent_metas(prefix):
    """全プロジェクトの subagents から、description が prefix で始まるものの (meta.json, jsonl) を集める。

    入れ子のサブエージェントも同じ subagents/ に平らに入るので、再帰はしない。対になる jsonl が無いものは除く。
    """
    out = []
    for m in long_path(PROJECTS).glob("*/*/subagents/agent-*.meta.json"):
        if not str(read_meta(m).get("description", "")).startswith(prefix):
            continue
        jl = m.with_name(m.name.removesuffix(".meta.json") + ".jsonl")
        if jl.exists():
            out.append((m, jl))
    return out


def find_agent_jsonl(prefix):
    """description が prefix で始まるサブエージェントのうち、jsonl が最後に更新されたもの。無ければ None。"""
    hits = agent_metas(prefix)
    return max(hits, key=lambda h: h[1].stat().st_mtime)[1] if hits else None


def role_of(description, prefix=None):
    """サブエージェントの description から役割 (designer・tester・reviewer・worker・other) を決める。上から順に判定する。

    prefix (この run の `ATELIER.<run>.`) を渡すと、その印を外した残りで役割を決める
    (`ATELIER.<run>.TEST …` のような、run の印を先頭に置いた形)。残りが番号で始まる
    `ATELIER.<run>.<seq> <出番>` は設計者。
    """
    d = description or ""
    if prefix and d.startswith(prefix):
        rest = d[len(prefix):]
        return "designer" if rest[:1].isdigit() else role_of(rest)
    words = d.split()
    first = words[0] if words else ""
    if d.startswith("ATELIER."):
        return "designer"
    if d.startswith("TEST"):
        return "tester"
    if ".review" in d or ".security" in d or first.endswith(".design") or d.startswith("REVIEW"):
        return "reviewer"
    if d.startswith("PBI-"):
        return "worker"
    return "other"


ROLES = ("caller", "designer", "worker", "reviewer", "tester", "other")


def run_prefix(run):
    """run.json の run_id の先頭 8 文字から、設計者の description の印 (`ATELIER.<run>.`) を作る。"""
    return f"ATELIER.{str(run.get('run_id') or '')[:8]}."


def run_sessions(run):
    """run.json の history から設計者のセッションの会話記録を集める。(会話記録, メインの役割) のリスト。

    headless は履歴の session_id のセッション (メインが designer)。
    agent は description が ATELIER.<run_id の先頭 8 文字>. で始まるサブエージェントを持つセッション (メインが caller)。
    """
    found = {}
    for e in run.get("history") or []:
        sid = e.get("session_id") if isinstance(e, dict) else None
        main = find_session_quiet(sid) if sid else None
        if main is not None:
            found.setdefault(main, "designer")
    for m, _ in agent_metas(run_prefix(run)):
        main = m.parent.parent.with_suffix(".jsonl")
        if main.exists():
            found.setdefault(main, "caller")
    return list(found.items())


def run_end(run):
    """run.json の finished_at か aborted_at の時刻 (秒)。どちらも無い・読めないときは None。両方あれば遅いほう。"""
    times = []
    for key in ("finished_at", "aborted_at"):
        v = run.get(key)
        if isinstance(v, str):
            try:
                times.append(parse_ts(v))
            except ValueError:
                pass
    return max(times) if times else None


def caller_run_agents(sub, prefix, end=None):
    """agent のセッションの subagents/ から、この run のサブエージェントを選ぶ。

    入れ子も同じ subagents/ に平らに入る。description が prefix で始まるものと、その子孫 (meta.json の
    parentAgentId をたどる) を選ぶ。親を書かない古い形の meta.json (見本) は、この run の設計者が始まった後に
    動いたものをこの run のものとみなす (この救済は meta に spawnDepth の無い旧形式だけ。spawnDepth のあるものは
    設計者の子孫だけを数える)。end があれば、選んだものの時刻をそこまでで切る。戻り値は (選んだもの [(jsonl, meta, リクエスト)], 設計者の最初の時刻 か None)。
    """
    items = []
    for jl in sorted(sub.glob("agent-*.jsonl")):
        meta_path = jl.with_suffix(".meta.json")
        meta = read_meta(meta_path) if meta_path.exists() else {}
        items.append((jl, meta, load_requests(jl)))
    id_of = lambda jl: jl.stem.removeprefix("agent-")
    qualified = {id_of(jl) for jl, meta, _ in items
                 if str(meta.get("description", "")).startswith(prefix)}
    # 子孫 (この run の設計者から起きた作業者・レビュー担当・テスト担当) をたどる。一巡で足りないことがあるので繰り返す
    changed = True
    while changed:
        changed = False
        for jl, meta, _ in items:
            parent = str(meta.get("parentAgentId") or "").removeprefix("agent-")
            if id_of(jl) not in qualified and parent and parent in qualified:
                qualified.add(id_of(jl))
                changed = True
    first = min((reqs[0]["ts"] for jl, meta, reqs in items if id_of(jl) in qualified and reqs), default=None)
    chosen = []
    for jl, meta, reqs in items:
        if id_of(jl) in qualified:
            chosen.append((jl, meta, reqs))
            continue
        if meta.get("parentAgentId") or "spawnDepth" in meta:
            # 別の run の設計者の子など、親か spawnDepth があるのにこの run の設計者の子孫でないもの
            continue
        desc = str(meta.get("description", ""))
        if first is not None and reqs and reqs[0]["ts"] >= first and not desc.startswith("ATELIER."):
            chosen.append((jl, meta, reqs))
    if end is not None:
        chosen = [(jl, meta, [r for r in reqs if r["ts"] <= end]) for jl, meta, reqs in chosen]
        chosen = [c for c in chosen if c[2]]
    return chosen, first


def caller_requests(reqs, start, end=None):
    """呼び出し側のリクエストのうち、この run の設計者を起動したターン以降のものだけを返す。

    設計者の最初のリクエストより前の最後の呼び出し側リクエストは、その設計者を起動したターンなので含める
    (見本のメインは設計者の 1 秒前にある)。
    """
    cut = start
    before = [r["ts"] for r in reqs if r["ts"] <= cut]
    if before:
        cut = max(before)
    return [r for r in reqs if r["ts"] >= cut and (end is None or r["ts"] <= end)]


def role_stats(sessions, prices, prefix=None, end=None):
    """会話記録 (と、そのサブエージェント) のリクエストを役割ごとに数える。

    呼び出し側のセッション (agent) は、この run の設計者 (description が prefix で始まる) とその子孫だけを数え、
    呼び出し側のリクエストも設計者を起動したターンから数える (前の run や関係ない作業を混ぜない)。
    end (run.json の finished_at か aborted_at) があれば、呼び出し側のリクエストと選んだサブエージェントをその時刻までで切る。
    headless の設計者のセッションは今までどおり、subagents/ の全部を数える。
    """
    roles = {r: [] for r in ROLES}
    for main, main_role in sessions:
        reqs = load_requests(main)
        sub = main.with_suffix("") / "subagents"
        if main_role == "caller" and prefix:
            chosen, first = caller_run_agents(sub, prefix, end)
            if first is not None:
                reqs = caller_requests(reqs, first, end)
            for _, meta, agent_reqs in chosen:
                roles[role_of(meta.get("description", ""), prefix)] += agent_reqs
        else:
            for jl in sorted(sub.glob("agent-*.jsonl")):
                meta_path = jl.with_name(jl.name.removesuffix(".jsonl") + ".meta.json")
                meta = read_meta(meta_path) if meta_path.exists() else {}
                roles[role_of(meta.get("description", ""))] += load_requests(jl)
        roles[main_role] += reqs
    out = {}
    for r, reqs in roles.items():
        c = cost_of(reqs, prices)
        out[r] = {"requests": len(reqs), "tokens": sum(q[k] for q in reqs for k in TOKEN_KEYS),
                  "usd": None if c["unpriced"] else c["usd"]}
    return out


def role_cost(reqs, prices):
    """リクエスト列 1 役割ぶんの requests・トークン 4 種・USD を返す。

    cache_write は 5 分 TTL と 1 時間 TTL の合計 (episode.cost の形)。単価の分からないリクエストが
    混ざった役割は、0 円と取り違えないように usd を None にする。
    """
    c = cost_of(reqs, prices)
    out = {"requests": len(reqs), "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for r in reqs:
        out["input"] += r["input"]
        out["output"] += r["output"]
        out["cache_read"] += r["cache_read"]
        out["cache_write"] += r["cache_write_5m"] + r["cache_write_1h"]
    out["usd"] = None if c["unpriced"] else c["usd"]
    return out


def roles_cost(role_reqs, prices=None):
    """役割ごとのリクエスト列を、リクエストのある役割だけの episode.cost の roles にする。

    prices を省くと、--cost と同じ (同梱 + home) の単価表を使う。
    """
    if prices is None:
        prices = common.load_prices(common.resolve_home(None))
    return {r: role_cost(reqs, prices) for r, reqs in role_reqs.items() if reqs}


def headless_role_requests(main):
    """headless の出番の会話記録を役割ごとのリクエスト列に分ける。

    メインのセッションが designer。subagents/ の子は description から role_of で役割を決める
    (headless の設計者は `ATELIER.<run>.<seq>` の印を持たないので、印を渡さない)。
    """
    roles = {"designer": load_requests(main)}
    sub = main.with_suffix("") / "subagents"
    for jl in sorted(sub.glob("agent-*.jsonl")):
        meta_path = jl.with_name(jl.name.removesuffix(".jsonl") + ".meta.json")
        meta = read_meta(meta_path) if meta_path.exists() else {}
        roles.setdefault(role_of(meta.get("description", "")), []).extend(load_requests(jl))
    return roles


def episode_cost(run, seq, prices=None):
    """1 つの出番 (run.json の seq) の費用を、役割ごとの数と USD で返す。

    形は {"method": headless|transcript|unmeasured, "roles": {<役割>: {requests, input, output,
    cache_read, cache_write, usd}}}。headless の出番 (history の該当行に session_id) は、その
    セッションの会話記録と subagents/ から。agent の出番は、description が
    `ATELIER.<run_id の先頭 8 文字>.<seq> ` で始まる設計者と、その子孫 (parentAgentId をたどる) から。
    設計者は designer、子は role_of の分類 (worker・reviewer・tester・other)。どちらも見つからなければ
    unmeasured。設計者の会話記録は子が終わるまで書き切られないことがあるので、見つかった分だけ数える。
    """
    entry = next((e for e in run.get("history") or [] if isinstance(e, dict) and e.get("seq") == seq), None)
    sid = entry.get("session_id") if entry else None
    if sid:
        main = find_session_quiet(sid)
        if main is not None:
            return {"method": "headless", "roles": roles_cost(headless_role_requests(main), prices)}
    prefix = run_prefix(run)
    episode_prefix = f"{prefix}{seq} "
    # run_id が無ければ設計者の印を作れない (全プロジェクトを無駄に読まない)
    designer = find_agent_jsonl(episode_prefix) if run.get("run_id") else None
    if designer is not None:
        # 設計者は caller_run_agents が episode_prefix の前方一致で選び、子は parentAgentId でたどる。
        # role_of には seq の付かない run の印を渡す (seq 付きだと設計者が other に落ちる)
        sub = designer.parent
        chosen, _ = caller_run_agents(sub, episode_prefix)
        role_reqs = {}
        for _, meta, reqs in chosen:
            role_reqs.setdefault(role_of(meta.get("description", ""), prefix), []).extend(reqs)
        return {"method": "transcript", "roles": roles_cost(role_reqs, prices)}
    return {"method": "unmeasured", "roles": {}}


def roles_table(roles):
    lines = ["| 役割 | リクエスト | トークン | 費用 (USD) |", "|---|---:|---:|---:|"]
    for r in ROLES:
        v = roles[r]
        usd = "不明" if v["usd"] is None else f"{v['usd']:.4f}"
        lines.append(f"| {r} | {fmt(v['requests'])} | {fmt(v['tokens'])} | {usd} |")
    return "\n".join(lines)


def pbi_sessions():
    out = []
    for main in long_path(PROJECTS).glob("*/*.jsonl"):
        sub = main.with_suffix("") / "subagents"
        if any(PBI_RE.search(read_meta(m).get("description", "")) for m in sub.glob("*.meta.json")):
            out.append(main)
    return sorted(out, key=lambda p: p.stat().st_mtime)


def session_stats(main_path, prices=None):
    main_path = long_path(main_path)
    sid = main_path.stem
    agents = []
    sub_cost = {"usd": 0.0, "requests": 0, "unpriced": 0}
    advisor_calls = []
    sub = main_path.with_suffix("") / "subagents"
    for jl in sorted(sub.glob("agent-*.jsonl")):
        meta_path = jl.with_suffix(".meta.json")
        meta = read_meta(meta_path) if meta_path.exists() else {}
        desc = meta.get("description", "")
        mt = PBI_RE.search(desc)
        reqs = load_requests(jl)
        if not reqs:
            continue
        if prices is not None:
            c = cost_of(reqs, prices)
            for k in sub_cost:
                sub_cost[k] += c[k]
        sub_advisor = load_advisor(jl)
        advisor_calls += sub_advisor
        agents.append({
            "agent": jl.stem.removeprefix("agent-"),
            "pbi": f"PBI-{int(mt.group(1)):03d}" if mt else None,
            "description": desc,
            "agent_type": meta.get("agentType"),
            "models": sorted({r["model"] for r in reqs}),
            **analyze(reqs),
            "advisor": advisor_summary(sub_advisor),
        })
    main_reqs = load_requests(main_path)
    orch = analyze(main_reqs)
    main_advisor = load_advisor(main_path)
    advisor_calls += main_advisor
    orch["advisor"] = advisor_summary(main_advisor)
    totals = {"by_model": {}, "rebuild": {}, "advisor": {"calls": 0, "orchestrator_calls": orch["advisor"]["calls"],
                                                         "subagent_calls": 0, "by_model": {}}}
    for part in [orch] + agents:
        adv = part["advisor"]
        totals["advisor"]["calls"] += adv["calls"]
        if part is not orch:
            totals["advisor"]["subagent_calls"] += adv["calls"]
        for model, m in adv["by_model"].items():
            t = totals["advisor"]["by_model"].setdefault(model, {"requests": 0, **{k: 0 for k in TOKEN_KEYS}})
            for k, v in m.items():
                t[k] += v
    for part in [orch] + agents:
        for model, m in part["by_model"].items():
            t = totals["by_model"].setdefault(model, {"requests": 0, **{k: 0 for k in TOKEN_KEYS}})
            for k, v in m.items():
                t[k] += v
        for k, v in part["rebuild"].items():
            totals["rebuild"][k] = totals["rebuild"].get(k, 0) + v
    by_pbi = {}
    for a in agents:
        key = a["pbi"] or "(PBI 以外)"
        p = by_pbi.setdefault(key, {"agents": 0, "models": set(), **{k: 0 for k in TOKEN_KEYS},
                                    "rebuilds": 0, "rebuild_tokens": 0})
        p["agents"] += 1
        p["models"].update(a["models"])
        for k in TOKEN_KEYS:
            p[k] += a["tokens"][k]
        p["rebuilds"] += a["rebuild"]["count"]
        p["rebuild_tokens"] += a["rebuild"]["tokens"]
    for p in by_pbi.values():
        p["models"] = sorted(p["models"])
    result = {
        "session": sid,
        "project_dir": main_path.parent.name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "orchestrator": orch,
        "agents": agents,
        "by_pbi": dict(sorted(by_pbi.items())),
        "totals": totals,
    }
    if prices is not None:
        orch_cost = cost_of(main_reqs, prices)
        # advisor の使用量はメインのモデルの usage に含まれないので、別に換算して全体に足す
        advisor_cost = cost_of(advisor_calls, prices)
        result["cost"] = {
            "prices_checked_at": prices.get("checked_at"),
            "home_prices_checked_at": prices.get("home_checked_at"),
            "orchestrator": orch_cost,
            "subagents": sub_cost,
            "advisor": advisor_cost,
            "total": {k: orch_cost[k] + sub_cost[k] + advisor_cost[k] for k in sub_cost},
        }
    return result


def fmt(n):
    return f"{n:,}"


def summary(s):
    lines = [f"## セッション {s['session']} ({s['project_dir']})", ""]
    parts = [("オーケストレーター", ", ".join(sorted(s["orchestrator"]["by_model"])), s["orchestrator"])] + [
        (a["description"][:40] or a["agent"], ", ".join(a["models"]), a) for a in s["agents"]]
    all_read = max(1, sum(a["tokens"]["cache_read"] for _, _, a in parts))
    lines.append("### エージェント別の読みとターン数 (読みの多い順)")
    lines.append("| 対象 | モデル | リクエスト | キャッシュ読み | 割合 | 1 リクエスト平均 | 最大の文脈 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for name, models, a in sorted(parts, key=lambda x: -x[2]["tokens"]["cache_read"]):
        read, n = a["tokens"]["cache_read"], max(1, a["requests"])
        lines.append(f"| {name} | {models} | {a['requests']} | {fmt(read)} | {read * 100 // all_read}% | "
                     f"{fmt(read // n)} | {fmt(a['peak_context'])} |")
    lines.append("")
    lines.append("### モデル別 (オーケストレーター + サブエージェント)")
    lines.append("| モデル | リクエスト | 入力 | 出力 | キャッシュ読み | 書き 5m | 書き 1h |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for model, m in sorted(s["totals"]["by_model"].items()):
        lines.append(f"| {model} | {m['requests']} | {fmt(m['input'])} | {fmt(m['output'])} | "
                     f"{fmt(m['cache_read'])} | {fmt(m['cache_write_5m'])} | {fmt(m['cache_write_1h'])} |")
    adv = s["totals"].get("advisor") or {"calls": 0}
    lines += ["", "### advisor (メインのモデルとは別の行)"]
    if adv["calls"]:
        lines.append(f"呼び出し {adv['calls']} 回 (オーケストレーター {adv['orchestrator_calls']} / サブエージェント {adv['subagent_calls']})")
        lines.append("| モデル | 呼び出し | 入力 | 出力 | キャッシュ読み |")
        lines.append("|---|---:|---:|---:|---:|")
        for model, m in sorted(adv["by_model"].items()):
            lines.append(f"| {model} | {m['requests']} | {fmt(m['input'])} | {fmt(m['output'])} | {fmt(m['cache_read'])} |")
    else:
        lines.append("呼び出し 0 回")
    lines += ["", "### キャッシュの作り直し", "| 対象 | 回数 | トークン | TTL 切れ | うち 1h TTL なら防げた | その他 | 間隔 ≤5m / 5m–1h / >1h | 読み率 |",
              "|---|---:|---:|---:|---:|---:|---|---:|"]
    rows = [("オーケストレーター", s["orchestrator"])] + [
        (a["description"][:40] or a["agent"], a) for a in s["agents"]]
    for name, a in rows:
        r, g = a["rebuild"], a["gaps"]
        lines.append(f"| {name} | {r['count']} | {fmt(r['tokens'])} | {r['ttl_expired']} | "
                     f"{r['avoidable_with_1h']} ({fmt(r['avoidable_with_1h_tokens'])}) | {r['other']} | "
                     f"{g['le_5m']} / {g['5m_1h']} / {g['gt_1h']} | {a['cache_hit_ratio']:.0%} |")
    lines += ["", "### PBI 別", "| PBI | エージェント | モデル | 入力+書き | 出力 | 読み | 作り直し (トークン) |",
              "|---|---:|---|---:|---:|---:|---|"]
    for pbi, p in s["by_pbi"].items():
        lines.append(f"| {pbi} | {p['agents']} | {', '.join(p['models'])} | "
                     f"{fmt(p['input'] + p['cache_write_5m'] + p['cache_write_1h'])} | {fmt(p['output'])} | "
                     f"{fmt(p['cache_read'])} | {p['rebuilds']} ({fmt(p['rebuild_tokens'])}) |")
    cost = s.get("cost")
    if cost:
        lines += ["", "### API 換算の費用 (参考値で、請求額ではない)",
                  "| 対象 | 費用 (USD) | リクエスト |", "|---|---:|---:|"]
        for label, key in (("設計者", "orchestrator"), ("サブエージェント (合計)", "subagents"), ("advisor", "advisor"),
                           ("全体", "total")):
            c = cost[key]
            lines.append(f"| {label} | {c['usd']:.4f} | {fmt(c['requests'])} |")
        if cost["total"]["unpriced"]:
            lines.append(f"単価不明 {cost['total']['unpriced']} 件 (費用には足していない)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--session")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--ctx", action="store_true")
    ap.add_argument("--agent", help="--ctx と一緒に使う: description がこの文字列で始まる最新のサブエージェントを測る")
    ap.add_argument("--run", help="--out と一緒に使う: run.json のパス。設計者のセッションを集めて役割ごとに分ける")
    ap.add_argument("--projects", help="会話記録の根 (既定 ~/.claude/projects)")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cost", action="store_true", help="設計者とサブエージェントの費用を API 換算で出す")
    ap.add_argument("--home", help="単価表 (prices.json) を置いた home のディレクトリ")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    global PROJECTS
    if args.projects:
        PROJECTS = Path(args.projects)
    if args.ctx and (args.all or (args.session and args.agent)):
        ap.error("--ctx は --all や、--session と --agent の同時指定とは使えない")
    if args.agent and not args.ctx:
        ap.error("--agent は --ctx と一緒に使う")
    if args.run and not args.out:
        ap.error("--run は --out と一緒に使う")

    if args.ctx:
        if args.session:
            path = find_session_quiet(args.session)
        elif args.agent is not None:
            path = find_agent_jsonl(args.agent)
        else:
            path = cwd_session()
        if path is None:
            print("context=unknown")
            sys.exit(3)
        a = analyze(load_requests(path))
        print(f"context={a['last_context']} peak={a['peak_context']} requests={a['requests']}")
        return
    if args.run:
        run = common.load_json(Path(args.run), None)
        if not isinstance(run, dict):
            sys.exit(f"run.json を読めない: {args.run}")
        prices = common.load_prices(common.resolve_home(args.home))
        roles = role_stats(run_sessions(run), prices, run_prefix(run), run_end(run))
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "roles.json").write_text(
            json.dumps({"run_id": run.get("run_id"), "roles": roles}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(roles_table(roles))
        return
    if args.all:
        paths = pbi_sessions()
    elif args.session:
        paths = [find_session(args.session)]
    else:
        paths = [latest_session()]

    prices = common.load_prices(common.resolve_home(args.home)) if args.cost else None
    results = [session_stats(p, prices) for p in paths]
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for s in results:
            (out / f"{s['session']}.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(results if args.all else results[0], ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(summary(s) for s in results))


if __name__ == "__main__":
    main()
