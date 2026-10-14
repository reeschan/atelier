"""PBI の検証コマンドを番号順に実行し、番号ごとの終了コードと末尾だけを表で出す (SKILL.md 8 章)。

出力は docs/pbi/verify/<PBI>.<番号>.log に全部書き、設計者の文脈には表だけが入る。
受け入れ条件の pass / fail の判定は設計者がする (このスクリプトは判定しない)。

使い方 (リポジトリのルートで):
  python <このディレクトリ>/verify.py PBI-003 [--dir docs/pbi] [--only 2,4] [--tail 3] [--timeout 900] [--name PBI-003.final]

PBI の「## 検証コマンド」節から、次のどちらかの形の行をコマンドとして読む (2 章のテンプレート):
  1. `pnpm test`
  2: pnpm lint
コマンドは bash (Git Bash) でリポジトリのルートから実行する。読めた行が 0 件なら、そう出して終了コード 2 で終わる。
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^\s*(\d+)[.:)]\s+(?P<rest>\S.*?)\s*$")


def strip_code(rest):
    """`…` で囲まれていれば、最初と最後のバッククォートの間を返す (コマンドの中のバッククォートを残すため)。"""
    if rest.startswith("`") and rest.count("`") >= 2:
        return rest[1:rest.rfind("`")]
    return rest


def commands(pbi_text):
    m = re.search(r"^## 検証コマンド\s*\n(.*?)(?=^## |\Z)", pbi_text, re.S | re.M)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pbi")
    ap.add_argument("--dir", default="docs/pbi")
    ap.add_argument("--only", default="")
    ap.add_argument("--tail", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--name", help="ログ名 (既定は PBI 名。全体の再検証などで分けたいとき)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    base = Path(args.dir)
    text = (base / f"{args.pbi}.md").read_text(encoding="utf-8")
    cmds = commands(text)
    only = {int(x) for x in args.only.split(",") if x.strip()}
    if only:
        cmds = [c for c in cmds if c[0] in only]
    if not cmds:
        print(f"{args.pbi}: 「## 検証コマンド」から番号付きのコマンドを読めなかった。PBI の書式を直すか、手で実行する")
        sys.exit(2)
    bash = shutil.which("bash") or "bash"
    logdir = base / "verify"
    logdir.mkdir(parents=True, exist_ok=True)
    name = args.name or args.pbi
    print(f"| 番号 | 終了コード | 末尾 ({args.tail} 行) | ログ |")
    print("|---|---|---|---|")
    for n, cmd in cmds:
        log = logdir / f"{name}.{n}.log"
        try:
            r = subprocess.run([bash, "-c", cmd], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=args.timeout)
            output, code = (r.stdout or "") + (r.stderr or ""), r.returncode
        except subprocess.TimeoutExpired:
            output, code = f"(タイムアウト {args.timeout} 秒)", "timeout"
        log.write_text(f"$ {cmd}\n{output}", encoding="utf-8")
        plain = [ANSI.sub("", ln).strip() for ln in output.strip().splitlines()]
        # node --test (TAP) の集計行は末尾より前にあるので、別に拾って先頭に置く
        tap = [ln for ln in plain if re.match(r"^# (tests|pass|fail) \d+", ln)]
        tail = tap + [ln for ln in plain[-args.tail:] if ln and ln not in tap]
        tail = " / ".join(t[:120] for t in tail).replace("|", "\\|") or "(出力なし)"
        print(f"| {n} | {code} | {tail} | {log.as_posix()} |")


if __name__ == "__main__":
    main()
