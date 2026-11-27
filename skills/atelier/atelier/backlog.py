"""The backlog (backlog.jsonl): what runs left over, kept for the next runs of the same repository.

The backlog is <git common dir>/atelier/backlog.jsonl, outside the work tree and shared by every worktree of the
repository. Each line is one leftover of a run (run["leftovers"], what the final check and the fix stage left over), in
the shape of a retrospective list item (retro.FIELDS: kind, what, why, tried, state, interpretation, options,
recommendation, affects and source), with the run it came from (`run`), the leftover's id (`item`) and its own id
(`id`, "<run>:<item>"). The retrospective items the leftover comes from are copied as they are into source.items.

The report writes the run's leftovers to it once (add_leftovers). The plan launch of a later run gives the planner the
entries no run has taken yet (candidates) as PBI candidates; a PBI names the entries it takes in `backlog`, and when that
plan is approved each named entry gets the run that took it (`taken_by`, take), so it is no candidate any more. The
mark goes again when the run does not fix the entry: a re-plan that no longer names it (take), or a report where every
PBI that named it did not pass (release_unpassed); the entry is then a candidate for the next runs again.

A work root that is not a git work tree has no backlog: nothing is read or written, and the report says so.

Runs in different worktrees share the file, so every change to it (take, release_unpassed, add_leftovers) holds one
lock next to it, from its first read to its last write: the OS's exclusive lock (flock on POSIX, msvcrt.locking on
Windows) on the file <git common dir>/atelier/backlog.jsonl.lock. The file stays there; the lock is held only while a
change runs, and the OS lets it go when its holder closes the file or dies, so no run removes another run's lock and a
lock left by a run that died needs no clean-up. A run waits for the lock up to LOCK_WAIT_SECONDS (then the change fails
with an OSError, as a write that fails does).
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from . import retro, state

SCHEMA = "atelier.backlog/1"
FILE_NAME = "backlog.jsonl"
LOCK_SUFFIX = ".lock"
LOCK_WAIT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.05
# The errors of a lock try while another holder has the lock (Windows gives EACCES or EDEADLOCK, POSIX EWOULDBLOCK).
_HELD_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
NOT_GIT = "作業ルートが git のリポジトリでないため"

KIND_LABELS = {
    "check_failure": "検査の失敗",
    "e2e_failure": "E2E の失敗",
    "fix_open": "修正の作業者が直せなかった",
    "unlock_failed": "鍵を外す修正を確かめられなかった",
}
TRIED = {
    "check_failure": "修正の段を 1 回回した後の最後の検査で落ちた（作り直していない）",
    "e2e_failure": "修正の段を 1 回回した後の最後の検査で E2E が落ちた（作り直していない）",
    "fix_open": "修正の段の作業者が直せず、open で返した",
    "unlock_failed": "鍵を外す修正を確かめられず、鍵のファイルを鍵のコミットの中身に戻した",
}
OPTIONS = ["次の run で PBI にして直す", "見送る（backlog に残す）"]
RECOMMENDATION = "次の run の計画で PBI にして直す"
# The fields a candidate shows the planner (the whole entry stays in the backlog file).
CANDIDATE_FIELDS = (
    "id", "run", "item", "kind", "what", "why", "tried", "state", "interpretation", "options", "recommendation", "affects",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def common_dir(root: Path) -> Path | None:
    """The git common dir of the work root, or None when the root is not inside a git work tree."""
    try:
        inside = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if inside.returncode or inside.stdout.strip() != "true":
            return None
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    text = proc.stdout.strip()
    if proc.returncode or not text:
        return None
    common = Path(text)
    return (common if common.is_absolute() else Path(root) / common).resolve()


def location(root: Path) -> Path | None:
    """<git common dir>/atelier/backlog.jsonl, or None outside git."""
    common = common_dir(Path(root))
    return common / "atelier" / FILE_NAME if common is not None else None


def entry_id(run_id: str, item_id: str) -> str:
    return f"{run_id}:{item_id}"


def _read_lines(path: Path) -> list[tuple[str, dict[str, Any] | None]]:
    """Every line of the file (its text, and the entry when it reads as one); a missing file has none."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    out: list[tuple[str, dict[str, Any] | None]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        out.append((line, entry if isinstance(entry, dict) and entry.get("id") else None))
    return out


def read(path: Path | None) -> list[dict[str, Any]]:
    """The entries of the backlog; a line that does not read as an entry is skipped (and kept in the file)."""
    if path is None:
        return []
    return [entry for _, entry in _read_lines(Path(path)) if entry is not None]


def _dumps(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def lock_path(path: Path) -> Path:
    """The lock file of the backlog at path."""
    return Path(path).with_name(Path(path).name + LOCK_SUFFIX)


def _try_lock(fd: int) -> bool:
    """Take the OS lock on the open lock file without waiting; False while another holder has it."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # The first byte; a lock may lie past the end of the file.
    except OSError as exc:
        if exc.errno in _HELD_ERRNOS:
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold the backlog's lock for one read-modify-write; TimeoutError (an OSError) after LOCK_WAIT_SECONDS.

    The lock is the OS's lock on the lock file (flock on POSIX, msvcrt.locking on Windows), not the file itself: the
    file stays, and the OS lets the lock go when its holder closes the file or dies.
    """
    lock = lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"backlog のロック {lock} を {LOCK_WAIT_SECONDS:g} 秒待っても取れませんでした。")
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _source_items(root: Path, run: dict[str, Any], ids: Iterable[str]) -> list[dict[str, Any]]:
    """The retrospective items a leftover comes from, as they are: the fix list's item, else the list's own."""
    wanted = [str(i) for i in ids if i]
    if not wanted:
        return []
    known: dict[str, dict[str, Any]] = {}
    try:
        body = retro.load(run)
        for item in [*(body.get("items") or []), *(body.get("dropped") or [])]:
            if isinstance(item, dict) and item.get("id"):
                known.setdefault(str(item["id"]), item)
    except state.StateError:
        pass  # No list (or a broken one): the fix list's items only.
    for item in run.get("fix_list") or []:
        if isinstance(item, dict) and item.get("id"):
            known[str(item["id"])] = item
    return [json.loads(json.dumps(known[i], ensure_ascii=False)) for i in wanted if i in known]


def _last_line(text: Any) -> str:
    return next((ln.strip() for ln in reversed(str(text or "").splitlines()) if ln.strip()), "")


def _union(values: Iterable[Iterable[Any]]) -> list[str]:
    out: list[str] = []
    for group in values:
        for value in group or []:
            text = str(value).strip()
            if text and text not in out:
                out.append(text)
    return out


def make_entry(root: Path, run: dict[str, Any], leftover: dict[str, Any]) -> dict[str, Any]:
    """One backlog line for a leftover: the retrospective item's fields, the run and the leftover's id."""
    run_id = str(run.get("run_id") or "run")
    kind = str(leftover.get("kind") or "")
    items = _source_items(root, run, leftover.get("items") or [])
    code = leftover.get("exit_code")
    what = str(leftover.get("what") or "")
    readings = [str(i.get("interpretation")) for i in items if i.get("interpretation") and i.get("interpretation") != retro.NONE_TEXT]
    affects = [i.get("affects") if isinstance(i.get("affects"), dict) else {} for i in items]
    return {
        "schema": SCHEMA,
        "id": entry_id(run_id, str(leftover.get("id") or "")),
        "run": run_id,
        "item": str(leftover.get("id") or ""),
        "kind": kind,
        "what": what,
        "why": _last_line(leftover.get("output")) or what,
        "tried": TRIED.get(kind, "この run では直していない"),
        "state": "残り（この run では直していない" + (f"、終了コード {code}" if code not in (None, "") else "") + "）",
        "interpretation": "; ".join(readings) or retro.NONE_TEXT,
        "options": list(OPTIONS),
        "recommendation": RECOMMENDATION,
        "affects": {
            "pbis": _union(a.get("pbis") or [] for a in affects),
            "files": _union(a.get("files") or [] for a in affects),
        },
        "source": {
            "record": f"run.json の leftovers の {leftover.get('id')}",
            "leftover": dict(leftover),
            "items": items,
        },
        "added_at": _now(),
        "taken_by": None,
    }


def _status(run: dict[str, Any]) -> dict[str, Any]:
    return run.setdefault("backlog", {})


def add_leftovers(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    """Write the run's leftovers to the backlog, each once (a line already there is not written again).

    Returns run["backlog"]: its path (None outside git), `git`, the leftover ids `written`, and `error` when the file
    could not be written. The entries this run took whose PBIs all did not pass are released first (release_unpassed).
    """
    kept = _status(run)
    path = location(Path(root))
    kept["git"] = path is not None
    kept["path"] = str(path) if path is not None else None
    written = kept.setdefault("written", [])
    if path is None:
        return kept
    todo = [lo for lo in run.get("leftovers") or [] if isinstance(lo, dict) and lo.get("id") and lo["id"] not in written]
    if not kept.get("taken") and not todo:
        return kept
    try:
        # One lock from the release's read to the append, so no other run's change falls between them.
        with _locked(path):
            _release_unpassed(path, run)
            have = {entry["id"] for entry in read(path)}
            lines = []
            for lo in todo:
                entry = make_entry(Path(root), run, lo)
                if entry["id"] not in have:
                    lines.append(_dumps(entry))
            if lines:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write("".join(line + "\n" for line in lines))
    except OSError as exc:
        kept["error"] = f"{path}: {exc}"
        return kept
    written += [lo["id"] for lo in todo]
    kept.pop("error", None)
    return kept


def candidates(root: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The entries the planner gets as PBI candidates: those no run took (and those this run took), in file order.

    Their ids are kept in run["backlog"]["candidates"]: a plan may name only them.
    """
    kept = _status(run)
    path = location(Path(root))
    kept["git"] = path is not None
    kept["path"] = str(path) if path is not None else None
    run_id = run.get("run_id")
    entries = [e for e in read(path) if not e.get("taken_by") or e.get("taken_by") == run_id]
    kept["candidates"] = [str(e["id"]) for e in entries]
    return [{k: e.get(k) for k in CANDIDATE_FIELDS if k in e} for e in entries]


def kept_candidates(root: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The candidates the plan launch gave, as the backlog holds them now (for a revision of that plan)."""
    ids = set((run.get("backlog") or {}).get("candidates") or [])
    if not ids:
        return []
    return [{k: e.get(k) for k in CANDIDATE_FIELDS if k in e} for e in read(location(Path(root))) if e["id"] in ids]


def candidate_ids(run: dict[str, Any]) -> list[str]:
    return list((run.get("backlog") or {}).get("candidates") or [])


def named(run: dict[str, Any]) -> dict[str, list[str]]:
    """Each backlog id the run's PBIs name -> the PBIs that name it, in plan order."""
    out: dict[str, list[str]] = {}
    for pbi in run.get("pbis", []):
        for bid in pbi.get("backlog") or []:
            out.setdefault(str(bid), []).append(pbi["id"])
    return out


def _write_lines(path: Path, lines: list[str]) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
    os.replace(temp, path)


def _released(entry: dict[str, Any]) -> dict[str, Any]:
    """The entry with no run's mark: a candidate again for the next runs."""
    out = {k: v for k, v in entry.items() if k not in ("taken_pbis", "taken_at")}
    out["taken_by"] = None
    return out


def _release(path: Path, run: dict[str, Any], ids: set[str] | None, keep: dict[str, list[str]]) -> list[str]:
    """Clear this run's mark on the entries it took that `keep` does not hold (only those in `ids`, when given).

    Returns the ids released; they leave run["backlog"]["taken"] and are kept in run["backlog"]["released"].
    """
    run_id = run.get("run_id")
    kept = _status(run)
    released: list[str] = []
    out_lines: list[str] = []
    for line, entry in _read_lines(path):
        if (
            entry is not None
            and run_id
            and entry.get("taken_by") == run_id
            and entry["id"] not in keep
            and (ids is None or entry["id"] in ids)
        ):
            line = _dumps(_released(entry))
            released.append(entry["id"])
        out_lines.append(line)
    if released:
        _write_lines(path, out_lines)
    if released or kept.get("taken"):
        kept["taken"] = [t for t in kept.get("taken") or [] if t not in released]
    if released:
        gone = kept.setdefault("released", [])
        gone += [r for r in released if r not in gone]
    return released


def take(root: Path, run: dict[str, Any]) -> list[str]:
    """Once the plan is approved, write on each backlog entry its PBIs name the run that took it (taken_by), and the
    PBIs. An entry another run took first keeps that run (kept in run["backlog"]["taken_elsewhere"]). An entry this run
    took for an earlier approval of its plan that the plan no longer names (a re-plan dropped its PBI) is released: no
    run holds it, and it is a candidate again. Returns the ids this run took."""
    wanted = named(run)
    kept = _status(run)
    if not wanted and not kept.get("taken"):
        return []
    path = location(Path(root))
    if path is None:
        return []
    # One lock from the first read to the last write, so two runs do not both take an entry, and an append of
    # another run does not fall between this read and the write that replaces the file.
    with _locked(path):
        return _take(path, run, wanted)


def _take(path: Path, run: dict[str, Any], wanted: dict[str, list[str]]) -> list[str]:
    """take, with the backlog's lock held."""
    kept = _status(run)
    if kept.get("taken"):
        _release(path, run, None, wanted)
    if not wanted:
        return list(kept.get("taken") or [])
    run_id = run.get("run_id")
    taken = kept.setdefault("taken", [])
    elsewhere = kept.setdefault("taken_elsewhere", [])
    rows = _read_lines(path)
    changed = False
    out_lines: list[str] = []
    for line, entry in rows:
        if entry is not None and entry["id"] in wanted:
            owner = entry.get("taken_by")
            if owner and owner != run_id:
                if entry["id"] not in elsewhere:
                    elsewhere.append(entry["id"])
            elif not owner or entry.get("taken_pbis") != wanted[entry["id"]]:
                entry = {**entry, "taken_by": run_id, "taken_pbis": wanted[entry["id"]], "taken_at": entry.get("taken_at") or _now()}
                line = _dumps(entry)
                changed = True
            if entry.get("taken_by") == run_id and entry["id"] not in taken:
                taken.append(entry["id"])
        out_lines.append(line)
    if changed:
        _write_lines(path, out_lines)
    kept["released"] = [r for r in kept.get("released") or [] if r not in taken]
    if not kept["released"]:
        kept.pop("released")
    return list(taken)


def release_unpassed(root: Path, run: dict[str, Any]) -> list[str]:
    """At the report, release the entries this run took whose naming PBIs all did not pass (held, unfinished and the
    like): the run did not fix them, so they are candidates again. Returns the ids released."""
    kept = _status(run)
    if not kept.get("taken"):
        return []
    path = location(Path(root))
    if path is None:
        return []
    with _locked(path):
        return _release_unpassed(path, run)


def _release_unpassed(path: Path, run: dict[str, Any]) -> list[str]:
    """release_unpassed, with the backlog's lock held."""
    kept = _status(run)
    if not kept.get("taken"):
        return []
    status = {p.get("id"): p.get("status") for p in run.get("pbis", [])}
    by = named(run)
    keep = {
        bid: pbis
        for bid, pbis in by.items()
        if any(status.get(pid) == "passed" for pid in pbis)
    }
    return _release(path, run, set(kept.get("taken") or []), keep)


def headline(run: dict[str, Any]) -> str:
    """The report's first line's note when the run left anything over ("" when it did not)."""
    left = [lo for lo in run.get("leftovers") or [] if isinstance(lo, dict)]
    if not left:
        return ""
    kept = run.get("backlog") or {}
    if not kept.get("git"):
        return f"未完了: 残り {len(left)} 件（{NOT_GIT} backlog に入れていない）"
    if kept.get("error"):
        return f"未完了: 残り {len(left)} 件（backlog に書けなかった）"
    return f"未完了: 残り {len(left)} 件を backlog に入れた"


def report_lines(run: dict[str, Any]) -> list[str]:
    """The report's lines on the backlog: where the leftovers went, and the entries this run took."""
    kept = run.get("backlog") or {}
    left = [lo for lo in run.get("leftovers") or [] if isinstance(lo, dict)]
    if not kept.get("git"):
        tail = f"（残り {len(left)} 件はこの報告の「残り」にだけある）" if left else ""
        lines = [f"- backlog: {NOT_GIT}、backlog を読み書きしていない{tail}"]
    elif kept.get("error"):
        lines = [f"- backlog: 残り {len(left)} 件を backlog に書けなかった（{kept['error']}）"]
    elif left:
        ids = "、".join(str(lo.get("id")) for lo in left)
        lines = [f"- backlog: 残り {len(left)} 件（{ids}）を {kept.get('path')} に入れた"]
    else:
        lines = ["- backlog: 残りが無いため、backlog に入れたものは無い"]
    taken = kept.get("taken") or []
    if taken:
        by = named(run)
        lines.append("- backlog から取り込んだ件: " + "、".join(f"{t}（{'、'.join(by.get(t) or [])}）" for t in taken))
    if kept.get("released"):
        lines.append("- backlog に戻した件（この run で直さなかった）: " + "、".join(kept["released"]))
    if kept.get("taken_elsewhere"):
        lines.append("- 別の run が先に取り込んでいた backlog の件: " + "、".join(kept["taken_elsewhere"]))
    return lines
