"""PBI の検証コマンドを番号順に実行し、番号ごとの終了コードと末尾だけを表で出す (appendix/verify.md)。

出力は docs/pbi/verify/<PBI>.<番号>.log に全部書き、設計者の文脈には表だけが入る。
番号ごとのコマンドと終了コードは docs/pbi/verify/<名前>.json にも書く (record_run.py が読む)。
差し戻しの検証では --name に報告名 (PBI-003.rework-1) を渡す。--only で一部だけ再実行したときは、
前の <名前>.json の結果に番号ごとに重ねる。
受け入れ条件の pass / fail の判定は設計者がする (このスクリプトは判定しない)。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/verify.py PBI-003 [--dir docs/pbi] [--only 2,4] [--tail 3] [--timeout 900] [--name PBI-003.final]

PBI の「## Verify commands」節 (旧い PBI は「## 検証コマンド」) から、次のどちらかの形の行をコマンドとして読む
(appendix/plan.md の PBI の雛形):
  1. `pnpm test`
  2: pnpm lint
コマンドは bash (Git Bash) でリポジトリのルートから実行する。読めた行が 0 件なら、そう出して終了コード 2 で終わる。

テストを最後にまとめる選択肢 (appendix/tests-at-end.md):
  <dir>/tests-at-end.json の "deferred" (文字列の一覧) のどれかを含むコマンドを、あとに回すコマンドとして扱う。
  作業者は流さず、orchestrator が波の終わりに 1 回だけ流す。
  --list-deferred  あとに回すコマンドの番号を「3,5」の形で出して終わる (作業者への prompt に書くため)
  --skip-deferred  あとに回すコマンドを除いて流す (作業者の報告のあとの検証)
  --deferred       位置引数に報告名をカンマでつないで渡す (PBI-003,PBI-004.rework-1)。あとに回すコマンドを
                   同じ文字列ごとに 1 回だけ流し、その結果を各報告の <報告名>.json の番号に重ねる
  tests-at-end.json が無いか、"enabled" が false か、"deferred" が空なら、--skip-deferred は何も除かず、
  --list-deferred は空を出す。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Windows で子のコンソールのウインドウを出さない (ヘッドレス)。ほかの OS では 0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^\s*(\d+)[.:)]\s+(?P<rest>\S.*?)\s*$")


def strip_code(rest):
    """`…` で囲まれていれば、最初と最後のバッククォートの間を返す (コマンドの中のバッククォートを残すため)。"""
    if rest.startswith("`") and rest.count("`") >= 2:
        return rest[1:rest.rfind("`")]
    return rest


def commands(pbi_text):
    m = re.search(r"^## (?:Verify commands|検証コマンド)\s*\n(.*?)(?=^## |\Z)", pbi_text, re.S | re.M)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        if line.strip().startswith("```"):
            continue
        mt = LINE.match(line)
        if mt:
            out.append((int(mt.group(1)), strip_code(mt.group("rest")).strip()))
    return out


def deferred_patterns(base):
    """<dir>/tests-at-end.json の "deferred" を返す。ファイルが無い・読めないときは空。"""
    p = base / "tests-at-end.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if not isinstance(data, dict) or data.get("enabled", True) is False:
        return []
    found = data.get("deferred", [])
    if not isinstance(found, list):
        return []
    return [m for m in found if isinstance(m, str) and m.strip()]


def is_deferred(cmd, patterns):
    return any(p in cmd for p in patterns)


def pbi_of(report):
    """報告名 (PBI-003.rework-1) から PBI のファイル名 (PBI-003) を返す。"""
    return report.split(".", 1)[0]


def run_one(bash, cmd, log, timeout, tail_n):
    """コマンドを 1 つ流してログに書き、終了コードと表に出す末尾を返す。"""
    try:
        r = subprocess.run([bash, "-c", cmd], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW)
        output, code = (r.stdout or "") + (r.stderr or ""), r.returncode
    except subprocess.TimeoutExpired:
        output, code = f"(タイムアウト {timeout} 秒)", "timeout"
    log.write_text(f"$ {cmd}\n{output}", encoding="utf-8")
    plain = [ANSI.sub("", ln).strip() for ln in output.strip().splitlines()]
    # node --test (TAP) の集計行は末尾より前にあるので、別に拾って先頭に置く
    tap = [ln for ln in plain if re.match(r"^# (tests|pass|fail) \d+", ln)]
    tail = tap + [ln for ln in plain[-tail_n:] if ln and ln not in tap]
    tail = " / ".join(t[:120] for t in tail).replace("|", "\\|") or "(出力なし)"
    return code, tail


def merge_results(out, name, pbi, results, keep_previous):
    """<名前>.json に番号ごとの結果を書く。keep_previous なら前の結果に番号ごとに重ねる。"""
    merged = {}
    if keep_previous and out.is_file():
        try:
            merged = {r["n"]: r for r in json.loads(out.read_text(encoding="utf-8")).get("results", [])}
        except (ValueError, KeyError, TypeError, AttributeError):
            merged = {}
    merged.update({r["n"]: r for r in results})
    out.write_text(json.dumps({"name": name, "pbi": pbi, "results": [merged[k] for k in sorted(merged)]},
                              ensure_ascii=False), encoding="utf-8")


def deferred_runs(base, reports, patterns):
    """あとに回すコマンドを、同じ文字列ごとに、使う報告と番号の一覧にまとめる。"""
    by_cmd = {}
    for report in reports:
        text = (base / f"{pbi_of(report)}.md").read_text(encoding="utf-8")
        for n, cmd in commands(text):
            if is_deferred(cmd, patterns):
                by_cmd.setdefault(cmd, []).append((report, n))
    return by_cmd


def run_deferred(base, reports, patterns, timeout, tail_n, bash):
    """あとに回すコマンドを同じ文字列ごとに 1 回だけ流し、各報告の結果に重ねる。"""
    logdir = base / "verify"
    logdir.mkdir(parents=True, exist_ok=True)
    by_cmd = deferred_runs(base, reports, patterns)
    if not by_cmd:
        print("あとに回すコマンドが無い (tests-at-end.json の deferred に合うコマンドが、渡した報告の PBI に無い)")
        sys.exit(2)
    print(f"| コマンド | 終了コード | 末尾 ({tail_n} 行) | ログ | 報告と番号 |")
    print("|---|---|---|---|---|")
    per_report = {}
    for i, (cmd, users) in enumerate(by_cmd.items(), start=1):
        log = logdir / f"deferred.{reports[0]}.{i}.log"
        code, tail = run_one(bash, cmd, log, timeout, tail_n)
        who = ", ".join(f"{r}:{n}" for r, n in users)
        shown = cmd[:60].replace("|", "\\|")
        print(f"| {shown} | {code} | {tail} | {log.as_posix()} | {who} |")
        for report, n in users:
            per_report.setdefault(report, []).append({"n": n, "command": cmd, "code": code, "log": log.as_posix()})
    for report, results in per_report.items():
        merge_results(logdir / f"{report}.json", report, pbi_of(report), results, True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pbi")
    ap.add_argument("--dir", default="docs/pbi")
    ap.add_argument("--only", default="")
    ap.add_argument("--tail", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--name", help="ログ名 (既定は PBI 名。全体の再検証などで分けたいとき)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--skip-deferred", action="store_true", help="あとに回すコマンドを除いて流す")
    mode.add_argument("--deferred", action="store_true",
                      help="報告名をカンマでつなぎ、あとに回すコマンドを同じ文字列ごとに 1 回流す")
    mode.add_argument("--list-deferred", action="store_true", help="あとに回すコマンドの番号を出して終わる")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    base = Path(args.dir)
    patterns = deferred_patterns(base)
    bash = shutil.which("bash") or "bash"
    if args.deferred:
        reports = [r.strip() for r in args.pbi.split(",") if r.strip()]
        run_deferred(base, reports, patterns, args.timeout, args.tail, bash)
        return
    text = (base / f"{args.pbi}.md").read_text(encoding="utf-8")
    cmds = commands(text)
    if args.list_deferred:
        print(",".join(str(n) for n, cmd in cmds if is_deferred(cmd, patterns)))
        return
    only = {int(x) for x in args.only.split(",") if x.strip()}
    # --only か --skip-deferred で絞ったか。絞った結果が空のときに「絞らない」と取り違えないため
    filtered = bool(only)
    if args.skip_deferred:
        skipped = {n for n, cmd in cmds if is_deferred(cmd, patterns)}
        if skipped:
            print(f"あとに回すので除いた番号: {','.join(str(n) for n in sorted(skipped))}")
            # 除いた番号の前の結果を残すため、--only と同じく前の結果に重ねる
            only = (only or {n for n, _ in cmds}) - skipped
            filtered = True
            if cmds and not only:
                print(f"{args.pbi}: 検証コマンドはすべてあとに回すもの。波の終わりの --deferred で流す")
                return
    if filtered:
        cmds = [c for c in cmds if c[0] in only]
    if not cmds:
        print(f"{args.pbi}: 「## Verify commands」から番号付きのコマンドを読めなかった。PBI の書式を直すか、手で実行する")
        sys.exit(2)
    logdir = base / "verify"
    logdir.mkdir(parents=True, exist_ok=True)
    name = args.name or args.pbi
    print(f"| 番号 | 終了コード | 末尾 ({args.tail} 行) | ログ |")
    print("|---|---|---|---|")
    results = []
    for n, cmd in cmds:
        log = logdir / f"{name}.{n}.log"
        code, tail = run_one(bash, cmd, log, args.timeout, args.tail)
        print(f"| {n} | {code} | {tail} | {log.as_posix()} |")
        results.append({"n": n, "command": cmd, "code": code, "log": log.as_posix()})
    # --only・--skip-deferred で一部だけ流したときは、前の結果に番号ごとに重ねる (record_run.py が全体の件数を読むため)
    merge_results(logdir / f"{name}.json", name, args.pbi, results, bool(only))


if __name__ == "__main__":
    main()
