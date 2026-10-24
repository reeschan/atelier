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
    g.add_argument("--ctx", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cost", action="store_true", help="設計者とサブエージェントの費用を API 換算で出す")
    ap.add_argument("--home", help="単価表 (prices.json) を置いた home のディレクトリ")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.ctx:
        a = analyze(load_requests(cwd_session()))
        print(f"context={a['last_context']} peak={a['peak_context']} requests={a['requests']}")
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
