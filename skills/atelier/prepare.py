"""起票した PBI に Jev のゲート (brief・pbi_quality) をかけ、select_model.py で担当を決め、表だけを出す。

設計者 (オーケストレーター) が Jev の呼び出し・応答の読み取り・jev-log の記録・決定表の判定を 1 回のコマンドで済ませ、
文脈にはテンプレートの 1 行と最後の表だけが入るようにする (appendix/plan.md)。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/prepare.py [docs/pbi] [--skip opencode,codex,agy,dsh] [--today YYYY-MM-DD] [--only PBI-003,PBI-004] [--tier haiku|sonnet|opus] [--home DIR] [--now ISO]

前提: docs/pbi/PBI-<連番>.md・.facts.json・.brief.md が揃っている。
- jev.config.json が無ければ、このディレクトリの jev.config.template.json をコピーする
- 応答 (docs/pbi/jev/<PBI>.brief.json・.pbi_quality.json) が既にあれば呼び直さない (--only で指定した PBI は呼び直す。--tier と一緒のときは呼び直さない)
- sensitive な PBI (facts の sensitive が true) には pbi_quality を送らず、brief だけを送る (本文を外部に出さない)
- Jev の呼び出しは並列 (同時 4 件)。失敗したゲートは飛ばし、表の「メモ」に code 付きで出す
  (例: brief の Jev 失敗 (no-key) (score は 1 扱い)。select_model.py は score を 1 として扱う)
- jev-log.jsonl には、PBI の判定 (段・route・template の note・補正後の値・品質の警告・dropped) が、
  同じ PBI・ゲート brief の前回の行と変わったときだけ 1 行書く (変わらなければ書かない。ts はタイムゾーン付き)
- 表の前に 1 行「優先テンプレート: …」を出し (写しと適用の状態。templates.load_overrides・state_of)、
  その次に Jev の 1 行を出す。この実行で呼んだゲートが全部成功なら「Jev: ok n/n (<model>)」、
  1 件でも失敗なら「Jev: unavailable (<code>)」。呼び直さず応答を再利用しただけなら
  「Jev: not-checked (cached …)」（今の疎通の根拠にしない）。model は応答の model。無ければ unknown。
  表の列「優先テンプレート」に route.json の template.note を出す (無ければ —)
- 表の列「補正後の値」は route.json の scores、「理由」は reasons[0] から「決定: 」を除いたもの、
  「外した手順」は route.json の dropped を `<runner> <model> (<理由>)` の `; ` つなぎにする。
  「route の先頭」は common.step_label
- select_model.py には準備する PBI を常に --only で渡す (--only の指定があればそれと route.json がまだ無い PBI。
  --tier のときは指定だけ。指定が無ければ全部)。route.json に override (goal で指定した route) があれば select_model.py がそのまま残す
- --tier は --only と一緒のときだけ受け付け、select_model.py に --only と一緒に渡す (tier up で route.json を書き直す)
- --home・--now はテンプレートの home (templates.resolve_home) と今の時刻 (templates.parse_time)。select_model.py にもそのまま渡す
"""

import argparse
import json
import shutil
import subprocess
import sys
import common
import templates
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Windows で子のコンソールのウインドウを出さない (ヘッドレス)。ほかの OS では 0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

HERE = Path(__file__).resolve().parent
JEV = HERE / "jev" / "jev.py"  # 同梱の Jev クライアント
QUALITY_LOW = 0.6  # targets_named / off_limits_named / criteria_checkable / commands_runnable がこれ未満なら警告
OUTSIDE_HIGH = 0.4  # needs_outside_context がこれより大きければ警告


def call_jev(config, gate, text_file, out_file):
    r = subprocess.run([sys.executable, str(JEV), str(config), gate, str(text_file)],
                       capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW)
    out_file.write_text(
        r.stdout or json.dumps({"error": (r.stderr or "")[-300:], "code": "unknown", "where": None}),
        encoding="utf-8")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return None if r.returncode != 0 or "error" in data else data


def load(path):
    """JSON を読む。読めない・JSON でない・error のキーがあるときは None。"""
    data = common.load_json(path)
    return None if isinstance(data, dict) and "error" in data else data


def jev_failure_code(data):
    """Jev の応答が使えるなら None。失敗なら code（無ければ unknown）。ファイルが無いときは 未実行。"""
    if isinstance(data, dict) and "error" not in data and isinstance(data.get("answers"), dict):
        return None
    if not isinstance(data, dict):
        return "未実行"
    code = data.get("code")
    return code if isinstance(code, str) and code else "unknown"


def jev_status_line(records, checked):
    """表の前の 1 行。checked はこの実行で Jev を呼んだゲートか。

    呼んで全部成功なら Jev: ok n/n (<model>)。1 件でも失敗なら Jev: unavailable (<code>)。
    呼ばずに再利用しただけなら Jev: not-checked (cached …)。それは今の疎通ではない。
    """
    total = len(records)
    models = []
    codes = []
    ok = 0
    for data in records:
        code = jev_failure_code(data)
        if code is None:
            ok += 1
            model = data.get("model") if isinstance(data, dict) else None
            if isinstance(model, str) and model and model not in models:
                models.append(" ".join(model.split()))
        elif code not in codes:
            codes.append(code)
    if total and not codes:
        body = f"{ok}/{total} ({', '.join(models) if models else 'unknown'})"
        return f"Jev: ok {body}" if checked else f"Jev: not-checked (cached {body})"
    code = ", ".join(codes) if codes else "unknown"
    return f"Jev: unavailable ({code})" if checked else f"Jev: not-checked (cached {code})"


def failure_memo(name, data, with_score=False):
    """失敗したゲートのメモ。成功なら None。code を括弧に入れる。"""
    code = jev_failure_code(data)
    if code is None:
        return None
    text = f"{name} の Jev 失敗 ({code})"
    if with_score:
        text += " (score は 1 扱い)"
    return text


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


def score_text(route):
    """表の「補正後の値」。route.json の scores を 新規性/結合度/あいまいさ の順に並べる。"""
    scores = route.get("scores") or {}
    return "/".join(str(scores.get(k, "-")) for k in ("novelty", "coupling", "ambiguity"))


def reason_text(route):
    """表の「理由」。route.json の reasons[0] から先頭の「決定: 」を除いたもの。"""
    reasons = route.get("reasons") or [""]
    return str(reasons[0]).removeprefix("決定: ")


def dropped_text(route):
    """表の「外した手順」。route.json の dropped を `<runner> <model> (<理由>)` の `; ` つなぎにする。"""
    dropped = route.get("dropped") or []
    return "; ".join(f"{d.get('runner')} {d.get('model')} ({d.get('reason')})" for d in dropped) or "なし"


def previous_decisions(path):
    """jev-log.jsonl の中で、PBI ごとの同じ PBI・ゲート brief の最後の行。"""
    last = {}
    if not path.exists():
        return last
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("gate") == "brief" and "pbi" in row:
                    last[row["pbi"]] = row
    except OSError:
        return {}
    return last


def decision_key(row):
    """判定を比べる 6 つ (ts は比べない)。template は note だけを見る。"""
    tpl = row.get("template")
    note = tpl.get("note") if isinstance(tpl, dict) else tpl
    return (row.get("tier"), row.get("route"), note, row.get("scores"),
            row.get("warnings"), row.get("dropped"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", default="docs/pbi")
    ap.add_argument("--skip", default="")
    ap.add_argument("--today")
    ap.add_argument("--only", default="")
    ap.add_argument("--tier", choices=common.TIERS, help="段を指定する (--only と一緒のときだけ)")
    ap.add_argument("--home", help="テンプレートの home (省略時は templates.resolve_home の解決順)")
    ap.add_argument("--now", help="今の時刻 (タイムゾーン付き ISO 8601。省略時は現在時刻)")
    args = ap.parse_args()
    if args.tier and not args.only:
        sys.exit("--tier は --only と一緒に使う")
    common.utf8_stdout()
    try:
        now = templates.parse_time(args.now) if args.now else common.now()
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
    unknown = sorted(only - set(pbis))
    if unknown:
        sys.exit(f"--only に facts の無い PBI がある: {', '.join(unknown)}")
    missing = [p for p in pbis if not (base / f"{p}.brief.md").exists() or not (base / f"{p}.md").exists()]
    if missing:
        sys.exit(f"brief.md か PBI 本文が無い: {', '.join(missing)}")

    facts_by = {p: load(base / f"{p}.facts.json") or {} for p in pbis}
    jobs = []
    for p in pbis:
        gates = [("brief", base / f"{p}.brief.md")]
        if not facts_by[p].get("sensitive"):
            gates.append(("pbi_quality", base / f"{p}.md"))
        for gate, src in gates:
            out = base / "jev" / f"{p}.{gate}.json"
            # --only は呼び直す。ただし --tier (段上げ) は段を指定するだけで答えを使わないので、無い答えだけを取る
            recall = p in only and not args.tier
            if recall or load(out) is None:
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
    # 準備する PBI を常に --only で渡す。--only の指定があればそれと route.json がまだ無い PBI (--tier のときは
    # 指定だけ)、無ければ全部。ほかの PBI の route.json と、override のある route.json は select_model.py が書き直さない
    if args.tier:
        targets = sorted(only)
    elif only:
        targets = [p for p in pbis if p in only or not (base / f"{p}.route.json").exists()]
    else:
        targets = pbis
    if not targets:
        sys.exit("準備する PBI が無い")
    cmd += ["--only", ",".join(targets)]
    if args.tier:
        cmd += ["--tier", args.tier]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW)
    if r.returncode != 0:
        sys.exit(f"select_model.py が失敗した: {r.stderr[-500:]}")

    ts = common.now().isoformat(timespec="seconds")
    previous = previous_decisions(base / "jev-log.jsonl")
    lines = []
    called = {job[3].resolve() for job in jobs}
    fresh, cached = [], []
    for p in pbis:
        paths = [base / "jev" / f"{p}.brief.json"]
        if not facts_by[p].get("sensitive"):
            paths.append(base / "jev" / f"{p}.pbi_quality.json")
        for path in paths:
            (fresh if path.resolve() in called else cached).append(common.load_json(path))
    print(template_line(home, now))
    # この実行で呼んだゲートだけで ok / unavailable を出す。再利用だけなら not-checked。
    print(jev_status_line(fresh, True) if fresh else jev_status_line(cached, False))
    print("| PBI | 段 | route の先頭 | review | 補正後の値 (新規性/結合度/あいまいさ) | 理由 | 優先テンプレート | "
          "外した手順 | 品質の警告 | メモ |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for p in pbis:
        brief_raw = common.load_json(base / "jev" / f"{p}.brief.json")
        quality_raw = common.load_json(base / "jev" / f"{p}.pbi_quality.json")
        brief = None if jev_failure_code(brief_raw) else brief_raw
        quality = None if jev_failure_code(quality_raw) else quality_raw
        route = load(base / f"{p}.route.json") or {}
        facts = facts_by[p]
        sensitive = bool(facts.get("sensitive"))
        warns = [] if sensitive else quality_warnings(quality)
        memo = []
        brief_memo = failure_memo("brief", brief_raw, with_score=True)
        if brief_memo:
            memo.append(brief_memo)
        if sensitive:
            memo.append("sensitive のため pbi_quality を送らない")
        else:
            quality_memo = failure_memo("pbi_quality", quality_raw)
            if quality_memo:
                memo.append(quality_memo)
        print(f"| {p} | {route.get('tier')} | {common.step_label(route['route'][0])} | {route.get('review')} | "
              f"{score_text(route)} | {reason_text(route)} | {template_note(route)} | {dropped_text(route)} | "
              f"{', '.join(warns) or 'なし'} | {'; '.join(memo) or '—'} |")
        entry = {"ts": ts, "pbi": p, "gate": "brief", "tier": route.get("tier"),
                 "route": route.get("route"), "template": route.get("template"),
                 "scores": route.get("scores"), "warnings": warns, "dropped": route.get("dropped") or [],
                 "action": "担当を決定 (prepare.py)"}
        model = brief_raw.get("model") if isinstance(brief_raw, dict) else None
        if jev_failure_code(brief_raw) is None and isinstance(model, str) and model:
            entry["model"] = model
        old = previous.get(p)
        if old is None or decision_key(old) != decision_key(entry):
            lines.append(entry)
    if lines:
        with open(base / "jev-log.jsonl", "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
