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

An e2e PBI that passes its wave check in a locked run adds the E2E files it wrote (the files in its targets that are
there) to the lock (extend): the list is written again with those files, and with the keys of the files workers also
touch that its targets name and the package.json scripts the plan's e2e commands run; the lock commit moves on to a
new commit on top of the branch holding them and the new list, and the copy outside the work tree, the commit id and
the hash are written again. From then on G1 and the wave check guard those files and keys like the acceptance tests.

When the user fixes the plan in the middle of the run, the lock comes off once the new plan is approved (lift): the
acceptance PBIs of the requirements it changed are built again and checked, and the next entry into building locks
the tests again, with a new list, a new lock commit on top of the branch, and a new copy, commit id and hash. The
E2E files of the e2e PBIs that passed before then are locked again with them, with the content the earlier lock
commit held, not what the work tree holds after the lift.

When the retrospective takes the lock off locked cases (an unlock), the fix stage opens only their acceptance test
file to the one fix worker of that unlock (without, for its launch's G1). The run then checks the fixed and added
lines: in a temporary git worktree outside the work tree at the lock commit (add_worktree, removed again with git
worktree remove and prune by remove_worktree) they must fail, and in the work tree they must end as decided. When
they do, relock commits the fixed file (the content the check judged) and then the list with the unlock record, and
writes the copy, the commit id and the hash again; when they do not, the file goes back to the lock commit's content
(restore).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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
        # The subcommand, past the options in front of it (-c name=value).
        name = next((a for a in args if not a.startswith("-") and "=" not in a), args[0])
        raise state.StateError(f"lock: git {name} failed: " + proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout.decode("utf-8", "surrogateescape")


def _norm(path: Any) -> str:
    # A target is a path, or {"path": ..., "kind": "unit"}.
    if isinstance(path, dict):
        path = path.get("path") or ""
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
    """True when the run puts acceptance first and has not locked its acceptance tests yet, or has taken the lock off
    to lock them again (lift)."""
    return bool(run.get("acceptance_first")) and not run.get("lock")


def lift(run: dict[str, Any], plan_return: str | None = None) -> dict[str, Any] | None:
    """Take the lock off after the user fixed the plan in the middle of the run, until it is made again.

    The acceptance stage then builds the revised acceptance tests as it did before the first lock (G1 does not guard a
    lock that is off, and no build or e2e PBI is launched: never BuildBeforeLock). The next entry into building locks
    the tests again (lock) with a new lock list, lock commit, copy outside the work tree and hash. The old lock is kept
    in run["lock_history"], with the plan return it was lifted for; its commit stays in the branch. Returns it.
    """
    kept = run.get("lock")
    if not kept:
        return None
    run.setdefault("lock_history", []).append({**kept, "plan_return": plan_return})
    run["lock"] = None
    return kept


def locked_requirements(run: dict[str, Any]) -> list[str]:
    """The requirements whose tests are locked: every scenario but those left unresolved (no_lock)."""
    return [sc["id"] for sc in acceptance.scenarios(run) if not sc.get("no_lock")]


def locked_cases(run: dict[str, Any]) -> dict[str, str]:
    """Each locked case -> its requirement, in plan order; nothing when the run has no lock."""
    if not run.get("lock"):
        return {}
    locked = set(locked_requirements(run))
    return {
        str(case["id"]): sc["id"]
        for sc in acceptance.scenarios(run)
        if sc["id"] in locked
        for case in sc.get("cases") or []
        if isinstance(case, dict) and case.get("id")
    }


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
        out.update(f for f in found if f not in left and _lockable(root, f))
    return sorted(out)


def _lockable(root: Path, name: str) -> bool:
    """A file the lock may take whole: not under docs/pbi/, there, not a symlink, and not a file whose keys are locked
    instead."""
    return (
        not name.startswith("docs/pbi/") and (root / name).is_file() and not (root / name).is_symlink()
        and signatures.key_spec(name) is None
    )


def passed_e2e(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The e2e PBIs that passed their wave check."""
    return [p for p in run.get("pbis", []) if p.get("kind") == "e2e" and p.get("status") == "passed"]


def e2e_files(root: Path, pbis: list[dict[str, Any]]) -> list[str]:
    """The work-root-relative E2E files of e2e PBIs: the files in their targets that are there (a directory or glob
    target covers the files in it). Files under docs/pbi/, symlinks and the files whose keys are locked instead
    (package.json and the like) are left out."""
    root = Path(root).resolve()
    names = _listed(root)
    out: set[str] = set()
    for pbi in pbis:
        if pbi.get("kind") != "e2e":
            continue
        for target in pbi.get("targets") or []:
            path = _norm(target)
            if not path:
                continue
            if (root / path).is_file():
                out.add(path)
            out.update(n for n in names if guards.covered(n, [path]))
    return sorted(f for f in out if _lockable(root, f))


def _carried_e2e(root: Path, run: dict[str, Any], pbis: list[dict[str, Any]]) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """The E2E files an earlier lock took for e2e PBIs, to lock again as they were (a lock made again after a lift).

    For each PBI, the latest lock in run["lock_history"] that took it (its e2e_pbis) gives the files of its targets
    that lock held, as path -> (mode, blob) in that lock commit: a passed PBI does not change in a revised plan, so
    what its wave check passed is what is locked again, whatever the work tree holds now. Returns those entries and
    the ids of the PBIs found so; the others' files are read from the work tree.
    """
    history = [h for h in reversed(run.get("lock_history") or []) if isinstance(h, dict) and h.get("commit")]
    entries: dict[str, tuple[str, str]] = {}
    taken: set[str] = set()
    for pbi in pbis:
        old = next((h for h in history if pbi["id"] in (h.get("e2e_pbis") or [])), None)
        if old is None:
            continue
        taken.add(pbi["id"])
        list_path = old.get("list") or LIST_PATH
        targets = [t for t in (_norm(t) for t in pbi.get("targets") or []) if t]
        mine = [str(f) for f in old.get("files") or [] if f != list_path and guards.covered(str(f), targets)]
        if not mine:
            continue
        got = _tree_entries(root, old["commit"], mine)
        missing = [f for f in mine if f not in got]
        if missing:
            raise state.StateError(f"lock: the earlier lock commit {old['commit']} lacks {', '.join(missing)}")
        entries.update(got)
    return entries, taken


def key_files(root: Path, run: dict[str, Any], e2e: list[dict[str, Any]] | None = None) -> list[str]:
    """The files workers also touch whose keys are locked: those at the work root, those an acceptance PBI with a
    locked requirement has in its targets, and those the e2e PBIs in e2e have in their targets."""
    root = Path(root).resolve()
    names = _listed(root)
    out = {name for name in signatures.KEY_FILES if (root / name).is_file()}
    locked = set(locked_requirements(run))
    owners = acceptance.requirement_owners(run)
    pbis = [
        p for p in run.get("pbis", [])
        if p.get("kind") == "acceptance" and any(owners.get(rid) == p["id"] for rid in locked)
    ]
    out.update(_target_key_files(root, names, pbis + [p for p in e2e or [] if p.get("kind") == "e2e"]))
    return sorted(out)


def _target_key_files(root: Path, names: list[str], pbis: list[dict[str, Any]]) -> list[str]:
    """The files workers also touch that the targets of pbis cover (a directory or glob target covers the files in it),
    less those under docs/pbi/, symlinks and files that are not there."""
    out: set[str] = set()
    for pbi in pbis:
        for target in pbi.get("targets") or []:
            path = _norm(target)
            if path:
                out.update(n for n in names if signatures.key_spec(n) is not None and guards.covered(n, [path]))
    return sorted(f for f in out if not f.startswith("docs/pbi/") and (root / f).is_file() and not (root / f).is_symlink())


def lock_commands(run: dict[str, Any], e2e: bool = False) -> list[str]:
    """The commands that run the tests, whose package.json scripts are locked keys; with e2e, the plan's e2e commands
    too (once an e2e PBI has passed, the scripts they run are locked like its E2E files)."""
    commands = [*(run.get("acceptance_commands") or []), *(run.get("plan_checks") or [])]
    if e2e:
        commands += list(run.get("plan_e2e") or [])
    return [str(c) for c in commands if c]


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


def _commit_only(
    root: Path,
    paths: list[str],
    message: str,
    keep: dict[str, tuple[str, str]] | None = None,
    given: dict[str, tuple[str, bytes]] | None = None,
) -> str:
    """Commit the working-tree content of paths, and nothing else, on top of HEAD; return the commit.

    The commit is built in a separate index, so changes the user staged or left in other files stay out of it.
    given (path -> (mode, content)) commits that content instead of what the work tree holds, converted as git add
    converts the file at that path (line endings, for example). keep (path -> (mode, blob)) puts those entries in the
    commit as given, whatever the work tree holds (the files an earlier lock commit locked). The real index then takes
    the committed content of paths and given, and of each kept path whose index entry still matches HEAD before the
    commit: a change the user staged in a kept path stays staged. Hooks do not run (git commit-tree).
    """
    keep = dict(keep or {})
    given = dict(given or {})
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ).stdout.decode("utf-8").strip()
    entries = dict(keep)
    for path, (mode, content) in given.items():
        entries[path] = (mode, _git(root, "hash-object", "-w", f"--path={path}", "--stdin", stdin=content).strip())
    with tempfile.TemporaryDirectory(prefix="atelier-lock-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if head:
            _git(root, "read-tree", head, env=env)
        if entries:
            info = "".join(f"{mode} {sha}\t{path}\0" for path, (mode, sha) in entries.items())
            _git(root, "update-index", "-z", "--index-info", env=env, stdin=info.encode("utf-8", "surrogateescape"))
        if paths:
            _git(root, "add", "-f", "--", *paths, env=env)
        tree = _git(root, "write-tree", env=env).strip()
    carried = [p for p in keep if p not in given]
    if carried:
        before = _tree_entries(root, head, carried) if head else {}
        staged = _index_entries(root, carried)
        carried = [p for p in carried if staged.get(p) == before.get(p)]
    if head and tree == _git(root, "rev-parse", head + "^{tree}").strip():
        commit = head
    else:
        commit = _git(root, "commit-tree", tree, "-m", message, *(["-p", head] if head else [])).strip()
        _git(root, "update-ref", "-m", message, "HEAD", commit, *([head] if head else []))
    reset = [*paths, *given, *carried]
    if reset:
        _git(root, "reset", "-q", commit, "--", *reset)
    return commit


_UNMERGED = ("unmerged", "")


def _index_entries(root: Path, paths: list[str]) -> dict[str, tuple[str, str]]:
    """path -> (mode, blob id) in the real index for the paths it holds; a path with a merge conflict (stages 1 to 3)
    gets _UNMERGED, which matches no commit."""
    out = _git(root, "ls-files", "-s", "-z", "--", *paths)
    entries: dict[str, tuple[str, str]] = {}
    for item in filter(None, out.split("\0")):
        meta, name = item.split("\t", 1)
        mode, sha, stage = meta.split()
        entries[name] = (mode, sha) if stage == "0" else _UNMERGED
    return entries


def _tree_entries(root: Path, commit: str, paths: list[str]) -> dict[str, tuple[str, str]]:
    """path -> (mode, blob id) in commit, for the paths that are files in it."""
    out = _git(root, "ls-tree", "-r", "-z", commit, "--", *paths)
    entries: dict[str, tuple[str, str]] = {}
    for item in filter(None, out.split("\0")):
        meta, name = item.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob":
            entries[name] = (mode, sha)
    return entries


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
    e2e = passed_e2e(run)
    accepted = lock_files(root, run)
    carried, taken = _carried_e2e(root, run, e2e)
    carried = {f: entry for f, entry in carried.items() if f not in accepted}
    files = sorted({*accepted, *carried, *e2e_files(root, [p for p in e2e if p["id"] not in taken])})
    sigs, unextractable = signatures.extract(root, [i for i in run.get("interfaces") or [] if isinstance(i, dict)])
    keys, unreadable = signatures.read_keys(root, key_files(root, run, e2e), lock_commands(run, e2e=bool(e2e)))
    data = lock_list(run, files, sigs, keys, unextractable + unreadable)
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    list_file = root / LIST_PATH
    list_file.parent.mkdir(parents=True, exist_ok=True)
    list_file.write_bytes(raw)
    paths = [*files, LIST_PATH]
    commit = _commit_only(root, [p for p in paths if p not in carried], f"atelier: lock the acceptance tests ({run_id})", carried)
    blobs = tree_blobs(root, commit, paths)
    missing = [p for p in paths if p not in blobs] + [f for f in carried if blobs.get(f) != carried[f][1]]
    if missing:
        raise state.StateError(f"lock: the lock commit {commit} lacks or changed {', '.join(missing)}")
    outside, digest, target = _keep_outside(root, run_id, raw, commit)

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
    if e2e:
        run["lock"]["e2e_pbis"] = sorted(p["id"] for p in e2e)
    return run["lock"]


def _keep_outside(root: Path, run_id: str, raw: bytes, commit: str) -> tuple[Path, str, Path]:
    """Write the copy of the list and the lock commit's id outside the work tree, and the copy's sha256 under the locks
    home. Returns the folder outside, the sha256 and the hash file."""
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
    return outside, digest, target


def extend(root: Path, run: dict[str, Any], pbis: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Add the E2E files of the e2e PBIs that passed their wave check to the lock, and move the lock commit on.

    pbis defaults to every e2e PBI that passed; those the lock already took (run["lock"]["e2e_pbis"]) are skipped.
    Their files that are there and not locked yet join the list's files. The keys of the files workers also touch
    (package.json and the like) that their targets name join the list's keys, by the same rules as an acceptance PBI's,
    and so do the package.json scripts the plan's e2e commands run, in the files already keyed; a key the list holds
    keeps its locked value. The list is written again (the rest of it as the copy outside the work tree holds it),
    then committed with the new files on top of HEAD; the files the lock already held go into that commit with the old
    lock commit's content. The copy outside the work tree, the commit id and the hash are written again, and
    run["lock"] takes the new commit, files and sha256. Returns the extension kept in run["lock_extensions"] (with
    "keys", each "<file> <key>", when keys were added), or None when the lock is off or there is nothing to add.
    """
    kept = run.get("lock")
    if not kept:
        return None
    root = Path(root).resolve()
    taken = set(kept.get("e2e_pbis") or [])
    fresh = [p for p in (passed_e2e(run) if pbis is None else pbis) if p.get("kind") == "e2e" and p["id"] not in taken]
    if not fresh:
        return None
    ids = sorted(taken | {p["id"] for p in fresh})
    list_path = kept.get("list") or LIST_PATH
    held = [f for f in kept.get("files") or [] if f != list_path]
    added = [f for f in e2e_files(root, fresh) if f not in held and f != list_path]
    listed = locked_list(root, run)
    keys = {str(f): dict(v) for f, v in (listed.get("keys") or {}).items() if isinstance(v, dict)}
    # The files workers also touch that the e2e PBIs' targets name get their keys locked, and the scripts the plan's
    # e2e commands run join the keys of the files already keyed; a key the lock holds keeps its locked value.
    found, unreadable = signatures.read_keys(
        root, sorted({*_target_key_files(root, _listed(root), fresh), *keys}), lock_commands(run, e2e=True)
    )
    keyed: list[str] = []
    for name, values in found.items():
        for key, value in values.items():
            if key not in keys.get(name, {}):
                keys.setdefault(name, {})[key] = value
                keyed.append(f"{name} {key}")
    unextractable = list(listed.get("unextractable") or [])
    unextractable += [u for u in unreadable if u.get("file") not in keys and u not in unextractable]
    if not added and not keyed:
        kept["e2e_pbis"] = ids
        return None

    files = sorted({*[str(f) for f in listed.get("files") or held], *added})
    data = {**listed, "files": files, "keys": keys, "unextractable": unextractable}
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    before = kept["commit"]
    keep = _tree_entries(root, before, held)
    missing = [f for f in held if f not in keep]
    if missing:
        raise state.StateError(f"lock: the lock commit {before} lacks {', '.join(missing)}")
    list_file = root / list_path
    list_file.parent.mkdir(parents=True, exist_ok=True)
    list_file.write_bytes(raw)
    message = f"atelier: lock the E2E tests of {', '.join(p['id'] for p in fresh)} ({run['run_id']})"
    commit = _commit_only(root, [*added, list_path], message, keep)
    paths = sorted({*held, *added}) + [list_path]
    blobs = tree_blobs(root, commit, paths)
    wrong = [p for p in paths if p not in blobs] + [f for f in held if blobs.get(f) != keep[f][1]]
    if wrong:
        raise state.StateError(f"lock: the new lock commit {commit} lacks or changed {', '.join(wrong)}")
    outside, digest, target = _keep_outside(root, run["run_id"], raw, commit)

    kept.update(commit=commit, files=paths, outside=str(outside), sha256=digest, hash_file=str(target), e2e_pbis=ids)
    kept["unextractable"] = unextractable
    extension = {"pbis": [p["id"] for p in fresh], "files": added, "commit_before": before, "commit": commit}
    if keyed:
        extension["keys"] = keyed
    run.setdefault("lock_extensions", []).append(extension)
    return extension


def relock(
    root: Path,
    run: dict[str, Any],
    files: list[str],
    record: dict[str, Any],
    added: dict[str, str | None] | None = None,
    checked: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Lock the acceptance tests again after an unlock whose fixed and added lines were checked.

    files are the locked files the unlock opened, as its fix worker left them in the work tree. checked (file -> the
    sha256 of the content the unlock's check judged) makes each such file commit exactly that content: the file is
    read once, refused (StateError, nothing moves) when the bytes read are not the checked ones, and those bytes are
    what is committed, so a write after the check cannot get into the lock. First they are
    committed on top of HEAD, with every other locked file and the list as the lock commit held them: that commit is
    the unlock record's commit (the fixed tests), and the lock commit before it is its commit_before. Then the list is
    written again, with the record added to its unlocks and the added cases (case id -> the PBI it is assigned to, or
    None) added to its cases and assignments, and committed on top: that is the new lock commit (a list cannot name
    the commit it is in). The copy outside the work tree, the commit id and the hash are written again, run["lock"]
    takes the new commit and sha256, and run["unlocks"] keeps the record, so a lock made again keeps it too. Returns
    the record as the list holds it.
    """
    kept = run.get("lock")
    if not kept:
        raise state.StateError("lock: the run has no lock to take off")
    root = Path(root).resolve()
    list_path = kept.get("list") or LIST_PATH
    held = [f for f in kept.get("files") or [] if f != list_path]
    opened = [f for f in dict.fromkeys(files) if f in held]
    if not opened:
        raise state.StateError(f"lock: {', '.join(files) or '(no file)'} is not a locked file")
    before = kept["commit"]
    stay = [f for f in held if f not in opened] + [list_path]
    keep = _tree_entries(root, before, stay)
    missing = [f for f in stay if f not in keep]
    if missing:
        raise state.StateError(f"lock: the lock commit {before} lacks {', '.join(missing)}")
    listed = locked_list(root, run)
    cases = [str(c) for c in listed.get("cases") or []]
    assignments = {str(k): str(v) for k, v in (listed.get("assignments") or {}).items()}
    for cid, pbi in (added or {}).items():
        if cid not in cases:
            cases.append(cid)
        if pbi:
            assignments[cid] = pbi
    given: dict[str, tuple[str, bytes]] = {}
    modes = _tree_entries(root, before, opened)
    for name in opened:
        if not checked or name not in checked:
            continue
        try:
            content = (root / name).read_bytes()
        except OSError as exc:
            raise state.StateError(f"lock: {name} cannot be read to lock it again ({type(exc).__name__})") from exc
        if hashlib.sha256(content).hexdigest() != checked[name]:
            raise state.StateError(f"lock: {name} changed after its check; the checked content is not there to lock again")
        given[name] = (modes[name][0] if name in modes else "100644", content)
    label = ", ".join(str(c) for c in record.get("cases") or []) or ", ".join(opened)
    fixed = _commit_only(
        root,
        [f for f in opened if f not in given],
        f"atelier: commit the fixed tests of the unlock of {label} ({run['run_id']})",
        keep,
        given,
    )
    entry = {**record, "commit_before": before, "commit": fixed}
    data = {**listed, "cases": cases, "assignments": assignments, "unlocks": [*(listed.get("unlocks") or []), entry]}
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    list_file = root / list_path
    list_file.parent.mkdir(parents=True, exist_ok=True)
    list_file.write_bytes(raw)
    commit = _commit_only(root, [list_path], f"atelier: lock the acceptance tests again after the unlock of {label} ({run['run_id']})")
    paths = [*held, list_path]
    blobs = tree_blobs(root, commit, paths)
    fixed_blobs = tree_blobs(root, fixed, opened)
    wrong = [p for p in paths if p not in blobs]
    wrong += [f for f in stay if f != list_path and blobs.get(f) != keep[f][1]]
    wrong += [f for f in opened if f not in fixed_blobs or blobs.get(f) != fixed_blobs[f]]
    if wrong:
        raise state.StateError(f"lock: the new lock commit {commit} lacks or changed {', '.join(sorted(set(wrong)))}")
    outside, digest, target = _keep_outside(root, run["run_id"], raw, commit)
    kept.update(commit=commit, outside=str(outside), sha256=digest, hash_file=str(target))
    run.setdefault("unlocks", []).append(entry)
    return entry


# A temporary worktree is the folder WORKTREE_NAME in a new folder named WORKTREE_PREFIX... outside the work tree.
WORKTREE_PREFIX = "atelier-unlock-"
WORKTREE_NAME = "tree"


def add_worktree(root: Path, commit: str) -> Path:
    """A temporary git worktree of commit (detached HEAD), in a new folder outside the work tree; hooks do not run.

    Returns the worktree's folder; remove_worktree takes it away again. When making it fails, what was made is taken
    away before the error (state.StateError) is raised. A temporary folder inside the work tree (TMPDIR set to a
    folder in it, for example) is refused, since the worktree must be outside it.
    """
    root = Path(root).resolve()
    try:
        base = Path(tempfile.mkdtemp(prefix=WORKTREE_PREFIX)).resolve()
    except OSError as exc:
        raise state.StateError(f"lock: the temporary folder for the worktree cannot be made ({exc})") from exc
    tree = base / WORKTREE_NAME
    hooks = base / "no-hooks"
    try:
        if base == root or root in base.parents:
            raise state.StateError(
                f"lock: the temporary folder {base} is inside the work tree {root}; set TMPDIR (TEMP on Windows) "
                "to a folder outside it"
            )
        hooks.mkdir()
    except (OSError, state.StateError) as exc:
        shutil.rmtree(base, ignore_errors=True)
        if isinstance(exc, state.StateError):
            raise
        raise state.StateError(f"lock: the temporary folder {base} cannot be set up ({exc})") from exc
    try:
        _git(root, "-c", f"core.hooksPath={hooks}", "worktree", "add", "--detach", str(tree), commit)
    except state.StateError:
        remove_worktree(root, tree)
        raise
    return tree


def remove_worktree(root: Path, tree: Path | str) -> list[str]:
    """Take a temporary worktree away: git worktree remove, then its folder, then git worktree prune.

    Only a folder add_worktree made (its name and the folder around it) is deleted. Returns what could not be done;
    empty when the worktree's folder is gone and git lists it no more.
    """
    root = Path(root).resolve()
    tree = Path(tree)
    problems: list[str] = []
    try:
        _git(root, "worktree", "remove", "--force", str(tree))
    except state.StateError as exc:
        if tree.exists():
            problems.append(str(exc))
    base = tree.parent
    if tree.name == WORKTREE_NAME and base.name.startswith(WORKTREE_PREFIX) and base != root and root not in base.parents:
        shutil.rmtree(base, ignore_errors=True)
    try:
        _git(root, "worktree", "prune")
    except state.StateError as exc:
        problems.append(str(exc))
    if tree.exists():
        problems.append(f"一時的な worktree のフォルダが残った: {tree}")
    elif _worktree_listed(root, tree):
        problems.append(f"一時的な worktree が git worktree list に残った: {tree}")
    return problems


def _worktree_listed(root: Path, tree: Path) -> bool:
    try:
        out = _git(root, "worktree", "list", "--porcelain")
    except state.StateError:
        return False
    listed = [line[len("worktree "):].strip() for line in out.splitlines() if line.startswith("worktree ")]
    return any(os.path.normcase(os.path.normpath(p)) == os.path.normcase(os.path.normpath(str(tree))) for p in listed)


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


def without(entry: dict[str, Any] | None, files: list[str] | None) -> dict[str, Any] | None:
    """A launch's lock entry (guard_entry, or a snapshot's locked) less the files an unlock opens to that launch: G1
    lets their change through. Every other locked file, the list, the signatures and the keys stay guarded."""
    if not entry or not files:
        return entry
    opened = set(files)
    return {**entry, "blobs": {name: blob for name, blob in (entry.get("blobs") or {}).items() if name not in opened}}


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
