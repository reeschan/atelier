"""Acceptance first: the acceptance stage's PBIs and tickets, and the Red check that judges them.

With acceptance_first on, the acceptance and stub PBIs are built before any other PBI. The Red check then runs
each requirement's acceptance test file with the run's acceptance command and reads the JUnit XML it writes. A
requirement passes when every one of its cases ran and failed with an assertion or a not-implemented error. A
requirement that fails is written again by its acceptance PBI; one that fails twice is left unresolved, without
a lock, and the PBIs its cases are assigned to go on as in light mode.

The Red check also runs the step definition checks (stepcheck) on each requirement's file and on the other Python
and JS/TS files in its acceptance PBI's targets. A file that breaks a check fails its requirements, with the file
and the check in the reasons. Files in other languages are skipped and the report names those languages.

Once the tests are locked, every wave check runs the locked cases again with the acceptance command and JUnit XML
(wave_case_plan, judge_wave_cases): a case that did not run is a reason to send the wave back, a case assigned to a
PBI of the wave that did not pass fails that PBI, and a case its worker left red with an objection counts as run.

When the user fixes the plan in the middle of the run (a plan return, or a broken plan), the requirements whose
scenarios the new plan changed go back to the acceptance stage (reopen_revised): their acceptance PBIs are built
again, checked Red and read by the planner, and the tests are locked again. In that Red check a case assigned to a
PBI a worker has already built may pass (built_cases).

When the retrospective takes the lock off locked cases (an unlock: resolve_unlock), the fix stage has their
acceptance test file fixed as an acceptance PBI would write it. The fixed and added lines are then run with the
acceptance command twice (unlock_check_plan): at the lock commit, in a temporary worktree, where they must fail, and
in the work tree, where they must pass when the decision fixes the scenario and fail with an assertion when it fixes
the implementation. The other cases of the file must end as they did before, and the lines that name them must stay
as they were at the lock commit (judge_unlock).
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import guards, plan, state, stepcheck, template

STAGE_KINDS = ("acceptance", "stub")
NO_RED_MODE = "light-no-red"
# A requirement whose Red check fails this many times is left unresolved; before that its acceptance PBI is rebuilt.
RED_TRIES = 2
RED_FINAL = ("passed", "unresolved", "skipped")
RED_RULE = "red_check"
# A file the planner marks fix twice leaves its requirements unresolved; before that its acceptance PBI is rebuilt.
REVIEW_TRIES = 2
REVIEW_RULE = "acceptance_review"
REVIEW_FINAL = ("ok", "unresolved")
REVIEW_ROUTE_LEVEL = "L5"
_STAGE_DONE = ("passed", "held", "sensitive")
_GOOD_FAILURES = ("assertion", "not_implemented")

ASSERTION_TYPES = frozenset({"AssertionError", "AssertionFailedError", "JestAssertionError"})
NOT_IMPLEMENTED_TYPES = frozenset({"NotImplementedError", "NotImplementedException"})
_EXCEPTION_NAME = re.compile(r"\s*([A-Za-z_][\w.]*(?:Error|Exception))\b")
_NOT_IMPLEMENTED_TEXT = re.compile(r"not[ _-]?implemented", re.IGNORECASE)
# An untyped failure counts as an assertion when its message starts with assert or names expect / expected as a word.
_EXPECT_WORD = re.compile(r"(?<![A-Za-z0-9_])expect(?:ed)?(?![A-Za-z0-9_])", re.IGNORECASE)
MESSAGE_LIMIT = 200


def scenarios(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [sc for sc in run.get("scenarios") or [] if isinstance(sc, dict) and isinstance(sc.get("id"), str)]


def stage_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The acceptance and stub PBIs, in plan order."""
    return [p for p in run.get("pbis", []) if p.get("kind") in STAGE_KINDS]


def requirement_owners(run: dict[str, Any]) -> dict[str, str]:
    """Requirement id -> the acceptance PBI that writes its acceptance tests.

    An acceptance PBI names its requirements in `requirements`. The requirements that no acceptance PBI names go to
    the first acceptance PBI that names none. A requirement with no acceptance PBI is left out.
    """
    acceptance_pbis = [p for p in run.get("pbis", []) if p.get("kind") == "acceptance"]
    owners: dict[str, str] = {}
    for pbi in acceptance_pbis:
        for rid in pbi.get("requirements") or []:
            owners.setdefault(rid, pbi["id"])
    open_pbi = next((p["id"] for p in acceptance_pbis if not p.get("requirements")), None)
    if open_pbi is not None:
        for sc in scenarios(run):
            owners.setdefault(sc["id"], open_pbi)
    return owners


def red_entry(run: dict[str, Any], rid: str) -> dict[str, Any]:
    """The Red check's state of one requirement: status, failures so far, the last reasons and every result."""
    return run.setdefault("red", {}).setdefault(rid, {"status": "pending", "failures": 0, "reasons": [], "history": []})


def red_pending(run: dict[str, Any]) -> bool:
    """True when the run puts acceptance first and its acceptance stage or Red check is not done yet."""
    if not run.get("acceptance_first"):
        return False
    if any(p.get("status", "pending") not in _STAGE_DONE for p in stage_pbis(run)):
        return True
    red = run.get("red") or {}
    return any(
        sc.get("mode") != NO_RED_MODE and (red.get(sc["id"]) or {}).get("status") not in RED_FINAL for sc in scenarios(run)
    )


def review_files(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The acceptance test files the planner has still to read: {"file", "requirements"} for each file whose
    requirements passed the Red check and that the review has not settled yet."""
    settled = (run.get("acceptance_review") or {}).get("files") or {}
    red = run.get("red") or {}
    owners = requirement_owners(run)
    by_file: dict[str, list[str]] = {}
    for sc in scenarios(run):
        rid = sc["id"]
        if (red.get(rid) or {}).get("status") != "passed":
            continue
        owner = state.get_pbi(run, owners.get(rid, "")) if owners.get(rid) else None
        file = str(((owner or {}).get("acceptance_files") or {}).get(rid) or "")
        if file and (settled.get(file) or {}).get("status") not in REVIEW_FINAL:
            by_file.setdefault(file, []).append(rid)
    return [{"file": file, "requirements": reqs} for file, reqs in by_file.items()]


def review_scenarios(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The approved scenarios the planner reads the tests against: id, text, mode, Gherkin and the cases' values."""
    out: list[dict[str, Any]] = []
    for sc in scenarios(run):
        item: dict[str, Any] = {"id": sc["id"], "text": sc.get("text", ""), "mode": sc.get("mode")}
        if sc.get("gherkin"):
            item["gherkin"] = sc["gherkin"]
        item["cases"] = [
            {"id": c.get("id"), "values": c.get("values") or {}} for c in sc.get("cases") or [] if isinstance(c, dict)
        ]
        out.append(item)
    return out


def review_pending(run: dict[str, Any]) -> bool:
    """True when the Red check is done and the planner has acceptance test files left to read."""
    return bool(run.get("acceptance_first")) and not red_pending(run) and bool(review_files(run))


def stage_pending(run: dict[str, Any]) -> bool:
    """True when the run puts acceptance first and its acceptance stage, Red check or review is not done yet."""
    return red_pending(run) or review_pending(run)


def stage_buildable(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The acceptance and stub PBIs of the current wave that need building."""
    return [p for p in state.buildable_pbis(run) if p.get("kind") in STAGE_KINDS]


def _ticket_scenario(sc: dict[str, Any]) -> dict[str, Any]:
    cases = [c for c in sc.get("cases") or [] if isinstance(c, dict)]
    out: dict[str, Any] = {"id": sc["id"], "text": sc.get("text", ""), "mode": sc.get("mode")}
    if sc.get("mode") == "normal":
        out["gherkin"] = sc.get("gherkin") or ""
        out["cases"] = [{"id": c.get("id"), "values": c.get("values") or {}} for c in cases]
    else:
        out["cases"] = [
            {"id": c.get("id"), "test_name": c.get("test_name") or c.get("name") or sc.get("text", "")} for c in cases
        ]
    return out


def ticket_fields(run: dict[str, Any], pbi: dict[str, Any]) -> dict[str, Any]:
    """What the ticket of an acceptance or stub PBI carries besides an ordinary PBI's fields.

    An acceptance PBI gets the scenarios of its requirements (Gherkin and Examples for normal, case ids and test
    names for light), the public interfaces and the acceptance command; after a failed Red check, the reasons too.
    A stub PBI gets the public interfaces it places stubs of.
    """
    kind = pbi.get("kind")
    if not run.get("acceptance_first") or kind not in STAGE_KINDS:
        return {}
    interfaces = [dict(i) for i in run.get("interfaces") or [] if isinstance(i, dict)]
    if kind == "stub":
        targets = list(pbi.get("targets") or [])
        own = [i for i in interfaces if plan._module_in_paths(str(i.get("module") or ""), targets)]
        return {"kind": "stub", "interfaces": own or interfaces}
    owners = requirement_owners(run)
    fields: dict[str, Any] = {
        "kind": "acceptance",
        "scenarios": [_ticket_scenario(sc) for sc in scenarios(run) if owners.get(sc["id"]) == pbi["id"]],
        "interfaces": interfaces,
        "acceptance_command": next(iter(run.get("acceptance_commands") or []), ""),
    }
    if pbi.get("red_failure"):
        fields["red_failure"] = [dict(item) for item in pbi["red_failure"]]
    if pbi.get("revised"):
        # Built again after a plan return: the requirements whose scenarios the re-plan changed.
        fields["revised"] = list(pbi["revised"])
        # The cases of these scenarios whose PBI a worker already built: the Red check lets only these pass.
        own = {str(c["id"]) for sc in fields["scenarios"] for c in sc.get("cases") or [] if c.get("id")}
        fields["may_pass"] = sorted(own & built_cases(run))
    return fields


def keep_acceptance_files(pbi: dict[str, Any], result: dict[str, Any]) -> None:
    """Keep the file each requirement's acceptance tests are in, as an acceptance PBI's result names it."""
    files = result.get("acceptance_files")
    if not isinstance(files, dict):
        return
    kept = dict(pbi.get("acceptance_files") or {})
    for rid, path in files.items():
        if isinstance(path, str) and path.strip():
            kept[str(rid)] = path.strip().replace("\\", "/").removeprefix("./")
    pbi["acceptance_files"] = kept


# Characters a shell (POSIX sh, PowerShell or cmd) would read in a path filled into the acceptance command.
_SHELL_CHARS = re.compile(r"[\"'`$\\;&|<>(){}\[\]*?!#~%^,@\r\n\t]")


def unsafe_path(path: str) -> str:
    """The characters of a path that a shell would read rather than pass on, or "" when there are none.

    Paths are filled into the acceptance command as they are (in double quotes when they hold a space), so a path
    with any of these is not run.
    """
    return "".join(dict.fromkeys(_SHELL_CHARS.findall(path)))


def _unsafe_reason(path: str) -> str:
    chars = unsafe_path(path)
    return f"ファイル {path} の名前に、シェルが読み替える文字 {chars} がある（コマンドに入れずに止めた）" if chars else ""


def _quoted(command: str, key: str, value: str) -> str:
    if re.search(r"\s", value) and f'"{key}"' not in command and f"'{key}'" not in command:
        value = f'"{value}"'
    return command.replace(key, value)


def fill_command(command: str, file: str, junit: str) -> str:
    """The acceptance command with {file} and {junit} filled in."""
    return _quoted(_quoted(command, "{file}", file), "{junit}", junit)


def red_targets(run: dict[str, Any]) -> list[str]:
    """Requirements the next Red check runs. A light-no-red requirement is never run; it is marked skipped."""
    out: list[str] = []
    for sc in scenarios(run):
        entry = red_entry(run, sc["id"])
        if entry["status"] in RED_FINAL:
            continue
        if sc.get("mode") == NO_RED_MODE:
            entry["status"] = "skipped"
            continue
        out.append(sc["id"])
    return out


def red_check_plan(run: dict[str, Any], results_dir: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The commands of the next Red check, and the requirements it cannot run.

    results_dir is the work-root-relative folder the JUnit XML files go to. Requirements whose tests are in the
    same file run once together. Returns (groups, unrun): each group is {"file", "junit", "command",
    "requirements"}; each unrun item is a failed verdict with the reason it could not run.
    """
    command = next(iter(run.get("acceptance_commands") or []), "")
    owners = requirement_owners(run)
    groups: dict[str, dict[str, Any]] = {}
    unrun: list[dict[str, Any]] = []
    for rid in red_targets(run):
        owner = state.get_pbi(run, owners.get(rid, "")) if owners.get(rid) else None
        reason = ""
        file = ""
        if owner is None:
            reason = "この要件の受け入れテストを書く受け入れの PBI が計画に無い"
        elif not command:
            reason = "run に acceptance のコマンドが無い"
        else:
            file = str((owner.get("acceptance_files") or {}).get(rid) or "")
            if not file:
                reason = f"受け入れの PBI {owner['id']} の結果の acceptance_files に、この要件のファイルが無い"
            elif not guards.covered(file, list(owner.get("targets") or [])):
                reason = f"ファイル {file} が受け入れの PBI {owner['id']} の targets に無い"
            else:
                reason = _unsafe_reason(file)
        if reason:
            unrun.append({"requirement": rid, "passed": False, "reasons": [reason]})
            continue
        group = groups.get(file)
        if group is None:
            junit = f"{results_dir}/{len(groups) + 1}.xml"
            group = groups[file] = {"file": file, "junit": junit, "command": fill_command(command, file, junit), "requirements": []}
        group["requirements"].append(rid)
    return list(groups.values()), unrun


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def read_junit(path: Path) -> tuple[list[dict[str, str]], str | None]:
    """Each testcase of a JUnit XML file as {"name", "outcome", "type", "message"}, or why it could not be read.

    outcome is passed, failure, error or skipped.
    """
    if not path.is_file():
        return [], "JUnit XML が書き出されていない"
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return [], f"JUnit XML が読めない（{exc}）"
    cases: list[dict[str, str]] = []
    for element in root.iter():
        if _local(element.tag) != "testcase":
            continue
        outcome, etype, message = "passed", "", ""
        for child in element:
            tag = _local(child.tag)
            if tag in ("failure", "error", "skipped"):
                outcome, etype = tag, child.get("type") or ""
                message = child.get("message") or child.text or ""
                break
        cases.append({"name": element.get("name") or "", "outcome": outcome, "type": etype, "message": message.strip()})
    return cases, None


def failure_kind(case: dict[str, str]) -> str:
    """How a testcase ended: passed, error, skipped, assertion, not_implemented, or the name of another failure."""
    outcome = case.get("outcome", "")
    if outcome != "failure":
        return outcome
    message = case.get("message", "")
    name = ""
    for text in (case.get("type", ""), message):
        found = _EXCEPTION_NAME.match(text or "")
        if found:
            name = found.group(1).rsplit(".", 1)[-1]
            break
    if name in ASSERTION_TYPES:
        return "assertion"
    if name in NOT_IMPLEMENTED_TYPES:
        return "not_implemented"
    if _NOT_IMPLEMENTED_TEXT.search(message) and name in ("", "Error"):
        return "not_implemented"
    if not name and (message.startswith("assert") or _EXPECT_WORD.search(message)):
        return "assertion"
    return name or "unknown"


def match_case_id(name: str, ids: list[str]) -> str | None:
    """The case id written in a testcase name (the longest one, not inside a longer id), or None."""
    best: str | None = None
    for cid in ids:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(cid)}(?![A-Za-z0-9])", name) and (best is None or len(cid) > len(best)):
            best = cid
    return best


def _head(text: str) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= MESSAGE_LIMIT else line[:MESSAGE_LIMIT] + "…"


def _problem(cid: str, kind: str, case: dict[str, str]) -> str:
    message = _head(case.get("message", ""))
    if kind == "passed":
        return f"{cid}: 通った（落ちるはずのケース）"
    if kind == "error":
        return f"{cid}: error で落ちた（import・構文・収集などの失敗）" + (f": {message}" if message else "")
    if kind == "skipped":
        return f"{cid}: 飛ばされた" + (f": {message}" if message else "")
    return f"{cid}: 落ち方が assertion でも未実装でもない（{kind}）" + (f": {message}" if message else "")


def judge_group(
    group: dict[str, Any],
    cases_by_requirement: dict[str, list[str]],
    testcases: list[dict[str, str]],
    xml_error: str | None,
    exit_code: Any = None,
    may_pass: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The verdict of each requirement run by one command of the Red check.

    A requirement passes when its JUnit XML was read, every one of its case ids is in a testcase name, every such
    testcase failed with an assertion or a not-implemented error, and the file ran no testcase without a case id
    of the group. A case in may_pass (assigned to a PBI a worker has built: after a plan return the behaviour can be
    there already) may also pass.
    """
    reqs = list(group["requirements"])
    if xml_error:
        reason = xml_error + (f"（コマンドの終了コード {exit_code}）" if isinstance(exit_code, int) else "")
        return [{"requirement": rid, "passed": False, "reasons": [reason]} for rid in reqs]
    # Every case id of the plan counts: a file may also hold the cases of a requirement this check does not run.
    owner_of = {cid: rid for rid, ids in cases_by_requirement.items() for cid in ids}
    seen: dict[str, set[str]] = {rid: set() for rid in reqs}
    problems: dict[str, list[str]] = {rid: [] for rid in reqs}
    extra: list[str] = []
    for case in testcases:
        cid = match_case_id(case.get("name", ""), list(owner_of))
        if cid is None:
            extra.append(case.get("name") or "（名前なし）")
            continue
        rid = owner_of[cid]
        if rid not in seen:
            continue
        seen[rid].add(cid)
        kind = failure_kind(case)
        if kind not in _GOOD_FAILURES and not (kind == "passed" and cid in may_pass):
            problems[rid].append(_problem(cid, kind, case))
    verdicts: list[dict[str, Any]] = []
    for rid in reqs:
        reasons = list(problems[rid])
        missing = [cid for cid in cases_by_requirement.get(rid, []) if cid not in seen[rid]]
        if missing:
            reasons.append("足りない id: " + ", ".join(missing))
        if extra:
            reasons.append("余計な id（計画のケースに無い testcase）: " + ", ".join(extra))
        verdicts.append({"requirement": rid, "passed": not reasons, "reasons": reasons})
    return verdicts


def built_cases(run: dict[str, Any]) -> set[str]:
    """The cases assigned to a PBI that a worker has built before (it has a launch), other than an acceptance or stub
    PBI. Their behaviour may already be in the work tree, so the Red check lets them pass. That happens only when the
    acceptance stage runs again after a plan return: before the first lock no such PBI is launched."""
    launched = {pid for pid, ids in (run.get("pbi_launches") or {}).items() if ids}
    out: set[str] = set()
    for sc in scenarios(run):
        for case in sc.get("cases") or []:
            if not isinstance(case, dict) or not case.get("id") or case.get("pbi") not in launched:
                continue
            pbi = state.get_pbi(run, str(case["pbi"]))
            if pbi is not None and (pbi.get("kind") or plan.DEFAULT_KIND) not in STAGE_KINDS:
                out.add(str(case["id"]))
    return out


def judge_red(root: Path, run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every requirement's verdict from a finished Red check: the ones it could not run, then each command's."""
    codes = {
        str(c.get("command")): c.get("exit_code") for c in result.get("commands") or [] if isinstance(c, dict)
    }
    cases_by_requirement = {
        sc["id"]: [str(c.get("id")) for c in sc.get("cases") or [] if isinstance(c, dict) and c.get("id")]
        for sc in scenarios(run)
    }
    verdicts = [dict(item) for item in pending.get("red_unrun") or []]
    groups = list(pending.get("red") or [])
    built = built_cases(run)
    for group in groups:
        testcases, error = read_junit(Path(root) / group["junit"])
        verdicts += judge_group(group, cases_by_requirement, testcases, error, codes.get(group["command"]), built)
    step_reasons = run_step_checks(Path(root), run, groups)
    for verdict in verdicts:
        extra = step_reasons.get(verdict["requirement"])
        if extra:
            verdict["reasons"] = [*verdict["reasons"], *extra]
            verdict["passed"] = False
    order = [sc["id"] for sc in scenarios(run)]
    return sorted(verdicts, key=lambda v: order.index(v["requirement"]) if v["requirement"] in order else len(order))


STEP_CHECK_PREFIX = "ステップ定義の検査: "


_WALK_SKIP = frozenset({".git", "node_modules", "__pycache__"})


def _target_files(root: Path, target: str) -> list[str]:
    """The work-root-relative files a target names: itself, or the files under a directory or matched by a glob."""
    path = plan._norm_path(target).rstrip("/")
    fixed, is_glob = plan._fixed_part(path)
    if not is_glob and not (root / path).is_dir():
        return [path]
    base = fixed.rsplit("/", 1)[0] if is_glob and "/" in fixed else ("" if is_glob else path)
    out: list[str] = []
    for top, dirs, names in os.walk(root / base if base else root):
        dirs[:] = sorted(d for d in dirs if d not in _WALK_SKIP)
        for name in sorted(names):
            rel = (Path(top) / name).relative_to(root).as_posix()
            if guards.covered(rel, [path]):
                out.append(rel)
    return out


def step_check_files(root: Path, run: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, list[str]]:
    """File -> the requirements its step definition checks count for.

    Each requirement's own file counts for the requirements run from it. The other files in the targets of an
    acceptance PBI (step definitions, conftest, support code) count for every requirement of that PBI in this check;
    a directory or glob target counts with every file under it or matching it now. Files that are some
    requirement's own file are left to that requirement.
    """
    owners = requirement_owners(run)
    files: dict[str, list[str]] = {}
    for group in groups:
        files.setdefault(group["file"], []).extend(group["requirements"])
    own_files = {str(f) for p in stage_pbis(run) for f in (p.get("acceptance_files") or {}).values()}
    checked = [rid for group in groups for rid in group["requirements"]]
    for pid in dict.fromkeys(owners.get(rid) for rid in checked):
        pbi = state.get_pbi(run, pid) if pid else None
        if pbi is None:
            continue
        reqs = [rid for rid in checked if owners.get(rid) == pid]
        for target in pbi.get("targets") or []:
            for path in _target_files(Path(root), str(target)):
                if path in own_files or not stepcheck.language_of(path) or stepcheck.is_runner_config(path):
                    continue
                files.setdefault(path, [])
                files[path] += [rid for rid in reqs if rid not in files[path]]
    return files


def run_step_checks(root: Path, run: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Run the step definition checks for one Red check. Returns requirement id -> the reasons it fails them.

    Keeps run["step_checks"]: the files checked and failed this time, and every skipped language with its files.
    A file that is not there is not checked (its requirement fails on the runner's output).
    """
    interfaces = [i for i in run.get("interfaces") or [] if isinstance(i, dict)]
    kept = run.setdefault("step_checks", {})
    skipped: dict[str, list[str]] = {lang: list(paths) for lang, paths in (kept.get("skipped") or {}).items()}
    checked: list[str] = []
    failed: list[str] = []
    reasons: dict[str, list[str]] = {}
    for path, reqs in step_check_files(root, run, groups).items():
        if not (root / path).is_file():
            continue
        result = stepcheck.check_file(root, path, interfaces)
        if not result["checked"]:
            if result["language"] and path not in skipped.get(result["language"], []):
                skipped.setdefault(result["language"], []).append(path)
            continue
        checked.append(path)
        if result["problems"]:
            failed.append(path)
            for rid in reqs:
                reasons.setdefault(rid, []).extend(STEP_CHECK_PREFIX + problem for problem in result["problems"])
    kept.update(checked=checked, failed=failed, skipped=skipped)
    return reasons


def skipped_step_languages(run: dict[str, Any]) -> dict[str, list[str]]:
    """The languages the step definition checks skip: from the plan's interfaces and from the Red checks so far."""
    out = stepcheck.interface_languages([i for i in run.get("interfaces") or [] if isinstance(i, dict)])
    for lang, paths in ((run.get("step_checks") or {}).get("skipped") or {}).items():
        out.setdefault(lang, [])
        out[lang] += [p for p in paths if p not in out[lang]]
    return out


def record_events(run: dict[str, Any], verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One run-record check event per acceptance PBI judged (and one for requirements with none), with the reasons."""
    owners = requirement_owners(run)
    by_pbi: dict[str | None, list[dict[str, Any]]] = {}
    for verdict in verdicts:
        by_pbi.setdefault(owners.get(verdict["requirement"]), []).append(verdict)
    first_checked = run.setdefault("first_pass_checked_pbis", [])
    events: list[dict[str, Any]] = []
    for pid, items in by_pbi.items():
        pbi = state.get_pbi(run, pid) if pid else None
        passed = all(v["passed"] for v in items)
        is_first = pid is not None and pid not in first_checked
        if is_first:
            first_checked.append(pid)
        events.append({
            "type": "check",
            "pbi": pid,
            "rules": [] if passed else [RED_RULE],
            "stage": "red",
            "model": ((pbi or {}).get("last_step") or {}).get("model") or "",
            "level": (pbi or {}).get("level") or "unknown",
            "result": "pass" if passed else "fail",
            "read_launch_ids": list((run.get("pbi_launches") or {}).get(pid, [])) if pid else [],
            "first_pass": is_first,
            "red": [{"requirement": v["requirement"], "passed": v["passed"], "reasons": list(v["reasons"])} for v in items],
        })
    return events


def _leave_unresolved(
    run: dict[str, Any], rid: str, owner: str | None, entry: dict[str, Any], kind: str = "red_check"
) -> dict[str, Any]:
    """Put a requirement on the run's unresolved list; its cases get no lock and its PBIs go on as in light mode."""
    sc = next((s for s in scenarios(run) if s["id"] == rid), {})
    cases = [c for c in sc.get("cases") or [] if isinstance(c, dict)]
    assigned = {c.get("pbi") for c in cases}
    users = [p["id"] for p in run.get("pbis", []) if p["id"] in assigned]
    for pid in users:
        pbi = state.get_pbi(run, pid)
        if pbi is not None and rid not in (pbi.get("as_light") or []):
            pbi["as_light"] = [*(pbi.get("as_light") or []), rid]
    if sc:
        sc["no_lock"] = True
    items = run.setdefault("unresolved", [])
    item = {
        "id": f"UR-{len(items) + 1}",
        "kind": kind,
        "requirement": rid,
        "pbis": [owner] if owner else [],
        "cases": [str(c.get("id")) for c in cases],
        "as_light_pbis": users,
        "failures": entry["failures"],
        "reason": "; ".join(entry["reasons"]),
    }
    items.append(item)
    return item


def settle_red(run: dict[str, Any], verdicts: list[dict[str, Any]], check_id: str | None) -> str:
    """Apply a Red check's verdicts and name the event that follows.

    A passed requirement is done. A failed one is rebuilt by its acceptance PBI (red_rebuild); one that failed
    RED_TRIES times, or has no acceptance PBI to rebuild, is left unresolved. The stub PBIs, and the acceptance
    PBIs not rebuilt, pass. When nothing is rebuilt the acceptance stage is over (red_done).
    """
    owners = requirement_owners(run)
    rebuild: dict[str, list[dict[str, Any]]] = {}
    for verdict in verdicts:
        rid = verdict["requirement"]
        entry = red_entry(run, rid)
        entry["history"].append({"check": check_id, "passed": verdict["passed"], "reasons": list(verdict["reasons"])})
        if verdict["passed"]:
            entry["status"] = "passed"
            entry["reasons"] = []
            continue
        entry["failures"] += 1
        entry["reasons"] = list(verdict["reasons"])
        owner = owners.get(rid)
        if owner is None or state.get_pbi(run, owner) is None or entry["failures"] >= RED_TRIES:
            entry["status"] = "unresolved"
            _leave_unresolved(run, rid, owner, entry)
        else:
            entry["status"] = "failed"
            rebuild.setdefault(owner, []).append({"requirement": rid, "reasons": list(verdict["reasons"])})
    for pbi in stage_pbis(run):
        if pbi.get("status") in ("held", "sensitive"):
            continue
        if pbi["id"] in rebuild:
            pbi["status"] = "pending"
            pbi["red_failure"] = rebuild[pbi["id"]]
            # The rebuilt tests are checked again, the ones that passed included, and so are the other acceptance
            # PBIs' tests in its targets.
            _reopen_red(run, pbi, owners)
        else:
            pbi["status"] = "passed"
            pbi.pop("red_failure", None)
            pbi.pop("revised", None)
    return "red_rebuild" if rebuild else "red_done"


RED_STATUS_LABELS = {"passed": "合格", "unresolved": "未解決", "skipped": "流さない（light-no-red）"}


def report_lines(run: dict[str, Any]) -> list[str]:
    """The report's lines on the Red check: a count per result, and the step definition checks with the languages
    they skipped."""
    if not run.get("acceptance_first"):
        return []
    lines: list[str] = []
    if run.get("red"):
        counts: dict[str, int] = {}
        for entry in (run.get("red") or {}).values():
            counts[entry.get("status", "pending")] = counts.get(entry.get("status", "pending"), 0) + 1
        text = "、".join(f"{label} {counts.get(key, 0)} 件" for key, label in RED_STATUS_LABELS.items())
        lines.append(f"- Red の確認: {text}")
    kept = run.get("step_checks")
    ran = f"流した {len(kept.get('checked') or [])} ファイル（落ちた {len(kept.get('failed') or [])}）" if kept else "流していない"
    lines.append(f"- ステップ定義の機械の検査: {ran}、飛ばした言語: {stepcheck.skipped_text(skipped_step_languages(run))}")
    return lines + case_result_lines(run) + wave_case_lines(run) + [f"- 鍵を外した記録: {len(run.get('unlocks') or [])} 件"]


def case_result_lines(run: dict[str, Any]) -> list[str]:
    """The report's line on the plan's cases: how many there are and how they ended (the last wave check, then the
    checks of the unlocks after it)."""
    total = list(case_requirements(run))
    if not total:
        return []
    ends = ends_before_unlock(run)
    if ends is None:
        return [f"- 受け入れのケース: 計画の {len(total)} 件（wave の検査で流していない）"]
    ran = set(ends["ran"]) & set(total)
    passed = set(ends["passed"]) & ran
    excused = set(((run.get("wave_cases") or [{}])[-1]).get("excused") or []) & (ran - passed)
    return [
        f"- 受け入れのケース: 計画の {len(total)} 件。通った {len(passed)} 件・落ちた {len(ran - passed)} 件"
        f"（うち異議つきの赤 {len(excused)} 件）・流れていない {len(total) - len(ran)} 件"
    ]


def unlock_record_lines(run: dict[str, Any]) -> list[str]:
    """The report's section on the unlocks: each case whose lock came off, what was fixed and the lock commits."""
    records = [r for r in run.get("unlocks") or [] if isinstance(r, dict)]
    if not records:
        return []
    lines = ["", "## 鍵を外した記録", ""]
    for r in records:
        cases = "、".join(str(c) for c in r.get("cases") or []) or "（ケースなし）"
        added = f"、足した行 {'、'.join(str(c) for c in r['added'])}" if r.get("added") else ""
        before, after = str(r.get("commit_before") or "")[:7], str(r.get("commit") or "")[:7]
        lines.append(
            f"- {r.get('item') or '（件なし）'}（要件 {r.get('requirement') or '（不明）'}、{plan.UNLOCK_FIX_LABELS.get(r.get('fix'), r.get('fix') or '')}）: "
            f"ケース {cases}{added}。ファイル {r.get('file') or '（不明）'}。鍵のコミット {before or '（なし）'} → {after or '（なし）'}"
        )
    return lines


def unresolved_lines(run: dict[str, Any]) -> list[str]:
    items = run.get("unresolved") or []
    if not items:
        return []
    lines = ["", "## 未解決", ""]
    for item in items:
        if item.get("kind") == REVIEW_RULE:
            pbis = "、".join(item.get("pbis") or []) or "なし"
            light = "、".join(item.get("as_light_pbis") or []) or "なし"
            lines.append(
                f"- {item['id']}（要件 {item.get('requirement')}、受け入れの PBI {pbis}）: planner の確認で 2 回「直す」と返され、"
                f"鍵をかけずに残した。軽量と同じ扱いで進めた PBI: {light}。理由: {item.get('reason') or '（記載なし）'}"
            )
        elif item.get("kind") == "red_check":
            pbis = "、".join(item.get("pbis") or []) or "なし"
            light = "、".join(item.get("as_light_pbis") or []) or "なし"
            lines.append(
                f"- {item['id']}（要件 {item.get('requirement')}、受け入れの PBI {pbis}）: Red の確認に {item.get('failures', 0)} 回落ち、"
                f"鍵をかけずに残した。軽量と同じ扱いで進めた PBI: {light}。理由: {item.get('reason') or '（記載なし）'}"
            )
        elif item.get("kind") == WAVE_LOCK_UNRESOLVED:
            lines.append(
                f"- {item['id']}（wave の検査 {item.get('check') or ''}）: 鍵のファイル・キー・シグネチャが鍵と違ったが、"
                f"変えた PBI が分からない。鍵のコミットの中身に戻せたものは戻した。理由: {item.get('reason') or '（記載なし）'}"
            )
        elif item.get("kind") == WAVE_CASES_UNRESOLVED:
            lines.append(
                f"- {item['id']}（wave の検査 {item.get('check') or ''}）: 流れなかったケースを負わせる PBI が wave に無い。"
                f"理由: {item.get('reason') or '（記載なし）'}"
            )
        elif item.get("kind") == WAVE_CASES_RED_UNRESOLVED:
            lines.append(
                f"- {item['id']}（wave の検査 {item.get('check') or ''}）: 前の wave で通ったケースが赤になったが、"
                f"負わせる PBI が wave に無い。理由: {item.get('reason') or '（記載なし）'}"
            )
        elif item.get("kind") == CHECK_UNRESOLVED:
            last = next((ln.strip() for ln in reversed(str(item.get("output") or "").splitlines()) if ln.strip()), "")
            lines.append(
                f"- {item['id']}（wave の検査 {item.get('check') or ''}）: {item.get('command') or '（コマンド不明）'} が落ちたが、"
                f"持ち主の PBI が決まらない。理由: {item.get('reason') or '（記載なし）'}。出力の最後の行: {last or '（出力なし）'}"
            )
        elif item.get("kind") == CARRY_OVER_UNRESOLVED:
            commands = "、".join(item.get("commands") or []) or "（コマンド不明）"
            held = "、".join(item.get("held_pbis") or []) or "なし"
            last = next((ln.strip() for ln in reversed(str(item.get("output") or "").splitlines()) if ln.strip()), "")
            lines.append(
                f"- {item['id']}（持ち越し {item.get('carry_over')}、修正の PBI {item.get('fix_pbi') or 'なし'}）: "
                f"{item.get('reason') or '修正の PBI が落ち続けた'}。聞かずに保留にした PBI: {held}。落ちたコマンド: {commands}。"
                f"出力の最後の行: {last or '（出力なし）'}"
            )
        else:
            lines.append(f"- {item.get('id')}: {item.get('reason') or ''}")
    return lines


def settle_review(run: dict[str, Any], result: dict[str, Any], tmpl: dict[str, Any], review_id: str | None) -> str:
    """Apply the planner's acceptance review and name the event that follows.

    result["files"] holds {"file", "verdict", "reason"} for every file asked. A file marked ok is settled. A file
    marked fix has its acceptance PBI built again on the L5 route, and every requirement of that PBI goes back to
    the Red check, with the other acceptance PBIs' files in its targets (their approvals are dropped). A requirement
    whose file is marked fix for the second time is left unresolved, without a lock.
    """
    kept = run.setdefault("acceptance_review", {"round": 0, "files": {}, "history": []})
    kept["round"] += 1
    # How many times the planner marked each requirement's file fix: a rebuild asks again about files that were ok,
    # so the count is kept per requirement, not taken from the round.
    fixes = kept.setdefault("fixes", {})
    asked = {item["file"]: item["requirements"] for item in review_files(run)}
    owners = requirement_owners(run)
    # Matched the way validate_acceptance_review matches them.
    verdicts = {plan._norm_path(str(item["file"])): item for item in result.get("files") or []}
    rebuild: dict[str, list[dict[str, Any]]] = {}
    for file, reqs in asked.items():
        item = verdicts.get(plan._norm_path(file)) or {"verdict": "ok", "reason": ""}
        reason = str(item.get("reason") or "")
        kept["history"].append(
            {"review": review_id, "round": kept["round"], "file": file, "verdict": item["verdict"], "reason": reason}
        )
        if item["verdict"] == "ok":
            kept["files"][file] = {"status": "ok", "requirements": list(reqs), "reason": ""}
            continue
        rebuilt_here = False
        for rid in reqs:
            owner = owners.get(rid)
            fixes[rid] = int(fixes.get(rid) or 0) + 1
            if owner is None or state.get_pbi(run, owner) is None or fixes[rid] >= REVIEW_TRIES:
                entry = red_entry(run, rid)
                entry["reasons"] = [reason]
                entry["status"] = "unresolved"
                _leave_unresolved(run, rid, owner, entry, kind=REVIEW_RULE)
            else:
                rebuilt_here = True
                rebuild.setdefault(owner, []).append({"requirement": rid, "reasons": [f"planner の確認（{file}）: {reason}"]})
        if not rebuilt_here:
            # A file some of whose requirements are rebuilt is read again; its unresolved ones are no longer asked.
            kept["files"][file] = {"status": "unresolved", "requirements": list(reqs), "reason": reason}
    for pid, failures in rebuild.items():
        pbi = state.get_pbi(run, pid)
        pbi["status"] = "pending"
        pbi["red_failure"] = failures
        pbi["level"] = REVIEW_ROUTE_LEVEL
        choice = template.choose_route(tmpl, pbi)
        pbi["route"] = choice["route"]
        pbi["route_rule"] = choice["rule"]
        pbi["route_inputs"] = choice["inputs"]
        pbi["route_template"] = choice["template"]
        pbi["route_index"] = 0
        # The rebuilt tests are checked again, and read again, the files that were ok included. So are the files of
        # other acceptance PBIs' requirements that the rebuilt PBI's targets let it change.
        reopened = _reopen_red(run, pbi, owners)
        for file, info in list(kept["files"].items()):
            if info.get("status") == "ok" and (
                set(info.get("requirements") or []) & reopened or guards.covered(file, list(pbi.get("targets") or []))
            ):
                del kept["files"][file]
                for rid in info.get("requirements") or []:
                    entry = red_entry(run, rid)
                    if entry["status"] == "passed":
                        entry["status"] = "pending"
    return "acceptance_rebuild" if rebuild else "acceptance_reviewed"


def _reopen_red(run: dict[str, Any], pbi: dict[str, Any], owners: dict[str, str]) -> set[str]:
    """Send back to the Red check the passed requirements a rebuilt acceptance PBI can change: its own, and those
    whose acceptance test file is in its targets. Returns the requirements it owns or whose file it can change."""
    targets = list(pbi.get("targets") or [])
    out: set[str] = set()
    for rid, owner in owners.items():
        owner_pbi = pbi if owner == pbi["id"] else state.get_pbi(run, owner)
        file = str(((owner_pbi or {}).get("acceptance_files") or {}).get(rid) or "")
        if owner != pbi["id"] and not (file and guards.covered(file, targets)):
            continue
        out.add(rid)
        entry = red_entry(run, rid)
        if entry["status"] == "passed":
            entry["status"] = "pending"
    return out


def _scenario_body(sc: dict[str, Any]) -> dict[str, Any]:
    """What a requirement's acceptance tests are written from: its text, mode and Gherkin, and each case's id, values
    and test name. Where its cases are assigned does not change the tests (the lock list is written again anyway)."""
    return {
        "text": sc.get("text"),
        "mode": sc.get("mode"),
        "gherkin": sc.get("gherkin"),
        "cases": [
            {"id": c.get("id"), "values": c.get("values"), "test_name": c.get("test_name") or c.get("name")}
            for c in sc.get("cases") or []
            if isinstance(c, dict)
        ],
    }


def _scenarios_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {sc["id"]: sc for sc in items or [] if isinstance(sc, dict) and isinstance(sc.get("id"), str)}


def keep_unchanged_marks(run: dict[str, Any], before: list[dict[str, Any]]) -> None:
    """A re-plan writes the scenarios anew. A requirement whose scenario it left as it was stays out of the lock
    (no_lock) when it was left unresolved before."""
    old = _scenarios_by_id(before)
    for sc in scenarios(run):
        prev = old.get(sc["id"])
        if prev is not None and prev.get("no_lock") and _scenario_body(prev) == _scenario_body(sc):
            sc["no_lock"] = True


def revised_requirements(run: dict[str, Any], before: list[dict[str, Any]]) -> list[str]:
    """The requirements whose scenario a re-plan changed or added, against the scenarios before it, in plan order."""
    old = _scenarios_by_id(before)
    return [sc["id"] for sc in scenarios(run) if sc["id"] not in old or _scenario_body(sc) != _scenario_body(old[sc["id"]])]


# The run's record of the re-plans that changed a case's test: case id -> how many wave checks were kept by then.
CASE_REVISIONS = "case_revisions"


def _case_bodies(sc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each case of a scenario -> what its test is written from: the requirement's text, mode and Gherkin, and the
    case's own id, values and test name."""
    body = _scenario_body(sc)
    head = {key: body[key] for key in ("text", "mode", "gherkin")}
    return {str(c["id"]): {**head, **c} for c in body["cases"] if c.get("id")}


def revised_cases(run: dict[str, Any], before: list[dict[str, Any]]) -> list[str]:
    """The cases whose test a re-plan changed or added, against the scenarios before it, in plan order."""
    old = _scenarios_by_id(before)
    out: list[str] = []
    for sc in scenarios(run):
        was = _case_bodies(old[sc["id"]]) if sc["id"] in old else {}
        out += [cid for cid, body in _case_bodies(sc).items() if was.get(cid) != body]
    return out


def reopen_revised(run: dict[str, Any], before: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Send the requirements a re-plan in the middle of an acceptance-first run changed back to the acceptance stage.

    A requirement whose scenario the planner changed or added starts the Red check over and is no longer left out of
    the lock. Its acceptance PBI is built again, with the changed requirements in its ticket (revised). As when the
    planner's review sends a file back, every passed requirement that PBI can change goes back to the Red check, and
    the review of the files involved is dropped. Every requirement sent back gets its Red and review tries anew. The
    requirements the re-plan left as they were, and the acceptance PBIs of no changed requirement, stay as they were.
    A case whose test the re-plan changed no longer counts its passes in the wave checks before it (passed_cases).

    Returns {"revised", "reopened", "rebuilt"}: the changed requirements, every requirement sent back, and the
    acceptance PBIs built again.
    """
    revised = revised_requirements(run, before)
    checks_so_far = len(run.get("wave_cases") or [])
    revised_at = run.setdefault(CASE_REVISIONS, {})
    for cid in revised_cases(run, before):
        revised_at[cid] = checks_so_far
    owners = requirement_owners(run)
    by_id = _scenarios_by_id(scenarios(run))
    rebuilt: list[dict[str, Any]] = []
    for rid in revised:
        by_id[rid].pop("no_lock", None)
        red_entry(run, rid).update(status="pending", failures=0, reasons=[])
        owner = state.get_pbi(run, owners.get(rid, "")) if owners.get(rid) else None
        if owner is not None and owner.get("status") not in ("held", "sensitive") and owner not in rebuilt:
            rebuilt.append(owner)
    for pbi in rebuilt:
        pbi["status"] = "pending"
        pbi.pop("red_failure", None)
        pbi["revised"] = [rid for rid in revised if owners.get(rid) == pbi["id"]]
        _reopen_red(run, pbi, owners)
    red = run.setdefault("red", {})
    kept = run.setdefault("acceptance_review", {"round": 0, "files": {}, "history": []})
    files = kept.setdefault("files", {})
    targets = [str(t) for pbi in rebuilt for t in pbi.get("targets") or []]
    for file, info in list(files.items()):
        reqs = [str(r) for r in info.get("requirements") or []]
        back = any((red.get(r) or {}).get("status") == "pending" for r in reqs)
        if back or (info.get("status") == "ok" and guards.covered(file, targets)):
            del files[file]
            for rid in reqs:
                if (red.get(rid) or {}).get("status") == "passed":
                    red[rid]["status"] = "pending"
    reopened = [rid for rid in by_id if (red.get(rid) or {}).get("status") == "pending"]
    fixes = kept.setdefault("fixes", {})
    for rid in reopened:
        red[rid].update(failures=0, reasons=[])
        fixes.pop(rid, None)
    return {"revised": revised, "reopened": reopened, "rebuilt": [p["id"] for p in rebuilt]}


def requirement_changes(
    run: dict[str, Any],
    new_plan: dict[str, Any],
    owners_before: dict[str, str] | None = None,
    scenarios_before: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Japanese errors for the passed acceptance PBIs whose requirements a re-plan changes in a way it cannot build.

    A passed acceptance PBI may take a requirement whose scenario the re-plan adds or changes: it is built again for
    it when the plan is approved (reopen_revised). It may not take a requirement whose scenario stays as it was, nor
    give one up, since it is not built again for that and its tests would not follow. The requirements it writes go
    by requirement_owners; owners_before and scenarios_before are those of the run when the re-plan started (by
    default the run's own).
    """
    after = {"pbis": [p for p in new_plan.get("pbis") or [] if isinstance(p, dict)], "scenarios": new_plan.get("scenarios")}
    had_by = requirement_owners(run) if owners_before is None else owners_before
    has_by = requirement_owners(after)
    revised = set(revised_requirements(after, scenarios(run) if scenarios_before is None else scenarios_before))
    errors: list[str] = []
    for pbi in run.get("pbis", []):
        if pbi.get("kind") != "acceptance" or pbi.get("status") != "passed":
            continue
        had = [rid for rid, owner in had_by.items() if owner == pbi["id"]]
        has = [rid for rid, owner in has_by.items() if owner == pbi["id"]]
        changed = [rid for rid in had if rid not in has] + [rid for rid in has if rid not in had and rid not in revised]
        if changed:
            errors.append(
                f"合格済みの受け入れの {pbi['id']} が受け持つ要件は、このようには変えられません（{', '.join(changed)}）。"
                "受け持つ要件は外せず、足せるのはシナリオを足すか変える要件だけです。"
            )
    return errors


WAVE_CASES_RULE = "acceptance_cases"
WAVE_LOCK_UNRESOLVED = "lock_changed"
WAVE_CASES_UNRESOLVED = "cases_missing"
# A case that passed in an earlier wave check and is red now, with no PBI of the wave to lay it on.
WAVE_CASES_RED_UNRESOLVED = "cases_red_again"
# A carry-over whose fix PBI failed past its tries; its PBIs and the PBIs that depend on them are held.
CARRY_OVER_UNRESOLVED = "carry_over"
# A failed command of a wave check with no PBI of the wave to lay it on (state.failure_owners).
CHECK_UNRESOLVED = "check_failed"


def wave_case_plan(
    run: dict[str, Any], requirements: list[str], results_dir: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """The commands that run the locked cases in a wave check, and why a locked requirement cannot be run.

    Each locked requirement's acceptance test file is run with the run's acceptance command, writing JUnit XML under
    results_dir; requirements in the same file run once together. Returns (groups, unrun): each group is {"file",
    "junit", "command", "requirements"}; each unrun item is a reason. A requirement left unrun leaves its cases
    missing, which the check lays on the wave.
    """
    command = next(iter(run.get("acceptance_commands") or []), "")
    owners = requirement_owners(run)
    groups: dict[str, dict[str, Any]] = {}
    unrun: list[str] = []
    for rid in requirements:
        owner = state.get_pbi(run, owners.get(rid, "")) if owners.get(rid) else None
        file = str(((owner or {}).get("acceptance_files") or {}).get(rid) or "")
        if not command:
            unrun.append(f"要件 {rid}: run に acceptance のコマンドが無い")
            continue
        if not file:
            unrun.append(f"要件 {rid}: 受け入れテストのファイルが分からない")
            continue
        if unsafe_path(file):
            unrun.append(f"要件 {rid}: {_unsafe_reason(file)}")
            continue
        group = groups.get(file)
        if group is None:
            junit = f"{results_dir}/{len(groups) + 1}.xml"
            group = groups[file] = {"file": file, "junit": junit, "command": fill_command(command, file, junit), "requirements": []}
        group["requirements"].append(rid)
    return list(groups.values()), unrun


def case_holder(run: dict[str, Any], assigned: str | None, wave: list[str]) -> str | None:
    """The PBI of the wave a case is judged on: the PBI it is assigned to, or the fix PBI of that PBI's carry-over.

    None when neither is in the wave (the case belongs to a PBI not built yet, or one that passed before).
    """
    if not assigned:
        return None
    if assigned in wave:
        return assigned
    for co in run.get("carry_overs") or []:
        if assigned in (co.get("pbis") or []) and co.get("fix_pbi") in wave:
            return str(co["fix_pbi"])
    return None


def objection_red_cases(run: dict[str, Any], assignments: dict[str, str]) -> set[str]:
    """The cases a worker left red with an objection: its own cases (assigned to its PBI, or carried to its fix PBI)
    that its objections name with state red. The wave check counts them as run and does not send them back."""
    out: set[str] = set()
    for pbi in run.get("pbis", []):
        mine = {pbi["id"]}
        if pbi.get("kind") == "fix":
            co = next((c for c in run.get("carry_overs") or [] if c.get("id") == pbi.get("carry_over")), None)
            mine |= set((co or {}).get("pbis") or [])
        for item in pbi.get("objections") or []:
            if not isinstance(item, dict) or item.get("state") != "red":
                continue
            cid = str(item.get("case") or "").strip()
            if cid and assignments.get(cid) in mine:
                out.add(cid)
    return out


def judge_wave_cases(
    root: Path,
    run: dict[str, Any],
    groups: list[dict[str, Any]],
    cases: list[str],
    assignments: dict[str, str],
    wave: list[str],
) -> dict[str, Any]:
    """The locked cases' verdict in a wave check, from the JUnit XML its acceptance commands wrote.

    A case ran when a testcase with its id did not end skipped. A case left red with an objection counts as run and
    is not sent back. A case that did not run (skipped, left out by a tag, its file moved) is sent back: laid on the
    PBI of the wave it is assigned to. A case assigned to a PBI of the wave that ran and did not pass fails that PBI.

    A case with no PBI of the wave to hold it goes by the failure owners' steps (case_owners): when it did not run,
    and when it passed in an earlier wave check and is red now. Laid on no PBI, it is left for the caller to put on
    the unresolved list (wave_reasons for the cases that did not run, red_again for the red ones). Other cases of PBIs
    not in the wave (not built yet) are not judged on their result.

    Returns {"ran", "missing", "failed", "excused", "passed", "by_pbi", "wave_reasons", "unowned_missing",
    "red_again", "notes"}.
    """
    ids = [str(c) for c in cases]
    requirement_of = {
        str(c.get("id")): sc["id"] for sc in scenarios(run) for c in sc.get("cases") or [] if isinstance(c, dict) and c.get("id")
    }
    seen: dict[str, str] = {}
    messages: dict[str, str] = {}
    group_of: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    for group in groups:
        for rid in group.get("requirements") or []:
            for cid, req in requirement_of.items():
                if req == rid:
                    group_of.setdefault(cid, group)
        testcases, error = read_junit(Path(root) / group["junit"])
        if error:
            notes.append(f"{group['file']}: {error}")
            continue
        group_reqs = set(group.get("requirements") or [])
        for case in testcases:
            cid = match_case_id(case.get("name", ""), ids)
            if cid is None:
                continue
            # A case counts only from the file of its own requirement.
            rid = requirement_of.get(cid)
            if rid is not None and rid not in group_reqs:
                continue
            group_of.setdefault(cid, group)
            kind = failure_kind(case)
            if kind == "skipped":
                continue
            if seen.get(cid) in (None, "passed"):
                seen[cid] = kind
                messages[cid] = str(case.get("message") or "")
    # A case its worker left red with an objection and that passes now counts as passed (the objection stays on the PBI).
    excused = {cid for cid in objection_red_cases(run, assignments) if seen.get(cid) != "passed"}
    missing = [cid for cid in ids if cid not in seen and cid not in excused]
    failed = {cid: kind for cid, kind in seen.items() if kind != "passed" and cid not in excused}
    before = passed_cases(run)
    pbis = [p for p in (state.get_pbi(run, pid) for pid in wave) if p is not None]
    by_pbi: dict[str, list[str]] = {}
    wave_reasons: list[str] = []
    unowned_missing: list[str] = []
    red_again: list[str] = []
    for cid in missing:
        reason = f"{cid}: 流れなかった（skip・タグで外れた・ファイルが動いたなど）"
        holder = case_holder(run, assignments.get(cid), wave)
        # A case with no holder in the wave goes by steps (2) and (3): no command of the wave ran it on its own.
        owners = [holder] if holder else case_owners(pbis, group_of.get(cid), "", by_checks=False)
        for pid in owners:
            by_pbi.setdefault(pid, []).append(reason)
        if not owners:
            wave_reasons.append(reason)
            unowned_missing.append(cid)
    for cid in ids:
        if cid not in failed:
            continue
        holder = case_holder(run, assignments.get(cid), wave)
        if holder:
            by_pbi.setdefault(holder, []).append(f"{cid}: 割り当てたケースが落ちた（{failed[cid]}）")
        elif cid in before:
            owners = case_owners(pbis, group_of.get(cid), messages.get(cid, ""))
            for pid in owners:
                by_pbi.setdefault(pid, []).append(f"{cid}: 前の wave で通ったケースが落ちた（{failed[cid]}）")
            if not owners:
                red_again.append(cid)
    return {
        "ran": [cid for cid in ids if cid in seen or cid in excused],
        "missing": missing,
        "failed": failed,
        "excused": sorted(excused & set(ids)),
        "passed": [cid for cid in ids if seen.get(cid) == "passed" and cid not in excused],
        "by_pbi": by_pbi,
        "wave_reasons": wave_reasons,
        "unowned_missing": unowned_missing,
        "red_again": red_again,
        "notes": notes,
    }


def case_owners(
    pbis: list[dict[str, Any]], group: dict[str, Any] | None, message: str, *, by_checks: bool = True
) -> list[str]:
    """The PBIs of the wave a locked case with no holder in the wave is laid on, by the failure owners' steps.

    (1) the PBIs with the command that ran the case in their own checks (skipped with by_checks False), else (2) the
    PBIs with the case's acceptance test file in their targets, or with a test file the failure message names, else
    (3) none: the case is left unresolved.
    """
    command = str((group or {}).get("command") or "")
    file = str((group or {}).get("file") or "")
    if by_checks and command:
        own = [p["id"] for p in pbis if command in (p.get("checks") or [])]
        if own:
            return own
    by_file = {p["id"] for p in pbis if file and guards.covered(file, [str(t) for t in p.get("targets") or []])}
    by_message = set(state.named_test_owners(message, pbis))
    return [p["id"] for p in pbis if p["id"] in by_file | by_message]


def passed_cases(run: dict[str, Any]) -> set[str]:
    """The locked cases that passed in a wave check so far. A pass from before a re-plan changed the case's test is
    left out: it was the pass of the test before the change (reopen_revised)."""
    revised_at = run.get(CASE_REVISIONS) or {}
    out: set[str] = set()
    for index, entry in enumerate(run.get("wave_cases") or []):
        if "passed" in entry:
            got = {str(c) for c in entry.get("passed") or []}
        else:
            # Kept before the passed cases were: the ones that ran, did not fail and had no objection.
            got = {str(c) for c in entry.get("ran") or []} - set(entry.get("failed") or {}) - set(entry.get("excused") or [])
        out |= {cid for cid in got if index >= int(revised_at.get(cid) or 0)}
    return out


def add_unresolved(run: dict[str, Any], kind: str, reason: str, **fields: Any) -> dict[str, Any]:
    """Put an item the wave check could not lay on a PBI on the run's unresolved list."""
    items = run.setdefault("unresolved", [])
    item = {"id": f"UR-{len(items) + 1}", "kind": kind, **fields, "reason": reason}
    items.append(item)
    return item


def wave_case_lines(run: dict[str, Any]) -> list[str]:
    """The report's line on the locked cases run by the wave checks."""
    kept = run.get("wave_cases") or []
    if not kept:
        return []
    last = kept[-1]
    sent_back = sum(1 for item in kept if item.get("sent_back"))
    return [
        f"- wave の検査の受け入れのケース: {len(kept)} 回流した。最後は {len(last.get('ran') or [])} / {last.get('total', 0)} 件が流れ、"
        f"異議つきの赤 {len(last.get('excused') or [])} 件。差し戻した回 {sent_back} 回"
    ]


UNLOCK_RULE = "unlock_check"
# Where an unlock's lines run: at the lock commit (in a temporary worktree) and in the work tree (the implementation now).
UNLOCK_PLACES = ("lock_commit", "work_tree")
UNLOCK_PLACE_LABELS = {"lock_commit": "鍵のコミットの状態", "work_tree": "今の実装"}


def case_requirements(run: dict[str, Any]) -> dict[str, str]:
    """Each case of the plan's scenarios -> its requirement, in plan order."""
    return {
        str(case["id"]): sc["id"]
        for sc in scenarios(run)
        for case in sc.get("cases") or []
        if isinstance(case, dict) and case.get("id")
    }


def requirement_file(run: dict[str, Any], rid: str) -> str:
    """A requirement's acceptance test file, as its acceptance PBI's result named it ("" when there is none)."""
    owner = requirement_owners(run).get(rid)
    pbi = state.get_pbi(run, owner) if owner else None
    return str(((pbi or {}).get("acceptance_files") or {}).get(rid) or "")


def resolve_unlock(run: dict[str, Any], unlock: dict[str, Any], locked: dict[str, str]) -> dict[str, Any]:
    """A triage decision's unlock, against the lock (locked: each locked case -> its requirement).

    The named cases that are locked are the fixed lines; the others are added lines of the same requirement, assigned
    to the PBI the first fixed case is assigned to (else the requirement's first case's). The file is the
    requirement's acceptance test file: the only file the unlock opens.
    """
    cases = [str(c) for c in unlock.get("cases") or []]
    fixed = [c for c in cases if c in locked]
    rid = str(unlock.get("requirement") or next((locked[c] for c in fixed), ""))
    sc = next((s for s in scenarios(run) if s["id"] == rid), {})
    assigned = {str(c.get("id")): c.get("pbi") for c in sc.get("cases") or [] if isinstance(c, dict) and c.get("id")}
    pbi = next((assigned[c] for c in fixed if assigned.get(c)), None) or next((p for p in assigned.values() if p), None)
    added = [c for c in cases if c not in locked]
    return {
        "cases": cases,
        "fixed": fixed,
        "added": added,
        "fix": unlock.get("fix"),
        "requirement": rid,
        "file": requirement_file(run, rid),
        "mode": sc.get("mode"),
        "assign": {c: str(pbi) for c in added} if pbi else {},
    }


def add_unlocked_cases(run: dict[str, Any], unlock: dict[str, Any], item_id: str = "") -> list[str]:
    """Put an unlock's added cases into its requirement's scenario once the tests are locked again with them, so that
    the scenarios and the lock list name the same cases (their values are in the test lines only). Returns them."""
    sc = next((s for s in scenarios(run) if s["id"] == unlock.get("requirement")), None)
    if sc is None:
        return []
    have = {str(c.get("id")) for c in sc.get("cases") or [] if isinstance(c, dict)}
    added: list[str] = []
    for cid in unlock.get("added") or []:
        if str(cid) in have:
            continue
        case: dict[str, Any] = {"id": str(cid), "values": {}, "needs": [], "added_by_unlock": item_id}
        pbi = (unlock.get("assign") or {}).get(cid)
        if pbi:
            case["pbi"] = pbi
        sc.setdefault("cases", []).append(case)
        added.append(str(cid))
    return added


def unlock_check_plan(run: dict[str, Any], unlock: dict[str, Any], results_dir: str) -> tuple[list[dict[str, str]], str]:
    """The commands that check an unlock's lines: the acceptance command on its file, once at the lock commit (run in
    the temporary worktree) and once in the work tree, each writing JUnit XML under results_dir (relative to where it
    runs). Returns ([{"where", "command", "junit"}], "") or ([], why they cannot run)."""
    command = next(iter(run.get("acceptance_commands") or []), "")
    file = str(unlock.get("file") or "")
    if not command:
        return [], "run に acceptance のコマンドが無い"
    if not file:
        return [], f"要件 {unlock.get('requirement') or '（不明）'} の受け入れテストのファイルが分からない"
    if unsafe_path(file):
        return [], _unsafe_reason(file)
    out = []
    for where in UNLOCK_PLACES:
        junit = f"{results_dir}/{where}.xml"
        out.append({"where": where, "command": fill_command(command, file, junit), "junit": junit})
    return out, ""


def ends_before_unlock(run: dict[str, Any]) -> dict[str, list[str]] | None:
    """How the locked cases ended last, for judge_unlock: {"passed", "ran"} as the last wave check left them, then as
    each earlier unlock's run in the work tree left its own cases. None when no wave check ran the locked cases."""
    kept = run.get("wave_cases") or []
    if not kept:
        return None
    last = kept[-1]
    ran = {str(c) for c in last.get("ran") or []}
    if "passed" in last:
        passed = {str(c) for c in last.get("passed") or []}
    else:
        passed = ran - set(last.get("failed") or {}) - set(last.get("excused") or [])
    for record in run.get("unlocks") or []:
        for cid, kind in ((record.get("verified") or {}).get("work_tree") or {}).items():
            ran.add(str(cid))
            if kind == "passed":
                passed.add(str(cid))
            else:
                passed.discard(str(cid))
    return {"passed": sorted(passed), "ran": sorted(ran)}


def _rank(kind: str) -> int:
    # A testcase run twice ends as its worst run: skipped < passed < an assertion or not-implemented failure < any
    # other failure (for example an error), so that the order of the testcases cannot hide an error.
    return 0 if kind == "skipped" else 1 if kind == "passed" else 2 if kind in _GOOD_FAILURES else 3


def _case_ends(testcases: list[dict[str, str]], ids: list[str]) -> tuple[dict[str, str], list[str]]:
    """How each case id ended, and the names of the testcases with no id of ids."""
    ends: dict[str, str] = {}
    extra: list[str] = []
    for case in testcases:
        cid = match_case_id(case.get("name", ""), ids)
        if cid is None:
            extra.append(case.get("name") or "（名前なし）")
            continue
        kind = failure_kind(case)
        if cid not in ends or _rank(kind) > _rank(ends[cid]):
            ends[cid] = kind
    return ends, extra


def _named_ids(line: str, ids: list[str]) -> set[str]:
    """The case ids a line names. As in match_case_id, an id inside a longer id named at the same place does not count."""
    spans = [
        (m.start(), m.end(), cid)
        for cid in ids
        for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(cid)}(?![A-Za-z0-9])", line)
    ]
    return {
        cid
        for start, end, cid in spans
        if not any(s <= start and end <= e and e - s > end - start for s, e, _ in spans)
    }


def case_lines(old: str, new: str, targets: list[str], others: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """The lines an unlock may not change, from a line diff of the file at the lock commit (old) and now (new).

    An unlock may change, remove or add only lines that name one of targets (its fixed and added cases) and none of
    others (the file's other locked cases). Blank lines, trailing spaces and line endings aside, every line the diff
    removes or adds is checked. Returns (each case of others -> the changed lines that name it, also when they name
    one of targets too, for example a line that holds two cases; the changed lines that name no case, for example a
    shared fixture's line, which may hold the other cases' content).
    """
    ids = [*targets, *others]
    by_case: dict[str, list[str]] = {cid: [] for cid in others}
    no_case: list[str] = []
    was = [line.rstrip() for line in old.splitlines() if line.strip()]
    now = [line.rstrip() for line in new.splitlines() if line.strip()]
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, was, now, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        for line in [*was[i1:i2], *now[j1:j2]]:
            named = _named_ids(line, ids)
            if not named:
                no_case.append(line)
            for cid in others:
                if cid in named and line not in by_case[cid]:
                    by_case[cid].append(line)
    return by_case, no_case


def unlock_texts(root: Path, run: dict[str, Any], unlock: dict[str, Any]) -> dict[str, str | None]:
    """An unlock's file at the lock commit and in the work tree, as text for judge_unlock (None where it cannot be
    read)."""
    file = str(unlock.get("file") or "")
    commit = str((run.get("lock") or {}).get("commit") or "")
    out: dict[str, str | None] = {where: None for where in UNLOCK_PLACES}
    if not file:
        return out
    if commit:
        proc = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:{file}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if proc.returncode == 0:
            out["lock_commit"] = proc.stdout.decode("utf-8", "replace")
    try:
        out["work_tree"] = (Path(root) / file).read_bytes().decode("utf-8", "replace")
    except OSError:
        pass
    return out


def judge_unlock(
    run: dict[str, Any],
    unlock: dict[str, Any],
    runs: dict[str, tuple[list[dict[str, str]], str | None]],
    locked: dict[str, str],
    before: dict[str, Any] | None = None,
    texts: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Whether an unlock's fixed and added lines hold, from the JUnit XML of its two runs.

    runs maps lock_commit and work_tree to (the testcases, why the XML could not be read). locked maps each locked
    case to its requirement; before is how the locked cases ended last (ends_before_unlock); texts maps lock_commit
    and work_tree to the file's text there (unlock_texts; None where it could not be read).
    - The unlock's cases: at the lock commit each ran and did not pass (an error counts as failing there, since the
      implementation is not in that commit; a light-no-red requirement cannot be made Red, so it only has to run). In
      the work tree each passed when the decision fixes the scenario, and failed with an assertion or a
      not-implemented error when it fixes the implementation.
    - The file's other locked cases, whose lines the fix leaves as they were: at the lock commit each ran and did not
      pass (light-no-red: ran); in the work tree each ran, and ended as in the last wave check (passed stays passed,
      red stays red). With texts, a line diff against the lock commit (case_lines) also fails the check when the fix
      changed, removed or added a line that names one of them (also when it names one of the unlock's cases too) or
      that names no case at all, so that a change that keeps such a case green fails too.
    - A testcase with no id of the unlock's cases or of the file's locked cases fails the check too.
    Returns {"passed", "reasons", "lock_commit", "work_tree"}: how each of the unlock's cases ended at each place.
    """
    targets = [str(c) for c in unlock.get("cases") or []]
    file = str(unlock.get("file") or "")
    modes = {sc["id"]: sc.get("mode") for sc in scenarios(run)}
    others = [cid for cid, rid in locked.items() if cid not in targets and file and requirement_file(run, rid) == file]
    no_red = {cid for cid in [*targets, *others] if modes.get(locked.get(cid) or unlock.get("requirement")) == NO_RED_MODE}
    ids = [*targets, *others]
    reasons: list[str] = []
    ends: dict[str, dict[str, str]] = {}
    extra: list[str] = []
    for where in UNLOCK_PLACES:
        testcases, error = runs.get(where) or ([], "流していない")
        label = UNLOCK_PLACE_LABELS[where]
        if error:
            reasons.append(f"{label}で流せなかった: {error}")
            continue
        ends[where], names = _case_ends(testcases, ids)
        extra += [n for n in names if n not in extra]
    if extra:
        reasons.append("鍵を外すケースにもファイルの鍵のケースにも無い testcase がある: " + ", ".join(extra))
    if texts is not None and others:
        old, new = texts.get("lock_commit"), texts.get("work_tree")
        if old is None or new is None:
            reasons.append(f"{file} の行を{UNLOCK_PLACE_LABELS['lock_commit']}と比べられなかった")
        else:
            by_case, no_case = case_lines(old, new, targets, others)
            since = UNLOCK_PLACE_LABELS["lock_commit"]
            reasons += [
                f"{cid}: 対象の外のケースの行が{since}から変わった（鍵を外していない行。ほかのケースと同じ行も変えられない）: {lines[0]}"
                for cid, lines in by_case.items()
                if lines
            ]
            if no_case:
                reasons.append(
                    f"{file}: どのケースの id も無い行が{since}から変わった（ほかのケースの中身かもしれないので変えられない、"
                    f"{len(no_case)} 行）: {no_case[0]}"
                )
    lock_ends = ends.get("lock_commit")
    now_ends = ends.get("work_tree")
    lock_label = UNLOCK_PLACE_LABELS["lock_commit"]
    fix = unlock.get("fix")
    passed_before = set((before or {}).get("passed") or [])
    ran_before = set((before or {}).get("ran") or [])
    for cid in targets:
        if lock_ends is not None:
            kind = lock_ends.get(cid)
            if kind in (None, "skipped"):
                reasons.append(f"{cid}: {lock_label}で{'飛ばされた' if kind else '流れなかった'}")
            elif kind == "passed" and cid not in no_red:
                reasons.append(f"{cid}: {lock_label}で通った（直した行・足した行は、鍵のコミットの状態では落ちるはず）")
        if now_ends is not None:
            kind = now_ends.get(cid)
            if kind in (None, "skipped"):
                reasons.append(f"{cid}: 今の実装で{'飛ばされた' if kind else '流れなかった'}")
            elif fix == "scenario" and kind != "passed":
                reasons.append(f"{cid}: シナリオを直すと決めたが、今の実装で通らない（{kind}）")
            elif fix == "implementation" and kind == "passed":
                reasons.append(f"{cid}: 実装を直すと決めたが、今の実装で通った（直した行が今の実装との違いを捉えていない）")
            elif fix == "implementation" and kind not in _GOOD_FAILURES:
                reasons.append(f"{cid}: 実装を直すと決めたが、今の実装での落ち方が assertion でも未実装でもない（{kind}）")
    for cid in others:
        if lock_ends is not None:
            kind = lock_ends.get(cid)
            if kind in (None, "skipped"):
                reasons.append(f"{cid}: 対象の外のケースが{lock_label}で流れなかった")
            elif kind == "passed" and cid not in no_red:
                reasons.append(f"{cid}: 対象の外のケースが{lock_label}で通った（その行が変わった）")
        if now_ends is not None:
            kind = now_ends.get(cid)
            if kind in (None, "skipped"):
                reasons.append(f"{cid}: 対象の外のケースが今の実装で流れなかった")
            elif cid in passed_before and kind != "passed":
                reasons.append(f"{cid}: 対象の外のケースが、前の wave の検査では通ったのに今の実装で通らない（{kind}）")
            elif cid in ran_before and cid not in passed_before and kind == "passed":
                reasons.append(f"{cid}: 対象の外のケースが、前の wave の検査では通らなかったのに今の実装で通る（その行が変わった）")
    return {
        "passed": not reasons,
        "reasons": reasons,
        **{where: {cid: (ends.get(where) or {}).get(cid, "missing") for cid in targets} for where in UNLOCK_PLACES},
    }


def _norm(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./")
