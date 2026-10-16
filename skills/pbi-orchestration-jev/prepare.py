"""起票した PBI に Jev のゲート (brief・pbi_quality) をかけ、select_model.py で担当を決め、表だけを出す。

設計者 (オーケストレーター) が Jev の呼び出し・応答の読み取り・jev-log の記録・決定表の判定を 1 回のコマンドで済ませ、
文脈にはテンプレートの 1 行と最後の表だけが入るようにする (SKILL.md 3〜4 章)。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/prepare.py [docs/pbi] [--skip opencode,codex,agy] [--today YYYY-MM-DD] [--only PBI-003,PBI-004] [--home DIR] [--now ISO]

前提: docs/pbi/PBI-<連番>.md・.facts.json・.brief.md が揃っている。
- jev.config.json が無ければ、このディレクトリの jev.config.template.json をコピーする
- 応答 (docs/pbi/jev/<PBI>.brief.json・.pbi_quality.json) が既にあれば呼び直さない (--only で指定した PBI は呼び直す)
- Jev の呼び出しは並列 (同時 4 件)。失敗したゲートは飛ばし、表の「メモ」に出す (select_model.py は score を 1 として扱う)
- 判定ごとに docs/pbi/jev-log.jsonl に 1 行ずつ追記する (brief の行に route.json の template を足す)
- 表の前に 1 行「優先テンプレート: …」を出し (写しと適用の状態。templates.load_overrides・state_of)、
  表の列「優先テンプレート」に route.json の template.note を出す (無ければ —)
- --home・--now はテンプレートの home (templates.resolve_home) と今の時刻 (templates.parse_time)。select_model.py にもそのまま渡す
"""

import argparse
import json
import shutil
import subprocess
import sys
import templates
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
JEV = HERE / "jev" / "jev.py"  # 同梱の Jev クライアント
QUALITY_LOW = 0.6  # targets_named / off_limits_named / criteria_checkable / commands_runnable がこれ未満なら警告
OUTSIDE_HIGH = 0.4  # needs_outside_context がこれより大きければ警告


def call_jev(config, gate, text_file, out_file):
    r = subprocess.run([sys.executable, str(JEV), str(config), gate, str(text_file)],
                       capture_output=True, text=True, encoding="utf-8")
    out_file.write_text(r.stdout or json.dumps({"error": r.stderr[-300:]}), encoding="utf-8")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return None if r.returncode != 0 or "error" in data else data


def load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return None if "error" in data else data


def answers(data):
    return (data or {}).get("answers") or {}


def quality_warnings(data):
    warns = []
    for key, v in answers(data).items():
        if not isinstance(v, dict):
            continue
        val = v.get("noul", v.get("score"))
        if not isinstance(val, (int, float)):
            continue
        if key == "needs_outside_context":
            if val > OUTSIDE_HIGH:
                warns.append(f"{key} {val:.2f}")
        elif val < QUALITY_LOW:
            warns.append(f"{key} {val:.2f}")
    return warns


def template_line(home, now):
    """表の前に出す 1 行。写し (overrides.json) と適用の状態から決める。"""
    overrides = templates.load_overrides(home)
    application = overrides.get("application")
    if not isinstance(application, dict):
        text = "優先テンプレート: 適用なし"
    else:
        name = application.get("template")
        name = name if isinstance(name, str) else "（名前なし）"
        try:
            state = templates.state_of(application, now)
        except ValueError:
            text = f"優先テンプレート: {name} の適用の時刻が不正"
        else:
            until = application.get("until")
            if state == "active":
                when = "期限なし" if until is None else f"{until} まで、期限切れは {application.get('on_expire')}"
                text = f"優先テンプレート: {name} を適用中 ({when})"
            elif state == "expired_ask":
                text = f"優先テンプレート: {name} は期限切れ。承認で「延長 / やめる / 期限なし」を確認する"
            else:
                text = f"優先テンプレート: {name} は期限切れ (drop)。当てない"
    synced = overrides.get("synced_at")
    return text + " / " + (f"写し: {synced} 時点" if synced else "写し: なし")


def template_note(route):
    """route.json の template.note。template が無ければ —。"""
    tpl = route.get("template")
    return tpl.get("note") if isinstance(tpl, dict) else "—"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", default="docs/pbi")
    ap.add_argument("--skip", default="")
    ap.add_argument("--today")
    ap.add_argument("--only", default="")
    ap.add_argument("--home", help="テンプレートの home (省略時は templates.resolve_home の解決順)")
    ap.add_argument("--now", help="今の時刻 (タイムゾーン付き ISO 8601。省略時は現在時刻)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        now = templates.parse_time(args.now) if args.now else datetime.now().astimezone()
    except ValueError as error:
        sys.exit(f"--now が不正: {error}")
    home = templates.resolve_home(args.home)

    base = Path(args.dir)
    (base / "jev").mkdir(parents=True, exist_ok=True)
    config = base / "jev.config.json"
    if not config.exists():
        shutil.copy(HERE / "jev.config.template.json", config)
    only = {p for p in args.only.split(",") if p}
    pbis = sorted(p.name.removesuffix(".facts.json") for p in base.glob("PBI-*.facts.json"))
    missing = [p for p in pbis if not (base / f"{p}.brief.md").exists() or not (base / f"{p}.md").exists()]
    if missing:
        sys.exit(f"brief.md か PBI 本文が無い: {', '.join(missing)}")

    jobs = []
    for p in pbis:
        for gate, src in (("brief", base / f"{p}.brief.md"), ("pbi_quality", base / f"{p}.md")):
            out = base / "jev" / f"{p}.{gate}.json"
            if p in only or load(out) is None:
                jobs.append((p, gate, src, out))
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda j: call_jev(config, j[1], j[2], j[3]), jobs))

    cmd = [sys.executable, str(HERE / "select_model.py"), "--all", str(base)]
    if args.skip:
        cmd += ["--skip", args.skip]
    if args.today:
        cmd += ["--today", args.today]
    if args.home:
        cmd += ["--home", args.home]
    if args.now:
        cmd += ["--now", args.now]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit(f"select_model.py が失敗した: {r.stderr[-500:]}")

    ts = datetime.now().isoformat(timespec="seconds")
    lines = []
    print(template_line(home, now))
    print("| PBI | 段 | route の先頭 | review | 優先テンプレート | 新規性/結合度/あいまいさ (確信度) | 品質の警告 | メモ |")
    print("|---|---|---|---|---|---|---|---|")
    for p in pbis:
        brief, quality = load(base / "jev" / f"{p}.brief.json"), load(base / "jev" / f"{p}.pbi_quality.json")
        route = json.loads((base / f"{p}.route.json").read_text(encoding="utf-8"))
        facts = json.loads((base / f"{p}.facts.json").read_text(encoding="utf-8"))
        a = answers(brief)
        sc = "/".join(str((a.get(k) or {}).get("score", "-")) for k in ("novelty", "coupling", "ambiguity"))
        conf = min(((a.get(k) or {}).get("confidence", 1.0) for k in ("novelty", "coupling", "ambiguity")), default=1.0)
        warns = quality_warnings(quality)
        memo = []
        if brief is None:
            memo.append("brief の Jev 失敗 (score は 1 扱い)")
        if quality is None:
            memo.append("pbi_quality の Jev 失敗")
        head = route["route"][0]
        head_label = f"{head['runner']} {head['model']}" + (f" {head['effort']}" if head.get("effort") else "")
        print(f"| {p} | {route['tier']} | {head_label} | {route['review']} | {template_note(route)} | {sc} ({conf:.2f}) | "
              f"{', '.join(warns) or 'なし'} | {'; '.join(memo) or '—'} |")
        lines.append({"ts": ts, "pbi": p, "gate": "brief", "scores": {k: (a.get(k) or {}).get("score") for k in a},
                      "confidence": {k: (a.get(k) or {}).get("confidence") for k in a}, "facts": facts,
                      "route": route, "template": route.get("template"), "action": "担当を決定 (prepare.py)"})
        lines.append({"ts": ts, "pbi": p, "gate": "pbi_quality", "warnings": warns,
                      "action": "承認一覧に警告として出す" if warns else "警告なし"})
    with open(base / "jev-log.jsonl", "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
