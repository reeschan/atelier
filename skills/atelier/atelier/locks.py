"""The lock on the acceptance tests: the lock list, the lock commit, the copy outside the work tree and its hash.

With acceptance_first on, once the planner's acceptance review is done the conductor locks the acceptance tests
before any build or e2e PBI is launched. It writes the lock list docs/pbi/locks.json (the locked files, the case ids,
the case assignments, the unlock record, and the signature and key fields), then commits the locked files and the
list, and nothing else, as the lock commit. A copy of the list and the lock commit's id go to
<git common dir>/atelier/<run>/, and the copy's sha256 to ~/.config/atelier/locks/<run>.json (ATELIER_LOCKS_HOME
moves that folder). The list in the work tree is what workers read; the copy outside it and the lock commit are what
the checks go by.

After the lock every launch's snapshot carries the lock commit's blobs, so G1 stops a launch that changes a locked
file or the list; the conductor then puts the file back to the lock commit's content. Files are compared as git
blobs, so a change of line endings that git converts away is not a change.

The list also holds the signatures of the plan's public interfaces (atelier.signatures: Python with ast, TypeScript
from tsc's declarations, Go with go doc) and the interfaces whose signature cannot be taken, and the compared keys
of the files workers also touch (package.json, pyproject.toml and the like): those files are not locked whole, only
their keys are. A launch that changes a locked signature or key is stopped by G1 too; the key is put back to its
locked value, the source file of a signature is left as the worker wrote it.

A locked run's wave check starts with the lock itself: the copy's sha256 against the record (copy_problem; a
difference stops the check and asks the user, since it is a fault of the lock, not of a worker), then the locked
files, signatures and keys against the lock (wave_changes), whenever they were changed. A changed file or key is put
back; each change fails the PBIs of the wave that have its path in their targets, or is left unresolved.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import acceptance, guards, signatures, state

SCHEMA = "atelier.locks/1"
HASH_SCHEMA = "atelier.lock_hash/1"
LIST_PATH = "docs/pbi/locks.json"
HOME_ENV = "ATELIER_LOCKS_HOME"
COPY_NAME = "locks.json"
COMMIT_NAME = "lock_commit"


def _git(root: Path, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None) -> str:
    # Paths are literal: a locked file named acceptance/case[1].py must not also take acceptance/case1.py.
    proc = subprocess.run(
        ["git", "--literal-pathspecs", "-C", str(root), *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.returncode:
        raise state.StateError(f"lock: git {args[0]} failed: " + proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout.decode("utf-8", "surrogateescape")


def _norm(path: str) -> str:
    return str(path).strip().replace("\\", "/").removeprefix("./")


def locks_home() -> Path:
    """The folder of the lock hashes: ATELIER_LOCKS_HOME, or ~/.config/atelier/locks."""
    configured = os.environ.get(HOME_ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".config" / "atelier" / "locks"


def hash_path(run_id: str) -> Path:
    return locks_home() / f"{run_id}.json"


def outside_dir(root: Path, run_id: str) -> Path:
    """<git common dir>/atelier/<run>/: where the copy of the list and the lock commit's id are kept."""
    common = Path(_git(root, "rev-parse", "--git-common-dir").strip())
    common = (common if common.is_absolute() else Path(root) / common).resolve()
    return common / "atelier" / run_id


def lock_pending(run: dict[str, Any]) -> bool:
    """True when the run puts acceptance first and has not locked its acceptance tests yet."""
    return bool(run.get("acceptance_first")) and not run.get("lock")


def locked_requirements(run: dict[str, Any]) -> list[str]:
    """The requirements whose tests are locked: every scenario but those left unresolved (no_lock)."""
    return [sc["id"] for sc in acceptance.scenarios(run) if not sc.get("no_lock")]


def _listed(root: Path) -> list[str]:
    names = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    return [n for n in names if n]


def lock_files(root: Path, run: dict[str, Any]) -> list[str]:
    """The work-root-relative files to lock: what the acceptance PBIs wrote for the locked requirements.

    That is each locked requirement's own acceptance test file, and the other files in the targets of an acceptance
    PBI with a locked requirement (step definitions, fixtures, support code, the runner's settings); a directory or
    glob target covers the files in it. The own files of requirements left unresolved are not locked, unless a
    locked requirement shares them. Files under docs/pbi/ and files that are not there are left out, and so are the
    files workers also touch (package.json, pyproject.toml and the like): their keys are locked instead (key_files).
    """
    root = Path(root)
    names = _listed(root)
    locked = set(locked_requirements(run))
    owners = acceptance.requirement_owners(run)
    out: set[str] = set()
    for pbi in run.get("pbis", []):
        if pbi.get("kind") != "acceptance":
            continue
        reqs = [rid for rid, owner in owners.items() if owner == pbi["id"]]
        mine = [rid for rid in reqs if rid in locked]
        if not mine:
            continue
        own = {rid: _norm(path) for rid, path in (pbi.get("acceptance_files") or {}).items() if isinstance(path, str)}
        kept = {own[rid] for rid in mine if rid in own}
        left = {own[rid] for rid in reqs if rid not in locked and rid in own} - kept
        found = set(kept)
        for target in pbi.get("targets") or []:
            path = _norm(target)
            if not path:
                continue
            if (root / path).is_file():
                found.add(path)
            found.update(n for n in names if guards.covered(n, [path]))
        out.update(
            f for f in found
            if f not in left and not f.startswith("docs/pbi/") and (root / f).is_file() and not (root / f).is_symlink()
            and signatures.key_spec(f) is None
        )
    return sorted(out)


def key_files(root: Path, run: dict[str, Any]) -> list[str]:
    """The files workers also touch whose keys are locked: those at the work root, and those an acceptance PBI with a
    locked requirement has in its targets."""
    root = Path(root).resolve()
    names = _listed(root)
    out = {name for name in signatures.KEY_FILES if (root / name).is_file()}
    locked = set(locked_requirements(run))
    owners = acceptance.requirement_owners(run)
    for pbi in run.get("pbis", []):
        if pbi.get("kind") != "acceptance" or not any(owners.get(rid) == pbi["id"] for rid in locked):
            continue
        for target in pbi.get("targets") or []:
            path = _norm(target)
            if path:
                out.update(n for n in names if signatures.key_spec(n) is not None and guards.covered(n, [path]))
    return sorted(f for f in out if not f.startswith("docs/pbi/") and (root / f).is_file() and not (root / f).is_symlink())


def lock_commands(run: dict[str, Any]) -> list[str]:
    """The commands that run the tests, whose package.json scripts are locked keys."""
    return [str(c) for c in [*(run.get("acceptance_commands") or []), *(run.get("plan_checks") or [])] if c]


def lock_list(
    run: dict[str, Any],
    files: list[str],
    sigs: dict[str, dict[str, Any]] | None = None,
    keys: dict[str, dict[str, Any]] | None = None,
    unextractable: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The lock list: the locked files, the case ids and their assignments, the unlock record, the signatures of the
    public interfaces, the compared keys of the files workers also touch, and the interfaces (and key files) whose
    signature (or keys) cannot be taken."""
    locked = set(locked_requirements(run))
    cases: list[str] = []
    assignments: dict[str, str] = {}
    for sc in acceptance.scenarios(run):
        if sc["id"] not in locked:
            continue
        for case in sc.get("cases") or []:
            if not isinstance(case, dict) or not case.get("id"):
                continue
            cid = str(case["id"])
            cases.append(cid)
            if case.get("pbi"):
                assignments[cid] = str(case["pbi"])
    return {
        "schema": SCHEMA,
        "run": run["run_id"],
        "files": list(files),
        "requirements": [rid for rid in locked_requirements(run)],
        "cases": cases,
        "assignments": assignments,
        "unlocks": list(run.get("unlocks") or []),
        "signatures": dict(sigs or {}),
        "keys": dict(keys or {}),
        "unextractable": list(unextractable or []),
    }


def _commit_only(root: Path, paths: list[str], message: str) -> str:
    """Commit the working-tree content of paths, and nothing else, on top of HEAD; return the commit.

    The commit is built in a separate index, so changes the user staged or left in other files stay out of it.
    The real index then takes the committed content of paths. Hooks do not run (git commit-tree).
    """
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ).stdout.decode("utf-8").strip()
    with tempfile.TemporaryDirectory(prefix="atelier-lock-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if head:
            _git(root, "read-tree", head, env=env)
        _git(root, "add", "-f", "--", *paths, env=env)
        tree = _git(root, "write-tree", env=env).strip()
    if head and tree == _git(root, "rev-parse", head + "^{tree}").strip():
        commit = head
    else:
        commit = _git(root, "commit-tree", tree, "-m", message, *(["-p", head] if head else [])).strip()
        _git(root, "update-ref", "-m", message, "HEAD", commit, *([head] if head else []))
    _git(root, "reset", "-q", commit, "--", *paths)
    return commit


def tree_blobs(root: Path, commit: str, paths: list[str]) -> dict[str, str]:
    """path -> blob id in commit, for the paths that are files in it."""
    out = _git(root, "ls-tree", "-r", "-z", commit, "--", *paths)
    blobs: dict[str, str] = {}
    for item in filter(None, out.split("\0")):
        meta, name = item.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "blob":
            blobs[name] = sha
    return blobs


def lock(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    """Lock the acceptance tests: write the list, make the lock commit, keep the copy outside and write its hash."""
    root = Path(root).resolve()
    run_id = run["run_id"]
    files = lock_files(root, run)
    sigs, unextractable = signatures.extract(root, [i for i in run.get("interfaces") or [] if isinstance(i, dict)])
    keys, unreadable = signatures.read_keys(root, key_files(root, run), lock_commands(run))
    data = lock_list(run, files, sigs, keys, unextractable + unreadable)
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    list_file = root / LIST_PATH
    list_file.parent.mkdir(parents=True, exist_ok=True)
    list_file.write_bytes(raw)
    paths = [*files, LIST_PATH]
    commit = _commit_only(root, paths, f"atelier: lock the acceptance tests ({run_id})")
    blobs = tree_blobs(root, commit, paths)
    missing = [p for p in paths if p not in blobs]
    if missing:
        raise state.StateError(f"lock: the lock commit {commit} lacks {', '.join(missing)}")

    outside = outside_dir(root, run_id)
    outside.mkdir(parents=True, exist_ok=True)
    copy = outside / COPY_NAME
    copy.write_bytes(raw)
    (outside / COMMIT_NAME).write_bytes((commit + "\n").encode("ascii"))
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    target = hash_path(run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"schema": HASH_SCHEMA, "run": run_id, "sha256": digest, "copy": str(copy), "commit": commit}
    target.write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    run["lock"] = {
        "commit": commit,
        "list": LIST_PATH,
        "files": paths,
        "requirements": data["requirements"],
        "unextractable": data["unextractable"],
        "outside": str(outside),
        "sha256": digest,
        "hash_file": str(target),
    }
    return run["lock"]


def locked_list(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    """The lock list the checks go by: the copy outside the work tree, or the list in the lock commit, whichever
    first has the sha256 recorded when the lock was made (both hold the same bytes). The bytes that are hashed are
    the bytes that are read. When neither does, that is a fault of the lock (StateError), not an empty list."""
    kept = run.get("lock") or {}
    if not kept:
        return {}
    expected = kept.get("sha256")
    root = Path(root).resolve()

    def from_copy() -> bytes:
        return (Path(kept["outside"]) / COPY_NAME).read_bytes()

    def from_commit() -> bytes:
        return _git(root, "show", f"{kept['commit']}:{kept.get('list') or LIST_PATH}").encode("utf-8", "surrogateescape")

    for read in ([from_copy] if kept.get("outside") else []) + ([from_commit] if kept.get("commit") else []):
        try:
            raw = read()
        except (OSError, state.StateError):
            continue
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise state.StateError(
        "lock: neither the copy of the lock list outside the work tree nor the list in the lock commit "
        f"{kept.get('commit')} has the sha256 recorded at the lock ({expected})"
    )


def guard_entry(root: Path, run: dict[str, Any]) -> dict[str, Any] | None:
    """What a launch's snapshot carries for G1 after the lock: the lock commit, the blob of each locked file, and the
    locked signatures and keys."""
    kept = run.get("lock")
    if not kept:
        return None
    root = Path(root).resolve()
    listed = locked_list(root, run)
    return {
        "commit": kept["commit"],
        "blobs": tree_blobs(root, kept["commit"], list(kept["files"])),
        "signatures": dict(listed.get("signatures") or {}),
        "keys": dict(listed.get("keys") or {}),
    }


def violated(violations: list[dict[str, Any]]) -> list[str]:
    """The locked files named by G1 violations."""
    return sorted({
        str(v["detail"])[len(guards.LOCKED_DETAIL):]
        for v in violations
        if v.get("rule") == "G1" and str(v.get("detail", "")).startswith(guards.LOCKED_DETAIL)
    })


def restore(root: Path, run: dict[str, Any], names: list[str]) -> list[str]:
    """Put locked files back to the lock commit's content (git converts line endings as on checkout)."""
    kept = run.get("lock")
    names = [n for n in names if kept and n in kept["files"]]
    if names:
        _git(Path(root).resolve(), "restore", f"--source={kept['commit']}", "--worktree", "--", *names)
    return names


COPY_RULE = "lock_copy"
WAVE_RULE = "G1"


def copy_problem(root: Path, run: dict[str, Any]) -> str | None:
    """Why the copy of the lock list outside the work tree cannot be trusted, or None when it can.

    The first step of a locked run's wave check: the copy's sha256 against the record under the locks home, and the
    lock commit's id kept beside the copy against the run's. A difference is a fault of the lock itself, not a
    worker's failure.
    """
    kept = run.get("lock") or {}
    run_id = run["run_id"]
    record_file = hash_path(run_id)
    try:
        record = json.loads(record_file.read_text(encoding="utf-8"))
    except OSError:
        return f"鍵の一覧の写しのハッシュの記録が読めない: {record_file}"
    except ValueError:
        return f"鍵の一覧の写しのハッシュの記録が JSON として読めない: {record_file}"
    expected = record.get("sha256") if isinstance(record, dict) else None
    if not isinstance(expected, str) or not expected:
        return f"鍵の一覧の写しのハッシュの記録に sha256 が無い: {record_file}"
    try:
        outside = outside_dir(Path(root), run_id)
    except state.StateError as exc:
        return f"作業ツリーの外の鍵のフォルダが分からない（{exc}）"
    copy = outside / COPY_NAME
    if not copy.is_file():
        return f"鍵の一覧の写しが無い: {copy}"
    try:
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    except OSError as exc:
        return f"鍵の一覧の写しが読めない（{type(exc).__name__}）: {copy}"
    if digest != expected:
        return f"鍵の一覧の写し（{copy}）の sha256 が記録と違う（記録: {expected} / 今: {digest}）"
    if kept.get("sha256") and kept["sha256"] != expected:
        return f"鍵の一覧の写しのハッシュの記録（{record_file}）が鍵をかけたときの値と違う"
    commit_file = outside / COMMIT_NAME
    try:
        commit = commit_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return f"鍵のコミットの id が読めない: {commit_file}"
    if commit != kept.get("commit") or (record.get("commit") and record["commit"] != commit):
        return f"鍵のコミットの id（{commit_file}）が記録と違う"
    return None


def wave_changes(root: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The locked files, signatures and keys that differ from the lock now, as G1 violations with the path each is in.

    The wave check's second step: files are compared as git blobs with the lock commit, signatures and keys with the
    copy of the list outside the work tree. Unlike G1 during a launch, every difference counts, whenever it was made.
    """
    kept = run.get("lock")
    if not kept:
        return []
    root = Path(root).resolve()
    listed = locked_list(root, run)
    sigs = dict(listed.get("signatures") or {})
    try:
        blobs = tree_blobs(root, kept["commit"], list(kept["files"]))
        names = guards.locked_changes(root, blobs)
    except RuntimeError as exc:
        raise state.StateError(f"lock: {exc}") from exc
    out: list[dict[str, Any]] = [{"rule": WAVE_RULE, "path": name, "detail": guards.LOCKED_DETAIL + name} for name in names]
    for violation in guards.locked_signature_changes(root, {"signatures": sigs}):
        entry = sigs.get(signatures.interface_id(str(violation["module"]), str(violation["name"]))) or {}
        out.append({**violation, "path": str(entry.get("file") or "")})
    out += guards.locked_key_changes(root, {"keys": dict(listed.get("keys") or {})})
    return out


def change_owners(changes: list[dict[str, Any]], pbis: list[dict[str, Any]]) -> tuple[dict[str, list[str]], list[str]]:
    """Whose wave failure each change is: the PBIs of the wave that have its path in their targets.

    Returns PBI id -> the details laid on it, and the details no PBI of the wave owns (left unresolved).
    """
    owned: dict[str, list[str]] = {}
    unowned: list[str] = []
    for change in changes:
        path = str(change.get("path") or "")
        owners = [p["id"] for p in pbis if path and guards.covered(path, list(p.get("targets") or []))]
        for pid in owners:
            owned.setdefault(pid, []).append(str(change["detail"]))
        if not owners:
            unowned.append(str(change["detail"]))
    return owned, unowned


def changed_signatures(violations: list[dict[str, Any]]) -> list[str]:
    """The interfaces ("<module> <name>") named by G1 violations of a locked signature."""
    return sorted({
        f"{v.get('module')} {v.get('name')}"
        for v in violations
        if v.get("rule") == "G1" and str(v.get("detail", "")).startswith(guards.SIGNATURE_DETAIL)
    })


def restore_keys(root: Path, run: dict[str, Any], violations: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Put the keys named by G1 violations back to their value in the lock list. Returns the keys put back and the
    keys that could not be (each as "<file> <key>")."""
    named = sorted({
        (str(v["path"]), str(v["key"]))
        for v in violations
        if v.get("rule") == "G1" and str(v.get("detail", "")).startswith(guards.KEY_DETAIL) and v.get("path") and v.get("key")
    })
    if not named or not run.get("lock"):
        return [], []
    root = Path(root).resolve()
    locked = locked_list(root, run).get("keys") or {}
    done: list[str] = []
    failed: list[str] = []
    for path, key in named:
        if path not in locked or key not in locked[path]:
            failed.append(f"{path} {key}")
            continue
        if not (root / path).is_file():
            # A deleted file comes back from the lock commit when it is there; its key is then set below.
            try:
                _git(root, "restore", f"--source={run['lock']['commit']}", "--worktree", "--", path)
            except state.StateError:
                pass
        (done if signatures.restore_key(root, path, key, locked[path][key]) else failed).append(f"{path} {key}")
    return done, failed
