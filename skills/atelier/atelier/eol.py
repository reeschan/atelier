"""Undo CRLF line endings that a worker wrote into files that Git checks out with LF.

Some editors and tools on Windows save every file with CRLF, and a formatter that wants LF then fails the
check on a change that is otherwise fine. After a worker returns, a text file (no NUL byte) that has CRLF
goes back to LF when a checkout would give it LF:
- its eol attribute is lf; or
- it has no eol attribute, core.autocrlf is not true, and it had no CRLF at HEAD (for a new file: no
  tracked file with the same suffix has CRLF).
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _autocrlf(root: Path) -> bool:
    out = _git(root, "config", "--get", "core.autocrlf").stdout.decode("utf-8", "replace").strip().lower()
    return out == "true"


def _eol_attr(root: Path, rel: str) -> str:
    out = _git(root, "check-attr", "eol", "--", rel).stdout.decode("utf-8", "replace").strip()
    return out.rsplit(": ", 1)[-1] if out else ""


def _head_blob(root: Path, rel: str) -> bytes | None:
    proc = _git(root, "show", f"HEAD:{rel}")
    return proc.stdout if proc.returncode == 0 else None


def _crlf_suffixes(root: Path) -> set[str]:
    """Suffixes of tracked files whose index copy has CRLF line endings."""
    out = _git(root, "ls-files", "--eol").stdout.decode("utf-8", "replace")
    suffixes: set[str] = set()
    for line in out.splitlines():
        info, _, name = line.partition("\t")
        if info.split()[:1] in (["i/crlf"], ["i/mixed"]):
            suffixes.add(PurePosixPath(name).suffix)
    return suffixes


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.autocrlf = _autocrlf(root)
        self._crlf_suffixes: set[str] | None = None

    def checks_out_lf(self, name: str) -> bool:
        attr = _eol_attr(self.root, name)
        if attr in ("lf", "crlf"):
            return attr == "lf"
        if self.autocrlf:
            return False
        head = _head_blob(self.root, name)
        if head is not None:
            return b"\r\n" not in head
        if self._crlf_suffixes is None:
            self._crlf_suffixes = _crlf_suffixes(self.root)
        return PurePosixPath(name).suffix not in self._crlf_suffixes


def restore_lf(root: Path, paths: Iterable[str]) -> list[str]:
    """Rewrite CRLF to LF in the given repository-relative files that Git checks out with LF; return those."""
    root = Path(root).resolve()
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return []
    repo = _Repo(root)
    restored: list[str] = []
    for rel in dict.fromkeys(paths):
        path = Path(root / rel)
        if path.is_symlink() or not path.is_file():
            continue
        try:
            name = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        data = path.read_bytes()
        if b"\r\n" not in data or b"\0" in data or not repo.checks_out_lf(name):
            continue
        path.write_bytes(data.replace(b"\r\n", b"\n"))
        restored.append(name)
    return restored
