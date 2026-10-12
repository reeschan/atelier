"""Claude Code の会話記録から、オーケストレーターとサブエージェントのトークン・キャッシュ統計を出す。

会話記録 (~/.claude/projects/<project>/<session>.jsonl と <session>/subagents/agent-*.jsonl) を読むだけで、
何も書き換えない。サブエージェントの PBI は、Agent ツールの description の先頭の "PBI-<番号>" から取る。

使い方:
  python agent_stats.py                       # 今動いているセッション (最後に更新された会話記録)
  python agent_stats.py --session <id>        # セッションを指定
  python agent_stats.py --all                 # PBI のサブエージェントを含む全セッション
  python agent_stats.py --out docs/pbi/stats  # <out>/<session>.json に書く (同じセッションは上書き)
  python agent_stats.py --json                # 要約の代わりに JSON を標準出力へ
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
TTL_5M = 300
TTL_1H = 3600
# 直前のリクエストまでにキャッシュされた量に対して、読めた量がこの割合を下回ったらキャッシュの作り直しとみなす
REBUILD_RATIO = 0.5
PBI_RE = re.compile(r"\bPBI-(\d+)", re.IGNORECASE)
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


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
        prev = r
    first = reqs[0] if reqs else None
    return {
        "requests": len(reqs),
        "started": datetime.fromtimestamp(reqs[0]["ts"]).isoformat(timespec="seconds") if reqs else None,
        "ended": datetime.fromtimestamp(reqs[-1]["ts"]).isoformat(timespec="seconds") if reqs else None,
        "duration_sec": round(reqs[-1]["ts"] - reqs[0]["ts"]) if reqs else 0,
        "tokens": tokens,
        "by_model": by_model,
        "cold_start_write": (first["cache_write_5m"] + first["cache_write_1h"]) if first else 0,
        "cache_hit_ratio": round(tokens["cache_read"] / max(1, tokens["cache_read"] + tokens["input"]
                                 + tokens["cache_write_5m"] + tokens["cache_write_1h"]), 4),
        "gaps": gaps,
        "rebuild": rebuild,
        "rebuild_events": events,
    }


def find_session(session_id):
    hits = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
    if not hits:
        sys.exit(f"セッション {session_id} の会話記録が見つからない")
    return hits[0]


def latest_session():
    files = list(PROJECTS.glob("*/*.jsonl"))
    if not files:
        sys.exit("会話記録が見つからない")
    return max(files, key=lambda p: p.stat().st_mtime)


def pbi_sessions():
    out = []
    for main in PROJECTS.glob("*/*.jsonl"):
        sub = main.with_suffix("") / "subagents"
        if any(PBI_RE.search(json.loads(m.read_text(encoding="utf-8")).get("description", ""))
               for m in sub.glob("*.meta.json")):
            out.append(main)
    return sorted(out, key=lambda p: p.stat().st_mtime)


def session_stats(main_path):
    sid = main_path.stem
    agents = []
    sub = main_path.with_suffix("") / "subagents"
    for jl in sorted(sub.glob("agent-*.jsonl")):
        meta_path = jl.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        desc = meta.get("description", "")
        mt = PBI_RE.search(desc)
        reqs = load_requests(jl)
        if not reqs:
            continue
        agents.append({
            "agent": jl.stem.removeprefix("agent-"),
            "pbi": f"PBI-{int(mt.group(1)):03d}" if mt else None,
            "description": desc,
            "agent_type": meta.get("agentType"),
            "models": sorted({r["model"] for r in reqs}),
            **analyze(reqs),
        })
    orch = analyze(load_requests(main_path))
    totals = {"by_model": {}, "rebuild": {}}
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
    return {
        "session": sid,
        "project_dir": main_path.parent.name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "orchestrator": orch,
        "agents": agents,
        "by_pbi": dict(sorted(by_pbi.items())),
        "totals": totals,
    }


def fmt(n):
    return f"{n:,}"


def summary(s):
    lines = [f"## セッション {s['session']} ({s['project_dir']})", ""]
    lines.append("### モデル別 (オーケストレーター + サブエージェント)")
    lines.append("| モデル | リクエスト | 入力 | 出力 | キャッシュ読み | 書き 5m | 書き 1h |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for model, m in sorted(s["totals"]["by_model"].items()):
        lines.append(f"| {model} | {m['requests']} | {fmt(m['input'])} | {fmt(m['output'])} | "
                     f"{fmt(m['cache_read'])} | {fmt(m['cache_write_5m'])} | {fmt(m['cache_write_1h'])} |")
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
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--session")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.all:
        paths = pbi_sessions()
    elif args.session:
        paths = [find_session(args.session)]
    else:
        paths = [latest_session()]

    results = [session_stats(p) for p in paths]
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
