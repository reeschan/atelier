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
コマンドは bash でリポジトリのルートから実行する。読めた行が 0 件なら、そう出して終了コード 2 で終わる。

bash と Node:
  Windows の bash は agents.json の tools.bash.path、git と同じ Git の bin\\bash.exe、PATH の bash
  (System32・WindowsApps の下は除く) の順に探す。どれも無ければ終了コード 2。
  Node はリポジトリの .node-version、.nvmrc、package.json の engines.node の順に版を読み、fnm に入っている
  その版のディレクトリを子の PATH の先頭に置く。見つからなければ warn を出して今の PATH で流す。
  ログの先頭には `bash: <パス>` と `node: <node -v>` の行を書く。

テストを最後にまとめる選択肢 (appendix/tests-at-end.md):
  <dir>/tests-at-end.json の "deferred" のどれかを含むコマンドを、あとに回すコマンドとして扱う。
  要素は文字列か {"match", "when": "wave"|"end", "timeout": 秒, "server": {"start", "url"}} (when の既定は wave)。
  作業者は流さず、orchestrator が波の終わり (wave) か run の最後 (end) に 1 回だけ流す。
  --list-deferred  あとに回すコマンドの番号を「3,5」の形で出して終わる (作業者への prompt に書くため)
  --skip-deferred  あとに回すコマンドを除いて流す (作業者の報告のあとの検証)
  --deferred       位置引数に報告名をカンマでつないで渡す (PBI-003,PBI-004.rework-1)。あとに回すコマンドのうち
                   --when (既定 wave) の合うものを同じ文字列ごとに 1 回だけ流し、各報告の <報告名>.json の番号に重ねる。
                   その時期の要素が無ければ `deferred: none for when=<時期>` を出して終了コード 0。
                   要素の timeout に当たれば結果を書いたうえで `blocked: limit` を出して終了コード 3
  --related a.cy.ts,b.cy.ts  "investigate" の {specs} をカンマでつないだ spec に置き換えて流す。
                   上限は spec の数 × limits.per_spec 秒。当たれば `blocked: limit` を出して終了コード 3。
                   spec が空か、investigate に {specs} が無ければ `refused: ...` で終了コード 2
  --final          "final" を時間の上限なしで流す。出力が limits.idle 秒無ければ止め、`blocked: idle` で終了コード 3。
                   <dir>/full-run.approved.json (conductor.py answer --full-run yes) が無ければ `refused: ...` で
                   終了コード 2。あればサーバーを用意したあと、流す直前に消す (承認は 1 回の通しで使い切る)
  PBI の検証コマンドに "final" (と同じか、それを含むか、"final" を && ・ ; で区切った最後のコマンドを含むコマンド)
  があれば、流さずに `refused: ...` で終了コード 2。
  "limits" の既定は {"per_spec": 900, "retries": 2, "idle": 600}。
  "server" ({"start", "url"}) のある検査 (--related・--final は最上位の "server") の前に url を見る。
  応答すればそのまま使い、止めない。応答しなければ start を起動して応答を待ち (最大 120 秒、来なければ
  `blocked: server` で終了コード 3)、検査のあとに起動したプロセスの木ごと止める。
  tests-at-end.json が無いか、"enabled" が false か、"deferred" が空なら、--skip-deferred は何も除かず、
  --list-deferred は空を出す。
"""

import argparse
import codecs
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from agents import fnm_node_dir
from common import load_json, resolve_home

# Windows で子のコンソールのウインドウを出さない (ヘッドレス)。ほかの OS では 0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
# Windows 以外では子を新しいプロセスグループにし、止めるときにグループごと止める
NEW_GROUP = {} if os.name == 'nt' else {'start_new_session': True}

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^\s*(\d+)[.:)]\s+(?P<rest>\S.*?)\s*$")
VERSION = re.compile(r"v?(\d+(?:\.\d+){0,2})")
LIMITS = {"per_spec": 900, "retries": 2, "idle": 600}
SERVER_WAIT = 120
BLOCKED_EXIT = 3
# 通しの承認 (conductor.py answer --full-run yes が書く)。--dir からの相対
FULL_RUN_APPROVAL = "full-run.approved.json"


class Blocked(Exception):
    """上限・無出力・サーバーの起動の失敗で止めたとき。メッセージは `blocked: ` のあとに出す理由。"""


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


def load_settings(base):
    """<dir>/tests-at-end.json を辞書で返す。無い・読めないときは空。"""
    data = load_json(base / "tests-at-end.json", {})
    return data if isinstance(data, dict) else {}


def deferred_entries(settings):
    """"deferred" の要素を {"match", "when", "timeout", "server"} にそろえて返す。enabled が false なら空。"""
    if settings.get("enabled", True) is False:
        return []
    found = settings.get("deferred", [])
    if not isinstance(found, list):
        return []
    out = []
    for item in found:
        if isinstance(item, str):
            item = {"match": item}
        if not isinstance(item, dict) or not isinstance(item.get("match"), str) or not item["match"].strip():
            continue
        out.append({"match": item["match"], "when": item.get("when", "wave"),
                    "timeout": item.get("timeout"), "server": item.get("server")})
    return out


def entry_for(cmd, entries):
    """cmd に合う最初の deferred の要素。無ければ None。"""
    return next((e for e in entries if e["match"] in cmd), None)


def deferred_patterns(base):
    """<dir>/tests-at-end.json の "deferred" の match の文字列の一覧 (when を問わない)。無ければ空。"""
    return [e["match"] for e in deferred_entries(load_settings(base))]


def is_deferred(cmd, entries):
    """cmd があとに回すコマンドか。entries は deferred_entries の要素か、deferred_patterns の文字列。"""
    return any((e if isinstance(e, str) else e["match"]) in cmd for e in entries)


def final_command(settings):
    """"final" (通しのコマンド) を前後の空白を除いて返す。無ければ None。"""
    final = settings.get("final")
    return final.strip() if isinstance(final, str) and final.strip() else None


def limits_of(settings):
    limits = dict(LIMITS)
    given = settings.get("limits")
    if isinstance(given, dict):
        limits.update({k: v for k, v in given.items() if isinstance(v, (int, float))})
    return limits


def pbi_of(report):
    """報告名 (PBI-003.rework-1) から PBI のファイル名 (PBI-003) を返す。"""
    return report.split(".", 1)[0]


# --- bash と Node ---

def _system_bash(path):
    """System32 (WSL の bash) か WindowsApps (ストアの shim) の下か。"""
    # 動いている OS に依らず \ と / の両方で区切る
    parts = [p.lower() for p in PureWindowsPath(path).parts]
    return "system32" in parts or "windowsapps" in parts


def find_bash(home, os_name=None, which=shutil.which):
    """検証に使う bash。Windows は tools.bash.path → git の Git の bin\\bash.exe → PATH (System32・WindowsApps を除く)。"""
    os_name = os.name if os_name is None else os_name
    if os_name != "nt":
        return which("bash")
    agents = load_json(Path(home) / "agents.json", {})
    tools = agents.get("tools") if isinstance(agents, dict) else None
    tool = tools.get("bash") if isinstance(tools, dict) else None
    configured = tool.get("path") if isinstance(tool, dict) else None
    if isinstance(configured, str) and Path(configured).is_file():
        return configured
    git = which("git")
    if git:
        where = Path(git).parent
        # git.exe は <root>\cmd・<root>\bin・<root>\mingw64\bin のどれか
        root = where.parent.parent if where.parent.name.lower() == "mingw64" else where.parent
        candidate = root / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    found = which("bash")
    if found and not _system_bash(found):
        return found
    return None


def wanted_node(root):
    """プロジェクトが求める Node の版 (26.8.1 か 26)。.node-version → .nvmrc → package.json の engines.node。"""
    texts = []
    for name in (".node-version", ".nvmrc"):
        p = root / name
        if p.is_file():
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
    pkg = load_json(root / "package.json", {})
    engines = pkg.get("engines") if isinstance(pkg, dict) else None
    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
        texts.append(engines["node"])
    for text in texts:
        m = VERSION.search(text)
        if m:
            return m.group(1)
    return None


def _path_key(env):
    """PATH のキー (Windows の Path のような大小文字を保つ)。"""
    return next((k for k in env if k.upper() == "PATH"), "PATH")


def child_env(root, base_env=None):
    """(子の環境変数, ログの先頭に書く Node の行)。fnm のプロジェクトの版の Node を PATH の先頭に置く。"""
    env = dict(os.environ if base_env is None else base_env)
    notes = []
    wanted = wanted_node(root)
    key = _path_key(env)
    if wanted:
        directory = fnm_node_dir(wanted, env)
        if directory:
            rest = [p for p in env.get(key, "").split(os.pathsep) if p and p != directory]
            env[key] = os.pathsep.join([directory, *rest])
        else:
            notes.append(f"warn node: {wanted} not found; using PATH")
    node = shutil.which("node", path=env.get(key, ""))
    version = "(not found)"
    if node:
        try:
            r = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=30, env=env,
                               creationflags=NO_WINDOW)
            version = (r.stdout or r.stderr).strip() or f"(exit {r.returncode})"
        except (OSError, subprocess.SubprocessError) as e:
            version = f"(failed: {e})"
    return env, notes + [f"node: {version}"]


# --- 実行 ---

def kill_tree(proc):
    """proc と、それが起動したプロセスの木を止める。

    Windows 以外では proc を NEW_GROUP (新しいセッション) で起動するので、プロセスグループの番号は proc.pid と同じ。
    起動したシェルが先に終わっていても、残った子 (裏で動くサーバーなど) をグループごと止める。
    Windows は taskkill /T で木をたどるので、起動したシェルが動いているときだけ止める
    (終わったあとの pid は別のプロセスに使い回されることがある)。
    """
    if os.name == "nt":
        if proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def stream(bash, cmd, env, timeout, idle):
    """cmd を流して (出力, 終了コード) を返す。上限なら終了コードは "timeout"、無出力なら "idle"。"""
    proc = subprocess.Popen([bash, "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW, **NEW_GROUP)
    chunks, last = [], [time.monotonic()]

    def pump():
        # 行ではなく届いた分ずつ読む (改行の無い進み具合の出力も、出力があったと数えるため)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            data = proc.stdout.read1(65536)
            if not data:
                break
            chunks.append(decoder.decode(data))
            last[0] = time.monotonic()
        chunks.append(decoder.decode(b"", final=True))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    start, code = time.monotonic(), None
    while code is None:
        try:
            code = proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if timeout and now - start >= timeout:
                kill_tree(proc)
                code = "timeout"
            elif idle and now - last[0] >= idle:
                kill_tree(proc)
                code = "idle"
    # 子が残した孫がパイプを持ったままでも待ち続けない
    reader.join(5)
    if code == "timeout":
        chunks.append(f"\n(タイムアウト {timeout} 秒)\n")
    elif code == "idle":
        chunks.append(f"\n(出力が {idle} 秒無いので止めた)\n")
    return "".join(chunks).replace("\r\n", "\n"), code


def run_one(ctx, cmd, log, timeout, tail_n, idle=None):
    """コマンドを 1 つ流してログに書き、終了コードと表に出す末尾を返す。"""
    output, code = stream(ctx["bash"], cmd, ctx["env"], timeout, idle)
    log.write_text("\n".join(ctx["header"]) + f"\n$ {cmd}\n{output}", encoding="utf-8")
    plain = [ANSI.sub("", ln).strip() for ln in output.strip().splitlines()]
    # node --test (TAP) の集計行は末尾より前にあるので、別に拾って先頭に置く
    tap = [ln for ln in plain if re.match(r"^# (tests|pass|fail) \d+", ln)]
    tail = tap + [ln for ln in plain[-tail_n:] if ln and ln not in tap]
    tail = " / ".join(t[:120] for t in tail).replace("|", "\\|") or "(出力なし)"
    return code, tail


def answers(url):
    """url が HTTP で応答するか (状態コードは問わない)。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(url, timeout=2).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


@contextmanager
def serving(ctx, server, log):
    """server ({"start", "url"}) を使える状態にする。自分で起動したものだけ、抜けるときに木ごと止める。"""
    if not isinstance(server, dict) or not server.get("url"):
        yield
        return
    url = server["url"]
    if answers(url):
        print(f"server: {url} answers; using it")
        yield
        return
    if not server.get("start"):
        raise Blocked(f"server ({url} が応答せず、start が無い)")
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen([ctx["bash"], "-c", server["start"]], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=ctx["env"], creationflags=NO_WINDOW, **NEW_GROUP)
    try:
        deadline = time.monotonic() + float(server.get("wait", SERVER_WAIT))
        while not answers(url):
            if proc.poll() is not None or time.monotonic() >= deadline:
                raise Blocked(f"server ({url} が `{server['start']}` のあと応答しない。ログ {log.as_posix()})")
            time.sleep(0.5)
        print(f"server: started {url} ({server['start']})")
        yield
    finally:
        kill_tree(proc)


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


def deferred_runs(base, reports, entries):
    """あとに回すコマンドを、同じ文字列ごとに (合う要素, 使う報告と番号の一覧) にまとめる。"""
    by_cmd = {}
    for report in reports:
        text = (base / f"{pbi_of(report)}.md").read_text(encoding="utf-8")
        for n, cmd in commands(text):
            entry = entry_for(cmd, entries)
            if entry:
                by_cmd.setdefault(cmd, (entry, []))[1].append((report, n))
    return by_cmd


def run_deferred(ctx, base, reports, entries, timeout, tail_n):
    """あとに回すコマンドを同じ文字列ごとに 1 回だけ流し、各報告の結果に重ねる。"""
    logdir = base / "verify"
    by_cmd = deferred_runs(base, reports, entries)
    limited = []
    if not by_cmd:
        print("あとに回すコマンドが無い (tests-at-end.json の deferred に合うコマンドが、渡した報告の PBI に無い)")
        sys.exit(2)
    print(f"| コマンド | 終了コード | 末尾 ({tail_n} 行) | ログ | 報告と番号 |")
    print("|---|---|---|---|---|")
    for i, (cmd, (entry, users)) in enumerate(by_cmd.items(), start=1):
        log = logdir / f"deferred.{reports[0]}.{i}.log"
        with serving(ctx, entry["server"], logdir / f"deferred.{reports[0]}.{i}.server.log"):
            limit = entry["timeout"] or timeout
            code, tail = run_one(ctx, cmd, log, limit, tail_n)
        if code == "timeout":
            limited.append(f"{cmd[:60]} ({limit} 秒)")
        who = ", ".join(f"{r}:{n}" for r, n in users)
        shown = cmd[:60].replace("|", "\\|")
        print(f"| {shown} | {code} | {tail} | {log.as_posix()} | {who} |")
        # 次のコマンドのサーバーが立たずに止まっても、流し終えた結果は残す
        for report, n in users:
            merge_results(logdir / f"{report}.json", report, pbi_of(report),
                          [{"n": n, "command": cmd, "code": code, "log": log.as_posix()}], True)
    # 結果を書いたあとで止める (上限に当たったことを終了コードで伝えるため)
    if limited:
        raise Blocked(f"limit ({', '.join(limited)})")


def run_named(ctx, base, settings, name, cmd, timeout, idle, tail_n, approval=None):
    """--related・--final の 1 本を流して <名前>.json に書く。上限・無出力で止めたら Blocked。

    approval (通しの承認のファイル) があれば、サーバーを用意したあと流す直前に消す。用意で止まれば承認は残る。
    """
    logdir = base / "verify"
    log = logdir / f"{name}.1.log"
    with serving(ctx, settings.get("server"), logdir / f"{name}.server.log"):
        if approval is not None:
            # 「はい」は 1 回の通しで使い切る。消せた 1 つだけが流す (同時に始めたもう 1 つは断る)
            try:
                approval.unlink()
            except FileNotFoundError:
                print("refused: the full-run approval was already used (conductor.py answer --full-run yes)")
                sys.exit(2)
        code, tail = run_one(ctx, cmd, log, timeout, tail_n, idle)
    print(f"| 番号 | 終了コード | 末尾 ({tail_n} 行) | ログ |")
    print("|---|---|---|---|")
    print(f"| 1 | {code} | {tail} | {log.as_posix()} |")
    merge_results(logdir / f"{name}.json", name, name,
                  [{"n": 1, "command": cmd, "code": code, "log": log.as_posix()}], False)
    if code == "timeout":
        raise Blocked(f"limit ({timeout} 秒)")
    if code == "idle":
        raise Blocked(f"idle ({idle} 秒出力なし)")


def prepare(base, home):
    """bash・子の環境・ログの先頭の行をそろえ、先頭の行を出す。bash が無ければ終了コード 2。"""
    bash = find_bash(home)
    if not bash:
        print("bash: not found. agents.json の tools.bash.path、Git for Windows の bin\\bash.exe、"
              "PATH の bash (System32・WindowsApps 以外) のどれも無い。Git for Windows を入れるか tools.bash.path を書く")
        sys.exit(2)
    env, notes = child_env(Path.cwd())
    header = [f"bash: {bash}", *notes]
    print("\n".join(header))
    (base / "verify").mkdir(parents=True, exist_ok=True)
    return {"bash": bash, "env": env, "header": header}


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pbi", nargs="?")
    ap.add_argument("--dir", default="docs/pbi")
    ap.add_argument("--only", default="")
    ap.add_argument("--tail", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--name", help="ログ名 (既定は PBI 名、--related は related、--final は final)")
    ap.add_argument("--when", choices=("wave", "end"), default="wave", help="--deferred で流す要素の時期")
    ap.add_argument("--home", help="agents.json のある home (既定は ATELIER_HOME などから)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--skip-deferred", action="store_true", help="あとに回すコマンドを除いて流す")
    mode.add_argument("--deferred", action="store_true",
                      help="報告名をカンマでつなぎ、あとに回すコマンドを同じ文字列ごとに 1 回流す")
    mode.add_argument("--list-deferred", action="store_true", help="あとに回すコマンドの番号を出して終わる")
    mode.add_argument("--related", help="investigate で流す spec をカンマでつないで渡す")
    mode.add_argument("--final", action="store_true", help="final (通し) を流す")
    args = ap.parse_args()
    if args.pbi is None and not (args.related or args.final):
        ap.error("PBI 名 (--deferred では報告名) が要る")
    return args


def main_named(args, base, settings):
    """--related・--final。上限・無出力・サーバーで止めたら `blocked: ...` を出して終了コード 3。"""
    limits = limits_of(settings)
    key = "final" if args.final else "investigate"
    template = settings.get(key)
    if not isinstance(template, str) or not template.strip():
        print(f"tests-at-end.json に \"{key}\" が無い")
        sys.exit(2)
    if args.final:
        approval = base / FULL_RUN_APPROVAL
        if not approval.is_file():
            print("refused: no full-run approval (conductor.py answer --full-run yes)")
            sys.exit(2)
        ctx = prepare(base, resolve_home(args.home))
        run_named(ctx, base, settings, args.name or "final", template, None, limits["idle"], args.tail,
                  approval)
        return
    # spec で絞れない --related は通しと同じになるので流さない
    specs = [s.strip() for s in args.related.split(",") if s.strip()]
    if not specs:
        print("refused: --related has no spec (give spec paths separated by commas)")
        sys.exit(2)
    if "{specs}" not in template:
        print('refused: "investigate" in tests-at-end.json has no {specs}; it would ignore the specs')
        sys.exit(2)
    ctx = prepare(base, resolve_home(args.home))
    cmd = template.replace("{specs}", ",".join(specs))
    run_named(ctx, base, settings, args.name or "related", cmd, len(specs) * limits["per_spec"], None, args.tail)


def _squash(cmd):
    return " ".join(cmd.split())


def runs_final(cmd, final):
    """cmd が通し (final) を流すか。final と同じ・final を含む・final の最後のコマンド (cd などの前置きを除く) を含む。"""
    cmd, final = _squash(cmd), _squash(final)
    core = _squash(re.split(r"&&|;", final)[-1])
    return cmd == final or final in cmd or (bool(core) and core in cmd)


def main_pbi(args, base, settings, entries):
    """PBI 1 つの検証コマンド (--only・--skip-deferred・--list-deferred を含む)。"""
    text = (base / f"{args.pbi}.md").read_text(encoding="utf-8")
    cmds = commands(text)
    final = final_command(settings)
    if final and not args.list_deferred:
        for n, cmd in cmds:
            if runs_final(cmd, final):
                print(f"refused: {args.pbi} verify {n} runs the whole suite (final); use related specs")
                sys.exit(2)
    if args.list_deferred:
        print(",".join(str(n) for n, cmd in cmds if is_deferred(cmd, entries)))
        return
    only = {int(x) for x in args.only.split(",") if x.strip()}
    # --only か --skip-deferred で絞ったか。絞った結果が空のときに「絞らない」と取り違えないため
    filtered = bool(only)
    if args.skip_deferred:
        skipped = {n for n, cmd in cmds if is_deferred(cmd, entries)}
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
    ctx = prepare(base, resolve_home(args.home))
    logdir = base / "verify"
    name = args.name or args.pbi
    print(f"| 番号 | 終了コード | 末尾 ({args.tail} 行) | ログ |")
    print("|---|---|---|---|")
    results = []
    for n, cmd in cmds:
        log = logdir / f"{name}.{n}.log"
        code, tail = run_one(ctx, cmd, log, args.timeout, args.tail)
        print(f"| {n} | {code} | {tail} | {log.as_posix()} |")
        results.append({"n": n, "command": cmd, "code": code, "log": log.as_posix()})
    # --only・--skip-deferred で一部だけ流したときは、前の結果に番号ごとに重ねる (record_run.py が全体の件数を読むため)
    merge_results(logdir / f"{name}.json", name, args.pbi, results, bool(only))


def main():
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    base = Path(args.dir)
    settings = load_settings(base)
    entries = deferred_entries(settings)
    try:
        if args.related or args.final:
            main_named(args, base, settings)
        elif args.deferred:
            reports = [r.strip() for r in args.pbi.split(",") if r.strip()]
            phase = [e for e in entries if e["when"] == args.when]
            if not phase:
                print(f"deferred: none for when={args.when}")
                return
            ctx = prepare(base, resolve_home(args.home))
            run_deferred(ctx, base, reports, phase, args.timeout, args.tail)
        else:
            main_pbi(args, base, settings, entries)
    except Blocked as e:
        print(f"blocked: {e}")
        sys.exit(BLOCKED_EXIT)


if __name__ == "__main__":
    main()
