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
"""

from __future__ import annotations

import re
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
    if not name and (message.startswith("assert") or "expect" in message.lower()):
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
) -> list[dict[str, Any]]:
    """The verdict of each requirement run by one command of the Red check.

    A requirement passes when its JUnit XML was read, every one of its case ids is in a testcase name, every such
    testcase failed with an assertion or a not-implemented error, and the file ran no testcase without a case id
    of the group.
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
        if kind not in _GOOD_FAILURES:
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
    for group in groups:
        testcases, error = read_junit(Path(root) / group["junit"])
        verdicts += judge_group(group, cases_by_requirement, testcases, error, codes.get(group["command"]))
    step_reasons = run_step_checks(Path(root), run, groups)
    for verdict in verdicts:
        extra = step_reasons.get(verdict["requirement"])
        if extra:
            verdict["reasons"] = [*verdict["reasons"], *extra]
            verdict["passed"] = False
    order = [sc["id"] for sc in scenarios(run)]
    return sorted(verdicts, key=lambda v: order.index(v["requirement"]) if v["requirement"] in order else len(order))


STEP_CHECK_PREFIX = "ステップ定義の検査: "


def step_check_files(run: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, list[str]]:
    """File -> the requirements its step definition checks count for.

    Each requirement's own file counts for the requirements run from it. The other files in the targets of an
    acceptance PBI (step definitions, conftest, support code) count for every requirement of that PBI in this check.
    Glob targets and files that are some requirement's own file are left to that requirement.
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
            path = str(target).replace("\\", "/").removeprefix("./")
            if path in own_files or any(ch in path for ch in "*?[") or not stepcheck.language_of(path) or stepcheck.is_runner_config(path):
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
    for path, reqs in step_check_files(run, groups).items():
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
            # The rebuilt tests are checked again, the ones that passed included.
            for rid, owner in owners.items():
                entry = red_entry(run, rid)
                if owner == pbi["id"] and entry["status"] == "passed":
                    entry["status"] = "pending"
        else:
            pbi["status"] = "passed"
            pbi.pop("red_failure", None)
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
    return lines + wave_case_lines(run)


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
        else:
            lines.append(f"- {item.get('id')}: {item.get('reason') or ''}")
    return lines


def settle_review(run: dict[str, Any], result: dict[str, Any], tmpl: dict[str, Any], review_id: str | None) -> str:
    """Apply the planner's acceptance review and name the event that follows.

    result["files"] holds {"file", "verdict", "reason"} for every file asked. A file marked ok is settled. A file
    marked fix has its acceptance PBI built again on the L5 route, and every requirement of that PBI goes back to
    the Red check; in the second review a file marked fix leaves its requirements unresolved, without a lock.
    """
    kept = run.setdefault("acceptance_review", {"round": 0, "files": {}, "history": []})
    kept["round"] += 1
    asked = {item["file"]: item["requirements"] for item in review_files(run)}
    owners = requirement_owners(run)
    verdicts = {_norm(str(item["file"])): item for item in result.get("files") or []}
    rebuild: dict[str, list[dict[str, Any]]] = {}
    for file, reqs in asked.items():
        item = verdicts.get(file) or {"verdict": "ok", "reason": ""}
        reason = str(item.get("reason") or "")
        kept["history"].append(
            {"review": review_id, "round": kept["round"], "file": file, "verdict": item["verdict"], "reason": reason}
        )
        if item["verdict"] == "ok":
            kept["files"][file] = {"status": "ok", "requirements": list(reqs), "reason": ""}
            continue
        for rid in reqs:
            owner = owners.get(rid)
            if owner is None or state.get_pbi(run, owner) is None or kept["round"] >= REVIEW_TRIES:
                entry = red_entry(run, rid)
                entry["reasons"] = [reason]
                entry["status"] = "unresolved"
                _leave_unresolved(run, rid, owner, entry, kind=REVIEW_RULE)
                kept["files"][file] = {"status": "unresolved", "requirements": list(reqs), "reason": reason}
            else:
                rebuild.setdefault(owner, []).append({"requirement": rid, "reasons": [f"planner の確認（{file}）: {reason}"]})
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
        # The rebuilt tests are checked again, and read again, the files that were ok included.
        for rid, owner in owners.items():
            if owner != pid:
                continue
            entry = red_entry(run, rid)
            if entry["status"] == "passed":
                entry["status"] = "pending"
            for file, info in list(kept["files"].items()):
                if rid in info.get("requirements", []) and info.get("status") == "ok":
                    del kept["files"][file]
    return "acceptance_rebuild" if rebuild else "acceptance_reviewed"


WAVE_CASES_RULE = "acceptance_cases"
WAVE_LOCK_UNRESOLVED = "lock_changed"
WAVE_CASES_UNRESOLVED = "cases_missing"


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
    PBI of the wave it is assigned to, or on the wave when none is (wave_reasons). A case assigned to a PBI of the
    wave that ran and did not pass fails that PBI. Cases of PBIs not in the wave are not judged on their result.

    Returns {"ran", "missing", "failed", "excused", "by_pbi", "wave_reasons", "notes"}.
    """
    ids = [str(c) for c in cases]
    seen: dict[str, str] = {}
    notes: list[str] = []
    for group in groups:
        testcases, error = read_junit(Path(root) / group["junit"])
        if error:
            notes.append(f"{group['file']}: {error}")
            continue
        for case in testcases:
            cid = match_case_id(case.get("name", ""), ids)
            if cid is None:
                continue
            kind = failure_kind(case)
            if kind == "skipped":
                continue
            if seen.get(cid) in (None, "passed"):
                seen[cid] = kind
    excused = objection_red_cases(run, assignments)
    missing = [cid for cid in ids if cid not in seen and cid not in excused]
    failed = {cid: kind for cid, kind in seen.items() if kind != "passed" and cid not in excused}
    by_pbi: dict[str, list[str]] = {}
    wave_reasons: list[str] = []
    for cid in missing:
        reason = f"{cid}: 流れなかった（skip・タグで外れた・ファイルが動いたなど）"
        holder = case_holder(run, assignments.get(cid), wave)
        if holder:
            by_pbi.setdefault(holder, []).append(reason)
        else:
            wave_reasons.append(reason)
    for cid in ids:
        holder = case_holder(run, assignments.get(cid), wave) if cid in failed else None
        if holder:
            by_pbi.setdefault(holder, []).append(f"{cid}: 割り当てたケースが落ちた（{failed[cid]}）")
    return {
        "ran": [cid for cid in ids if cid in seen or cid in excused],
        "missing": missing,
        "failed": failed,
        "excused": sorted(excused & set(ids)),
        "by_pbi": by_pbi,
        "wave_reasons": wave_reasons,
        "notes": notes,
    }


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


def _norm(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./")
