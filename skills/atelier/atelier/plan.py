"""PBI plan, wave ordering, finding validation/filtering, triage, fix grouping, and role prompts."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .survey import inside_repo

SCHEMA_PLAN = "atelier.plan/1"
SCHEMA_FINDINGS = "atelier.findings/1"
SCHEMA_TRIAGE = "atelier.triage/1"
SCHEMA_ACCEPTANCE_REVIEW = "atelier.acceptance_review/1"
REVIEW_VERDICTS = ("ok", "fix")

LEVELS = ("L1", "L2", "L3", "L4", "L5")
SEVERITIES = ("critical", "major", "minor")
FINDING_KINDS = ("correctness", "spec", "rule", "style")
EVIDENCE_PATTERN = re.compile(r".+:[1-9][0-9]*")
# Fields a re-planned PBI that already passed must keep unchanged ("targets" is compared as paths plus the unit-test marks).
FROZEN_FIELDS = ("id", "goal", "targets", "checks", "kind")
PBI_KINDS = ("build", "acceptance", "e2e", "stub")
DEFAULT_KIND = "build"
SCENARIO_MODES = ("normal", "light", "light-no-red")
UNIT_MARK = "unit"
TRIAGE_DECISIONS = ("fix", "skip", "ask")
# A triage item's unlock: the locked cases whose lock comes off, and what was decided for them. scenario: the test
# lines change to what the implementation does; implementation: the test lines change and the implementation follows.
UNLOCK_FIELDS = ("cases", "fix", "requirement")
UNLOCK_FIXES = ("scenario", "implementation")
UNLOCK_FIX_LABELS = {"scenario": "シナリオを直す", "implementation": "実装を直す"}
UNLOCK_DECISIONS = ("fix", "ask")
MIN_CONFIDENCE = 80

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_LEVELS_PATH = Path(__file__).with_name("levels.json")


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _norm_path(path: str) -> str:
    return PurePosixPath(path.strip().replace("\\", "/")).as_posix().removeprefix("./")


_GLOB_CHARS = "*?["


def _fixed_part(path: str) -> tuple[str, bool]:
    """The path before its first glob character, and whether it had one."""
    norm = _norm_path(path).rstrip("/")
    norm = "" if norm == "." else norm
    cuts = [norm.index(c) for c in _GLOB_CHARS if c in norm]
    return (norm[: min(cuts)], True) if cuts else (norm, False)


def paths_overlap(a: str, b: str) -> bool:
    """True when two target paths may name the same file.

    The same path, or a directory and a path inside it, overlap. A glob is compared by its part before the
    first glob character, as a plain prefix either way, so an unsure case counts as overlapping.
    """
    fa, glob_a = _fixed_part(a)
    fb, glob_b = _fixed_part(b)
    if glob_a or glob_b:
        return fa.startswith(fb) or fb.startswith(fa)
    if not fa or not fb:
        return True
    return fa == fb or fa.startswith(fb + "/") or fb.startswith(fa + "/")


def targets_overlap(a: Iterable[str], b: Iterable[str]) -> bool:
    """True when any path of one list may name a file that a path of the other names."""
    left = [p for p in a if _is_nonempty_str(p)]
    return any(paths_overlap(x, y) for x in left for y in b if _is_nonempty_str(y))


def _validate_str_list(
    value: Any,
    label: str,
    errors: list[str],
    *,
    require_nonempty_list: bool = False,
) -> list[str] | None:
    if not isinstance(value, list):
        errors.append(f"{label} は配列にしてください。")
        return None
    if require_nonempty_list and not value:
        errors.append(f"{label} は 1 件以上指定してください。")
        return None
    out: list[str] = []
    for idx, elem in enumerate(value):
        if not _is_nonempty_str(elem):
            errors.append(f"{label}[{idx}] は空でない文字列にしてください。")
        else:
            out.append(elem)
    return out


def split_targets(targets: Any) -> tuple[list[str], list[str]]:
    """A PBI's target entries as (every path, the paths marked as unit tests).

    An entry is a path string or {"path": ..., "kind": "unit"}. Entries of any other shape are skipped here;
    validate_plan reports them. Both lists keep their order and name each path once.
    """
    paths: list[str] = []
    units: list[str] = []
    for entry in targets if isinstance(targets, list) else []:
        marked = isinstance(entry, dict) and entry.get("kind") == UNIT_MARK
        path = entry.get("path") if isinstance(entry, dict) else entry
        if not _is_nonempty_str(path):
            continue
        if path not in paths:
            paths.append(path)
        if marked and path not in units:
            units.append(path)
    return paths, units


def _validate_targets(value: Any, label: str, errors: list[str]) -> list[str] | None:
    """Validate targets whose entries are path strings or {"path", "kind": "unit"}; return the paths."""
    if not isinstance(value, list):
        errors.append(f"{label} は配列にしてください。")
        return None
    if not value:
        errors.append(f"{label} は 1 件以上指定してください。")
        return None
    out: list[str] = []
    for idx, entry in enumerate(value):
        item = f"{label}[{idx}]"
        if isinstance(entry, dict):
            if not _is_nonempty_str(entry.get("path")):
                errors.append(f"{item} の path は空でない文字列にしてください。")
                continue
            if entry.get("kind") != UNIT_MARK:
                errors.append(f"{item} の kind は {UNIT_MARK!r} にしてください（ユニットテストの印）。")
            extra = sorted(set(entry) - {"path", "kind"})
            if extra:
                errors.append(f"{item} に使えない項目があります: {', '.join(extra)}")
            out.append(entry["path"])
        elif _is_nonempty_str(entry):
            out.append(entry)
        else:
            errors.append(f"{item} は空でない文字列か、path と kind（unit）を持つオブジェクトにしてください。")
    return out


def _validate_interfaces(value: Any, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append("interfaces は配列にしてください。")
        return
    for idx, entry in enumerate(value):
        item = f"interfaces[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{item} はオブジェクトにしてください。")
            continue
        for key in ("module", "name"):
            if not _is_nonempty_str(entry.get(key)):
                errors.append(f"{item} の {key} は空でない文字列にしてください。")


def _validate_scenarios(value: Any, pbi_ids: set[str], errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append("scenarios は配列にしてください。")
        return
    seen_scenarios: set[str] = set()
    seen_cases: set[str] = set()
    for idx, scenario in enumerate(value):
        label = f"scenarios[{idx}]"
        if not isinstance(scenario, dict):
            errors.append(f"{label} はオブジェクトにしてください。")
            continue
        sid = scenario.get("id")
        if not _is_nonempty_str(sid):
            errors.append(f"{label} の id は空でない文字列にしてください。")
        elif sid in seen_scenarios:
            errors.append(f"シナリオ id {sid!r} が重複しています。")
        else:
            seen_scenarios.add(sid)
            label = sid
        if not _is_nonempty_str(scenario.get("text")):
            errors.append(f"{label} の text（要件の本文）は空でない文字列にしてください。")
        mode = scenario.get("mode")
        if mode not in SCENARIO_MODES:
            errors.append(f"{label} の mode は {', '.join(SCENARIO_MODES)} のいずれかにしてください。")
        gherkin = scenario.get("gherkin")
        if mode == "normal" and not _is_nonempty_str(gherkin):
            errors.append(f"{label} は mode が normal なので、gherkin（Gherkin の本文）を書いてください。")
        elif gherkin is not None and not isinstance(gherkin, str):
            errors.append(f"{label} の gherkin は文字列にしてください。")
        spare = scenario.get("spare_count")
        if not _is_int(spare) or spare < 0:
            errors.append(f"{label} の spare_count（控えの件数）は 0 以上の整数にしてください。")
        cases = scenario.get("cases")
        if not isinstance(cases, list) or not cases:
            errors.append(f"{label} の cases は 1 件以上の配列にしてください。")
            continue
        for cidx, case in enumerate(cases):
            clabel = f"{label}.cases[{cidx}]"
            if not isinstance(case, dict):
                errors.append(f"{clabel} はオブジェクトにしてください。")
                continue
            cid = case.get("id")
            if not _is_nonempty_str(cid):
                errors.append(f"{clabel} の id は空でない文字列にしてください。")
            elif cid in seen_cases:
                errors.append(f"ケース id {cid!r} が重複しています。")
            else:
                seen_cases.add(cid)
                clabel = f"{label}.{cid}"
            if not isinstance(case.get("values"), dict):
                errors.append(f"{clabel} の values は列名と値のオブジェクトにしてください。")
            owner = case.get("pbi")
            if not _is_nonempty_str(owner):
                errors.append(f"{clabel} の pbi（割り当て先の PBI）は空でない文字列にしてください。")
            elif owner not in pbi_ids:
                errors.append(f"{clabel} の pbi {owner!r} は計画にありません。")
            needs = case.get("needs", [])
            if not isinstance(needs, list) or not all(_is_nonempty_str(n) for n in needs):
                errors.append(f"{clabel} の needs（通るのに要る PBI）は PBI id の配列にしてください（無ければ []）。")
            else:
                for need in needs:
                    if need not in pbi_ids:
                        errors.append(f"{clabel} の needs の {need!r} は計画にありません。")


def _validate_requirements(pbis: list[Any], scenarios: Any, errors: list[str]) -> None:
    """An acceptance PBI may name the requirements (scenario ids) whose acceptance tests it writes.

    Only an acceptance PBI names them, each requirement is in the scenarios, and no two PBIs name the same one.
    """
    known = (
        {sc["id"] for sc in scenarios if isinstance(sc, dict) and _is_nonempty_str(sc.get("id"))}
        if isinstance(scenarios, list)
        else set()
    )
    claimed: dict[str, str] = {}
    for idx, pbi in enumerate(pbis):
        if not isinstance(pbi, dict) or "requirements" not in pbi:
            continue
        label = pbi["id"] if _is_nonempty_str(pbi.get("id")) else f"pbis[{idx}]"
        reqs = pbi["requirements"]
        if pbi.get("kind", DEFAULT_KIND) != "acceptance":
            errors.append(f"{label} の requirements は kind が acceptance の PBI にだけ書けます。")
            continue
        if not isinstance(reqs, list) or not all(_is_nonempty_str(r) for r in reqs):
            errors.append(f"{label} の requirements は要件（scenarios の id）の配列にしてください。")
            continue
        for rid in reqs:
            if rid not in known:
                errors.append(f"{label} の requirements の {rid!r} は scenarios にありません。")
            elif claimed.setdefault(rid, label) != label:
                errors.append(
                    f"要件 {rid} の受け入れテストを {claimed[rid]} と {label} の両方が書くことになっています。1 つの受け入れの PBI にしてください。"
                )


def _validate_backlog(pbi: dict[str, Any], label: str, known: set[str], errors: list[str]) -> None:
    """A PBI's `backlog` (optional): the ids of backlog entries it takes, each one of the candidates."""
    if "backlog" not in pbi:
        return
    names = _validate_str_list(pbi.get("backlog"), f"{label}.backlog", errors)
    unknown = [n for n in dict.fromkeys(names or []) if n not in known]
    if unknown:
        errors.append(
            f"{label}.backlog の {', '.join(repr(n) for n in unknown)} は backlog の候補（Backlog candidates）にありません。"
        )


def _validate_data_needs(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{label} は配列にしてください（無ければ []）。")
        return
    for idx, need in enumerate(value):
        item = f"{label}[{idx}]"
        if not isinstance(need, dict):
            errors.append(f"{item} はオブジェクトにしてください。")
            continue
        for key in ("source", "dest", "why"):
            if not _is_nonempty_str(need.get(key)):
                errors.append(f"{item} の {key} は空でない文字列にしてください。")
        if _is_nonempty_str(need.get("dest")) and not inside_repo(need["dest"]):
            errors.append(f"{item} の dest はリポジトリの中の相対パスにしてください（.. と絶対パスは使えません）。")


def frozen_changes(old_pbis: list[dict[str, Any]], new_plan: dict[str, Any], passed_ids: Iterable[str]) -> list[str]:
    """Japanese errors for passed PBIs that a re-plan dropped or changed."""
    new_by_id = {p.get("id"): p for p in new_plan.get("pbis") or [] if isinstance(p, dict)}
    errors: list[str] = []
    for old in old_pbis:
        pid = old.get("id")
        if pid not in set(passed_ids):
            continue
        new = new_by_id.get(pid)
        if new is None:
            errors.append(f"合格済みの {pid} が組み直した計画にありません。")
            continue
        changed = []
        for field in FROZEN_FIELDS:
            if field == "targets":
                new_paths, new_units = split_targets(new.get("targets"))
                if new_paths != list(old.get("targets") or []) or new_units != list(old.get("unit_tests") or []):
                    changed.append(field)
            elif field == "kind":
                if (new.get("kind") or DEFAULT_KIND) != (old.get("kind") or DEFAULT_KIND):
                    changed.append(field)
            elif new.get(field) != old.get(field):
                changed.append(field)
        if changed:
            errors.append(f"合格済みの {pid} の {', '.join(changed)} が変わっています。")
    return errors


def _compute_waves(pbis: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    for idx, pbi in enumerate(pbis):
        pid = pbi.get("id")
        if not _is_nonempty_str(pid) or pid in by_id:
            raise ValueError("PBI の id が不正または重複しています。")
        by_id[pid] = pbi
        order[pid] = idx

    remaining = list(by_id.keys())
    done: set[str] = set()
    result: list[list[dict[str, Any]]] = []

    while remaining:
        wave_ids: list[str] = []
        for pid in remaining:
            deps = by_id[pid].get("depends_on") or []
            if not isinstance(deps, list):
                raise ValueError(f"{pid} の depends_on は配列にしてください。")
            for dep in deps:
                if dep not in by_id:
                    raise ValueError(f"{pid} が存在しない PBI {dep!r} に依存しています。")
                if dep == pid:
                    raise ValueError(f"{pid} が自分自身に依存しています。")
            if all(dep in done for dep in deps):
                wave_ids.append(pid)
        if not wave_ids:
            raise ValueError("PBI の依存関係に循環があります。")
        wave_ids.sort(key=lambda pid: order[pid])
        result.append([by_id[pid] for pid in wave_ids])
        done.update(wave_ids)
        remaining = [pid for pid in remaining if pid not in done]

    return result


def validate_plan(obj: Any, acceptance_first: bool = False, backlog_ids: Iterable[str] | None = None) -> list[str]:
    """Validate an atelier.plan/1 object and return Japanese error strings.

    With acceptance_first on, the plan must also carry scenarios and interfaces. With it off, a plan without
    them passes as before; any that are given are still checked for shape. The rules on e2e PBIs (level L3 or higher,
    a wave after every build PBI) hold whether acceptance_first is on or off.

    A PBI may name the backlog entries it takes in `backlog`; each must be one of backlog_ids (the candidates the plan
    launch gave the planner; none when it is None).
    """
    if not isinstance(obj, dict):
        return ["計画は JSON オブジェクトにしてください。"]

    errors: list[str] = []
    if obj.get("schema") != SCHEMA_PLAN:
        errors.append(f"schema は {SCHEMA_PLAN} にしてください。")

    _validate_str_list(obj.get("open_questions"), "open_questions", errors)
    _validate_str_list(obj.get("checks"), "checks", errors)
    _validate_str_list(obj.get("e2e"), "e2e", errors)
    if "final_review" not in obj:
        errors.append("final_review を指定してください（スキルを使わないときは null）。")
    else:
        review = obj["final_review"]
        if review is not None and (
            not isinstance(review, dict)
            or not _is_nonempty_str(review.get("skill"))
            or not isinstance(review.get("args", ""), str)
        ):
            errors.append("final_review は null か {\"skill\": 名前, \"args\": 文字列} にしてください。")

    if "interfaces" in obj:
        _validate_interfaces(obj["interfaces"], errors)
    elif acceptance_first:
        errors.append("interfaces（公開の口のモジュールと名前の配列）を指定してください。")

    pbis = obj.get("pbis")
    if not isinstance(pbis, list) or not pbis:
        errors.append("pbis は 1 件以上の配列にしてください。")
        return errors

    seen_ids: set[str] = set()
    valid_pbi_items: list[dict[str, Any]] = []

    for index, pbi in enumerate(pbis):
        label = f"pbis[{index}]"
        if not isinstance(pbi, dict):
            errors.append(f"{label} はオブジェクトにしてください。")
            continue

        pid = pbi.get("id")
        if not _is_nonempty_str(pid):
            errors.append(f"{label} の id は空でない文字列にしてください。")
        elif pid in seen_ids:
            errors.append(f"PBI id {pid!r} が重複しています。")
        else:
            seen_ids.add(pid)
            label = pid

        for field in ("title", "goal", "level_reason"):
            if not _is_nonempty_str(pbi.get(field)):
                errors.append(f"{label} の {field} は空でない文字列にしてください。")

        if pbi.get("level") not in LEVELS:
            errors.append(f"{label} の level は {', '.join(LEVELS)} のいずれかにしてください。")

        sensitive = pbi.get("sensitive_candidate")
        if type(sensitive) is not bool:
            errors.append(f"{label} の sensitive_candidate は真偽値にしてください。")
        s_reason = pbi.get("sensitive_reason")
        if not isinstance(s_reason, str):
            errors.append(f"{label} の sensitive_reason は文字列にしてください。")
        elif sensitive is True and not s_reason.strip():
            errors.append(f"{label} で sensitive_candidate が true の場合は sensitive_reason が必要です。")

        _validate_str_list(pbi.get("depends_on"), f"{label}.depends_on", errors)
        kind = pbi.get("kind", DEFAULT_KIND)
        if kind not in PBI_KINDS:
            errors.append(f"{label} の kind は {', '.join(PBI_KINDS)} のいずれかにしてください（省略時は {DEFAULT_KIND}）。")
        targets = _validate_targets(pbi.get("targets"), f"{label}.targets", errors)
        dnt = _validate_str_list(pbi.get("do_not_touch"), f"{label}.do_not_touch", errors)
        if targets is not None and dnt is not None:
            overlap = {_norm_path(p) for p in targets} & {_norm_path(p) for p in dnt}
            if overlap:
                errors.append(f"{label} の targets と do_not_touch が重なっています: {', '.join(sorted(overlap))}")

        own_checks = _validate_str_list(pbi.get("checks"), f"{label}.checks", errors)
        if own_checks == [] and not obj.get("checks"):
            errors.append(f"{label} の checks が空です。計画の checks も空のときは、PBI に関係するテストを checks に書いてください。")
        _validate_str_list(pbi.get("facts"), f"{label}.facts", errors)
        _validate_str_list(pbi.get("acceptance"), f"{label}.acceptance", errors, require_nonempty_list=True)
        evidence = _validate_str_list(pbi.get("evidence"), f"{label}.evidence", errors, require_nonempty_list=True)
        for place in evidence or []:
            if not EVIDENCE_PATTERN.fullmatch(place.strip()):
                errors.append(f"{label}.evidence の {place!r} は path:行番号 にしてください。")
        _validate_data_needs(pbi.get("data_needs"), f"{label}.data_needs", errors)
        _validate_backlog(pbi, label, set(backlog_ids or []), errors)

        if _is_nonempty_str(pid):
            valid_pbi_items.append(pbi)

    if "scenarios" in obj:
        _validate_scenarios(obj["scenarios"], seen_ids, errors)
        if acceptance_first and isinstance(obj["scenarios"], list) and not obj["scenarios"]:
            errors.append("scenarios は 1 件以上指定してください。")
    elif acceptance_first:
        errors.append("scenarios（要件ごとのシナリオの配列）を指定してください。")
    _validate_requirements(pbis, obj.get("scenarios"), errors)

    if len(valid_pbi_items) == len(pbis) and len(seen_ids) == len(pbis):
        dep_ok = True
        for pbi in pbis:
            pid = pbi["id"]
            deps = pbi.get("depends_on")
            if not isinstance(deps, list):
                dep_ok = False
                continue
            for dep in deps:
                if not _is_nonempty_str(dep):
                    dep_ok = False
                    continue
                if dep == pid:
                    errors.append(f"{pid} が自分自身に依存しています。")
                    dep_ok = False
                elif dep not in seen_ids:
                    errors.append(f"{pid} が存在しない PBI {dep!r} に依存しています。")
                    dep_ok = False
        if dep_ok:
            try:
                wave_list = _compute_waves(pbis)
            except ValueError as exc:
                errors.append(str(exc))
            else:
                wave_of = {p["id"]: n for n, wave in enumerate(wave_list) for p in wave}
                _check_e2e_rules(pbis, wave_of, errors)
                if acceptance_first:
                    _check_form_rules(obj, pbis, wave_of, errors)

    return errors


_SOURCE_EXTENSIONS = (
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs", "rb", "java", "kt", "php", "cs", "swift", "vue", "svelte",
)
_TEST_NAME = re.compile(r"(^test[_.-])|([_.-](test|tests|spec)$)", re.IGNORECASE)
LIGHT_MODES = ("light", "light-no-red")
LIGHT_MAX_FILES = 3


def _stem(path: str) -> str:
    norm = _norm_path(path)
    head, dot, ext = norm.rpartition(".")
    return head if dot and ext.lower() in _SOURCE_EXTENSIONS and "/" not in ext else norm


def is_test_file(path: str) -> bool:
    """True when the file name looks like a test file (test_x.py, x_test.go, x.test.ts, x.spec.ts and the like)."""
    name = PurePosixPath(_norm_path(path)).name
    return bool(_TEST_NAME.search(_stem(name)))


def implementation_files(pbi: dict[str, Any]) -> list[str]:
    """A PBI's targets that are not tests: neither marked as unit tests nor named like a test file."""
    paths, units = split_targets(pbi.get("targets"))
    return [p for p in paths if p not in units and not is_test_file(p)]


def _module_in_paths(module: str, paths: Iterable[str]) -> bool:
    mod = module.strip().replace("\\", "/")
    mod = _stem(mod) if "/" in mod or mod.rsplit(".", 1)[-1].lower() in _SOURCE_EXTENSIONS else mod.replace(".", "/")
    mod = mod.removeprefix("./").strip("/")
    if not mod:
        return False
    for path in paths:
        stem = _stem(path)
        if PurePosixPath(stem).name in ("__init__", "index"):
            stem = PurePosixPath(stem).parent.as_posix()
        if stem == mod or stem.endswith("/" + mod) or mod.endswith("/" + stem):
            return True
    return False


def _has_question_mark(value: Any) -> bool:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return "?" in text


def _check_e2e_rules(pbis: list[dict[str, Any]], wave_of: dict[str, int], errors: list[str]) -> None:
    """The rules on e2e PBIs, whether acceptance_first is on or off: level L3 or higher, and a wave after every build
    PBI (the E2E files they write are added to the lock once they pass, so no build PBI comes after them)."""
    build_waves = [wave_of[p["id"]] for p in pbis if p.get("kind", DEFAULT_KIND) == "build"]
    for pbi in pbis:
        if pbi.get("kind", DEFAULT_KIND) != "e2e":
            continue
        pid = pbi["id"]
        if pbi.get("level") in LEVELS[:2]:
            errors.append(f"{pid} は kind が e2e なので、level は L3 以上にしてください（今は {pbi.get('level')}）。")
        if any(wave_of[pid] <= w for w in build_waves):
            errors.append(f"{pid} は kind が e2e なので、すべての kind が build の PBI より後の wave に置いてください。")


def _check_form_rules(obj: dict[str, Any], pbis: list[dict[str, Any]], wave_of: dict[str, int], errors: list[str]) -> None:
    """The rules on the shape of an acceptance-first plan: where acceptance and stub PBIs sit and how cases are
    assigned (the e2e rules are _check_e2e_rules, for every plan)."""
    by_id = {p["id"]: p for p in pbis}

    def kind_of(pbi: dict[str, Any]) -> Any:
        return pbi.get("kind", DEFAULT_KIND)

    for pbi in pbis:
        pid, kind = pbi["id"], kind_of(pbi)
        if kind in ("acceptance", "stub") and wave_of[pid] != 0:
            errors.append(f"{pid} は kind が {kind} なので、最初の wave（depends_on なし）に置いてください。")

    scenarios = obj.get("scenarios")
    if not isinstance(scenarios, list):
        return
    # Shapes validate_plan already reported are read as empty here, so the rules add errors and do not crash.
    listed = obj.get("interfaces") if isinstance(obj.get("interfaces"), list) else []
    interfaces = [i for i in listed if isinstance(i, dict) and _is_nonempty_str(i.get("module"))]
    # Owners as acceptance.requirement_owners computes them: named by an acceptance PBI, or else the open one.
    acceptance_pbis = [p for p in pbis if kind_of(p) == "acceptance"]
    named = {
        r
        for p in acceptance_pbis
        for r in (p.get("requirements") if isinstance(p.get("requirements"), list) else [])
        if isinstance(r, str)
    }
    has_open = any(not p.get("requirements") for p in acceptance_pbis)
    unowned = [
        str(s.get("id"))
        for s in scenarios
        if isinstance(s, dict) and _is_nonempty_str(s.get("id")) and s["id"] not in named and not has_open
    ]
    if unowned:
        errors.append(
            f"要件 {', '.join(unowned)} の受け入れテストを書く kind が acceptance の PBI がありません。"
            "kind が acceptance の PBI の requirements にその要件を入れるか、requirements を持たない acceptance の PBI を置いてください。"
        )
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("cases"), list):
            continue
        sid = scenario.get("id")
        cases = [c for c in scenario["cases"] if isinstance(c, dict)]

        for case in cases:
            owner = case.get("pbi")
            needs = case.get("needs", [])
            if not isinstance(owner, str) or owner not in by_id or not isinstance(needs, list):
                continue
            needed = {owner, *(n for n in needs if isinstance(n, str) and n in by_id)}
            last = max(wave_of[n] for n in needed)
            latest = sorted(n for n in needed if wave_of[n] == last)
            if len(latest) > 1:
                errors.append(
                    f"ケース {case.get('id')!r}（要件 {sid}）は、wave が一番後の PBI が同じ wave に複数あります"
                    f"（{', '.join(latest)}）。1 つのケースを同じ wave の複数の PBI にまたがらせず、PBI を分け直してください。"
                )
            elif owner != latest[0]:
                errors.append(
                    f"ケース {case.get('id')!r}（要件 {sid}）の割り当て先は、wave の順で一番後に終わる {latest[0]} にしてください"
                    f"（今は {owner}）。"
                )

        if scenario.get("mode") in LIGHT_MODES:
            owners = sorted({c["pbi"] for c in cases if isinstance(c.get("pbi"), str) and c["pbi"] in by_id})
            broken: list[str] = []
            built = [by_id[o] for o in owners if kind_of(by_id[o]) == "build"]
            built_paths = [p for pbi in built for p in split_targets(pbi.get("targets"))[0]]
            new_ifaces = [f"{i['module']}.{i.get('name')}" for i in interfaces if _module_in_paths(i["module"], built_paths)]
            if new_ifaces:
                broken.append(f"新しい公開の口がある（{', '.join(new_ifaces)}）")
            files = sorted({f for o in owners for f in implementation_files(by_id[o])})
            if len(owners) > 1:
                broken.append(f"ケースの割り当て先が {len(owners)} つの PBI（{', '.join(owners)}）にわたる")
            if len(files) > LIGHT_MAX_FILES:
                broken.append(f"実装のファイルが {len(files)} つある（{LIGHT_MAX_FILES} つ以下にしてください）")
            marked = [str(c.get("id")) for c in cases if _has_question_mark(c.get("values"))]
            if marked:
                broken.append(f"ケースの期待値に ? がある（{', '.join(marked)}）")
            if broken:
                errors.append(
                    f"要件 {sid} は mode が {scenario.get('mode')} ですが、軽量の条件から外れています: {'; '.join(broken)}。"
                    "mode を normal にするか、計画を分け直してください。"
                )


def waves(plan: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Partition plan['pbis'] into dependency waves in topological order."""
    if not isinstance(plan, dict) or not isinstance(plan.get("pbis"), list):
        raise ValueError("計画オブジェクトに pbis の配列が必要です。")
    return _compute_waves(plan["pbis"])


def validate_findings(obj: Any) -> list[str]:
    """Validate an atelier.findings/1 object and return Japanese error strings."""
    if not isinstance(obj, dict):
        return ["見直しの指摘は JSON オブジェクトにしてください。"]

    errors: list[str] = []
    if obj.get("schema") != SCHEMA_FINDINGS:
        errors.append(f"schema は {SCHEMA_FINDINGS} にしてください。")

    findings = obj.get("findings")
    if not isinstance(findings, list):
        errors.append("findings は配列にしてください。")
        return errors

    seen_ids: set[str] = set()
    for index, item in enumerate(findings):
        label = f"findings[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} はオブジェクトにしてください。")
            continue

        fid = item.get("id")
        if not _is_nonempty_str(fid):
            errors.append(f"{label} の id は空でない文字列にしてください。")
        elif fid in seen_ids:
            errors.append(f"指摘 id {fid!r} が重複しています。")
        else:
            seen_ids.add(fid)
            label = fid

        for field in ("file", "why", "fix"):
            if not _is_nonempty_str(item.get(field)):
                errors.append(f"{label} の {field} は空でない文字列にしてください。")

        if "files" in item and item["files"] is not None:
            _validate_str_list(item["files"], f"{label}.files", errors)

        line = item.get("line")
        if not _is_int(line) or line < 1:
            errors.append(f"{label} の line は 1 以上の整数にしてください。")

        if item.get("severity") not in SEVERITIES:
            errors.append(f"{label} の severity は {', '.join(SEVERITIES)} のいずれかにしてください。")

        conf = item.get("confidence")
        if not _is_int(conf) or not (0 <= conf <= 100):
            errors.append(f"{label} の confidence は 0〜100 の整数にしてください。")

        if item.get("kind") not in FINDING_KINDS:
            errors.append(f"{label} の kind は {', '.join(FINDING_KINDS)} のいずれかにしてください。")
        elif item.get("kind") == "rule" and not _is_nonempty_str(item.get("rule")):
            errors.append(f"{label} は kind が rule なので、rule（根拠の規約の場所）を書いてください。")

    return errors


def split_findings(findings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into those triage sees and those it does not.

    Kept: confidence >= 80 and kind is not 'style'. Each dropped finding gets 'dropped_by'
    ('confidence' or 'style') so the fix approval page can still show it.
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in findings:
        conf = item.get("confidence", 0)
        if not _is_int(conf) or conf < MIN_CONFIDENCE:
            dropped.append({**item, "dropped_by": "confidence"})
        elif item.get("kind") == "style":
            dropped.append({**item, "dropped_by": "style"})
        else:
            kept.append(item)
    return kept, dropped


def filter_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop findings with confidence < 80 or kind == 'style'."""
    return split_findings(findings)[0]


def wave_commands(pbis: Iterable[dict[str, Any]], plan_checks: Iterable[str]) -> list[str]:
    """The given PBIs' checks, then the plan's checks, each command once."""
    out: list[str] = []
    for pbi in pbis:
        for cmd in pbi.get("checks") or []:
            if cmd not in out:
                out.append(cmd)
    for cmd in plan_checks:
        if cmd not in out:
            out.append(cmd)
    return out


def related_commands(pbis: list[dict[str, Any]], files: Iterable[str]) -> list[str]:
    """The checks of the PBIs that own one of the files (have it in their targets), each command once."""
    fixed = {_norm_path(f) for f in files if _is_nonempty_str(f)}
    owners = [p for p in pbis if fixed & {_norm_path(t) for t in (p.get("targets") or []) if _is_nonempty_str(t)}]
    return wave_commands(owners, [])


def fix_commands(pbis: list[dict[str, Any]], plan_checks: list[str], files: Iterable[str]) -> list[str]:
    """The checks of PBIs that own a fixed file and the plan's checks.

    With neither, every PBI's checks, so a fix never goes unchecked.
    """
    commands = wave_commands([{"checks": related_commands(pbis, files)}], plan_checks)
    return commands or wave_commands(pbis, [])


def validate_acceptance_review(obj: Any, expected_files: Iterable[str]) -> list[str]:
    """Validate an atelier.acceptance_review/1 object: every expected file once, each ok or fix with a reason."""
    if not isinstance(obj, dict):
        return ["受け入れテストの確認の結果は JSON オブジェクトにしてください。"]
    errors: list[str] = []
    if obj.get("schema") != SCHEMA_ACCEPTANCE_REVIEW:
        errors.append(f"schema は {SCHEMA_ACCEPTANCE_REVIEW} にしてください。")
    items = obj.get("files")
    if not isinstance(items, list):
        errors.append("files は配列にしてください。")
        return errors
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"files[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} はオブジェクトにしてください。")
            continue
        file = item.get("file")
        if not _is_nonempty_str(file):
            errors.append(f"{label} の file は空でない文字列にしてください。")
        else:
            file = _norm_path(file)
            if file in seen:
                errors.append(f"ファイル {file} が重複しています。")
            seen.add(file)
            label = file
        verdict = item.get("verdict")
        if verdict not in REVIEW_VERDICTS:
            errors.append(f"{label} の verdict は {', '.join(REVIEW_VERDICTS)} のいずれかにしてください。")
        elif verdict == "fix" and not _is_nonempty_str(item.get("reason")):
            errors.append(f"{label} は fix なので reason を空でない文字列で書いてください。")
    expected = {_norm_path(f) for f in expected_files}
    missing = sorted(expected - seen)
    extra = sorted(seen - expected)
    if missing:
        errors.append(f"受け入れテストのファイルについて返していません: {', '.join(missing)}。")
    if extra:
        errors.append(f"確認の対象でないファイルが含まれています: {', '.join(extra)}。")
    return errors


def _validate_unlock(
    unlock: Any,
    label: str,
    decision: Any,
    locked_cases: Mapping[str, str] | None,
    plan_cases: set[str],
    errors: list[str],
) -> list[str]:
    """Check a triage item's `unlock`: the locked cases whose lock the decision takes off, and what it decided for them
    (fix the scenario, or fix the implementation). Returns its case ids (none when they cannot be read).

    With locked_cases (each locked case -> its requirement) the cases are checked against the lock too: they are the
    locked cases of one requirement whose lines are fixed, and new ids for the lines added to it (requirement names it
    when no locked case does). A case of a requirement that is not locked is refused, and so is an unlock in a run
    with no locked case.
    """
    where = f"{label} の unlock"
    if not isinstance(unlock, dict):
        errors.append(f"{where} は cases と fix を持つオブジェクトにしてください。")
        return []
    extra = sorted(set(unlock) - set(UNLOCK_FIELDS))
    if extra:
        errors.append(f"{where} に使えない項目があります: {', '.join(extra)}")
    ids = _validate_str_list(unlock.get("cases"), f"{where}.cases", errors, require_nonempty_list=True) or []
    repeated = sorted({cid for cid in ids if ids.count(cid) > 1})
    if repeated:
        errors.append(f"{where}.cases に同じケースが重複しています: {', '.join(repeated)}")
    if unlock.get("fix") not in UNLOCK_FIXES:
        errors.append(f"{where}.fix は scenario（シナリオを直す）か implementation（実装を直す）にしてください。")
    requirement = unlock.get("requirement")
    if requirement is not None and not _is_nonempty_str(requirement):
        errors.append(f"{where}.requirement は空でない文字列にしてください（省くと cases の鍵のケースの要件）。")
        requirement = None
    if decision in TRIAGE_DECISIONS and decision not in UNLOCK_DECISIONS:
        errors.append(f"{label} は unlock を持つので、decision を fix か ask にしてください（見送るなら unlock を省く）。")
    if locked_cases is None or not ids:
        return ids
    if not locked_cases:
        errors.append(f"{where}: この run には鍵をかけたケースが無いので、鍵を外せません。")
        return ids
    owners = sorted({locked_cases[cid] for cid in ids if cid in locked_cases})
    if len(owners) > 1:
        errors.append(f"{where}.cases は 1 つの要件のケースにしてください（{', '.join(owners)} にまたがっています）。")
        return ids
    if requirement is not None and requirement not in set(locked_cases.values()):
        errors.append(f"{where}.requirement {requirement!r} は鍵をかけた要件ではありません。")
        return ids
    if requirement is not None and owners and owners[0] != requirement:
        errors.append(f"{where}.cases の鍵のケースは要件 {owners[0]} のもので、requirement の {requirement} と違います。")
    unlocked = [cid for cid in ids if cid not in locked_cases and cid in plan_cases]
    if unlocked:
        errors.append(f"{where}.cases の {', '.join(unlocked)} は鍵をかけていない要件のケースです。")
    added = [cid for cid in ids if cid not in locked_cases and cid not in plan_cases]
    if added and requirement is None and not owners:
        errors.append(
            f"{where}.cases の {', '.join(added)} は鍵のケースに無い（足す行）ので、足す要件を requirement に書いてください。"
        )
    return ids


def validate_triage(
    obj: Any,
    expected_ids: Iterable[str] | None = None,
    locked_cases: Mapping[str, str] | None = None,
    plan_cases: Iterable[str] | None = None,
) -> list[str]:
    """Validate an atelier.triage/1 object and return Japanese error strings.

    expected_ids are the ids of the retrospective list: the result must have each of them once, and no other id.
    An item may carry `unlock` ({"cases", "fix", "requirement"}): the decision takes the lock off those cases, and
    fix says what it decided, `scenario` (the test lines change to what the implementation does) or `implementation`
    (the test lines change and the implementation follows them). Such an item is fixed or asked, never skipped, and
    a case is unlocked by one item only. locked_cases (each locked case -> its requirement) and plan_cases (every case
    id of the plan) check the cases against the lock as well (_validate_unlock).
    """
    if not isinstance(obj, dict):
        return ["仕分け結果は JSON オブジェクトにしてください。"]

    errors: list[str] = []
    if obj.get("schema") != SCHEMA_TRIAGE:
        errors.append(f"schema は {SCHEMA_TRIAGE} にしてください。")

    items = obj.get("items")
    if not isinstance(items, list):
        errors.append("items は配列にしてください。")
        return errors

    seen_ids: set[str] = set()
    unlocked_by: dict[str, str] = {}
    known_cases = {str(c) for c in plan_cases or []}
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} はオブジェクトにしてください。")
            continue

        fid = item.get("id")
        if not _is_nonempty_str(fid):
            errors.append(f"{label} の id は空でない文字列にしてください。")
        elif fid in seen_ids:
            errors.append(f"仕分け対象 id {fid!r} が重複しています（振り返りの一覧の各件は 1 回だけ仕分けてください）。")
        else:
            seen_ids.add(fid)
            label = fid

        if item.get("decision") not in TRIAGE_DECISIONS:
            errors.append(f"{label} の decision は {', '.join(TRIAGE_DECISIONS)} のいずれかにしてください。")

        if not _is_nonempty_str(item.get("reason")):
            errors.append(f"{label} の reason は空でない文字列にしてください。")

        if "unlock" in item:
            for cid in _validate_unlock(item["unlock"], label, item.get("decision"), locked_cases, known_cases, errors):
                if cid in unlocked_by and unlocked_by[cid] != label:
                    errors.append(f"ケース {cid} の鍵を {unlocked_by[cid]} と {label} の 2 件で外しています（1 件にしてください）。")
                unlocked_by.setdefault(cid, label)

    if expected_ids is not None:
        expected = set(expected_ids)
        missing = sorted(expected - seen_ids)
        extra = sorted(seen_ids - expected)
        if missing:
            errors.append(f"仕分け結果に振り返りの一覧の件が足りません: {', '.join(missing)}")
        if extra:
            errors.append(f"仕分け結果に振り返りの一覧に無い id があります: {', '.join(extra)}")

    return errors


def is_unlock(item: Any) -> bool:
    """True for an item of the fix list whose triage took the lock off locked cases (it carries an `unlock`)."""
    return isinstance(item, dict) and isinstance(item.get("unlock"), dict)


def _item_files(item: dict[str, Any]) -> list[str]:
    """The files an item of the fix list touches: its file and files, and the files a retrospective item affects.

    An item with an unlock touches only the acceptance test file the unlock opens.
    """
    if is_unlock(item):
        raw = [item["unlock"].get("file")]
    else:
        affects = item.get("affects") if isinstance(item.get("affects"), dict) else {}
        raw = [item.get("file"), *(item.get("files") or []), *(affects.get("files") or [])]
    out: list[str] = []
    for path in raw:
        if _is_nonempty_str(path):
            norm = _norm_path(path)
            if norm not in out:
                out.append(norm)
    return out


def fix_groups(findings: list[dict[str, Any]], max_workers: int = 3) -> list[list[dict[str, Any]]]:
    """Group findings by shared files and pack into at most max_workers disjoint groups.

    An item with an unlock is a group of its own, and those groups come first: their workers fix locked acceptance
    tests the way an acceptance PBI writes them, and the run checks each fix and locks the tests again before the
    other groups change the implementation. max_workers bounds the other groups.
    """
    if max_workers < 1:
        raise ValueError("max_workers は 1 以上にしてください。")
    unlocks = [[item] for item in findings if is_unlock(item)]
    return unlocks + _shared_file_groups([item for item in findings if not is_unlock(item)], max_workers)


def _shared_file_groups(findings: list[dict[str, Any]], max_workers: int) -> list[list[dict[str, Any]]]:
    """Group findings by shared files and pack into at most max_workers disjoint groups."""
    if not findings:
        return []

    parent: dict[str, str] = {}

    def find(key: str) -> str:
        while parent.setdefault(key, key) != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    item_keys: list[str] = []
    for idx, item in enumerate(findings):
        files = _item_files(item)
        if not files:
            key = f"__isolated__:{idx}"
            find(key)
        else:
            key = files[0]
            find(key)
            for other in files[1:]:
                parent[find(other)] = find(key)
        item_keys.append(key)

    chunks_by_root: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for idx, (item, key) in enumerate(zip(findings, item_keys)):
        chunks_by_root.setdefault(find(key), []).append((idx, item))

    chunks = list(chunks_by_root.values())
    bins: list[list[tuple[int, dict[str, Any]]]] = [[] for _ in range(min(max_workers, len(chunks)))]
    for chunk in sorted(chunks, key=lambda c: len(c), reverse=True):
        min(bins, key=len).extend(chunk)

    groups: list[list[tuple[int, dict[str, Any]]]] = [sorted(b, key=lambda pair: pair[0]) for b in bins if b]
    groups.sort(key=lambda g: g[0][0])
    return [[item for _, item in g] for g in groups]


def _load_levels_json() -> list[dict[str, Any]]:
    try:
        return json.loads(_LEVELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def rule_docs_lines(root: Path, rule_docs: Iterable[str] | None) -> list[str]:
    """Prompt lines naming the rule documents every role reads in full before it starts."""
    docs = [d for d in (rule_docs or []) if _is_nonempty_str(d)]
    if not docs:
        return ["Rule documents: none"]
    return ["Rule documents (read each in full before you start):"] + [f"- {(root / d).resolve()}" for d in docs]


def planner_prompt(
    root: str | Path,
    mode: str,
    *,
    spec_path: str | Path,
    facts_path: str | Path,
    result_path: str | Path,
    plan_path: str | Path | None = None,
    request_text: str | None = None,
    findings_path: str | Path | None = None,
    retro_path: str | Path | None = None,
    levels: list[dict[str, Any]] | None = None,
    out_path: str | Path | None = None,
    rule_docs: Iterable[str] | None = None,
    passed_pbis: Iterable[str] | None = None,
    acceptance_first: bool | None = None,
    scenarios: list[dict[str, Any]] | None = None,
    interfaces: list[dict[str, Any]] | None = None,
    acceptance_files: list[dict[str, Any]] | None = None,
    backlog: list[dict[str, Any]] | None = None,
    backlog_note: str | None = None,
) -> Path:
    """Write a planner prompt for mode ('plan', 'revise', 'triage' or 'acceptance_review') using only approved spec and facts.

    In triage the input is the retrospective list (retro_path; findings_path is the older name of the same input).
    In plan and revise, backlog lists the backlog entries the plan may take as PBIs (backlog_note says why there are
    none, for example when the work root is not a git repository).
    """
    if mode not in ("plan", "revise", "triage", "acceptance_review"):
        raise ValueError(f"unsupported planner mode: {mode}")

    root_p = Path(root).resolve()
    prompt = Path(out_path) if out_path else root_p / "docs" / "pbi" / "prompts" / f"planner-{mode}.md"
    result_p = Path(result_path).resolve()
    prompt.parent.mkdir(parents=True, exist_ok=True)
    result_p.parent.mkdir(parents=True, exist_ok=True)

    template = (_PROMPTS_DIR / "planner.md").read_text(encoding="utf-8")
    lines = [
        "",
        f"Mode: {mode}",
        f"Repository: {root_p}",
        f"Approved spec: {Path(spec_path).resolve()}",
        f"Fact sheet: {Path(facts_path).resolve()}",
        *rule_docs_lines(root_p, rule_docs),
    ]
    if mode in ("plan", "revise"):
        lvl_data = levels if levels is not None else _load_levels_json()
        lines.append(f"Levels: {json.dumps(lvl_data, ensure_ascii=False)}")
        lines.append(f"Acceptance first: {'on' if acceptance_first else 'off'}")
        if backlog:
            lines.append(f"Backlog candidates: {json.dumps(backlog, ensure_ascii=False)}")
        else:
            lines.append(f"Backlog candidates: none{f' ({backlog_note})' if backlog_note else ''}")
    if mode == "revise":
        if plan_path is not None:
            lines.append(f"Current plan: {Path(plan_path).resolve()}")
        lines.append(f"User revision request: {request_text or ''}")
        lines.append(f"Passed PBIs: {', '.join(passed_pbis or []) or 'none'}")
    if mode == "triage":
        listing = retro_path if retro_path is not None else findings_path
        if listing is not None:
            lines.append(f"Retrospective list: {Path(listing).resolve()}")
    if mode == "acceptance_review":
        lines.append(f"Approved scenarios: {json.dumps(scenarios or [], ensure_ascii=False)}")
        lines.append(f"Public interfaces: {json.dumps(interfaces or [], ensure_ascii=False)}")
        lines.append(f"Acceptance test files: {json.dumps(acceptance_files or [], ensure_ascii=False)}")
    lines.append(f"Result: {result_p}")

    prompt.write_text(template + "\n".join(lines) + "\n", encoding="utf-8")
    return prompt


def worker_prompt(
    root: str | Path,
    *,
    mode: str,
    ticket: dict[str, Any],
    result_path: str | Path,
    out_path: str | Path | None = None,
    rule_docs: Iterable[str] | None = None,
) -> Path:
    """Write a worker prompt for mode ('pbi' or 'fix') from its ticket alone."""
    if mode not in ("pbi", "fix"):
        raise ValueError(f"unsupported worker mode: {mode}")

    root_p = Path(root).resolve()
    ticket_id = str(ticket.get("id") or mode)
    prompt = Path(out_path) if out_path else root_p / "docs" / "pbi" / "prompts" / f"worker-{ticket_id}.md"
    result_p = Path(result_path).resolve()
    prompt.parent.mkdir(parents=True, exist_ok=True)
    result_p.parent.mkdir(parents=True, exist_ok=True)

    template = (_PROMPTS_DIR / "worker.md").read_text(encoding="utf-8")
    parts = [
        template,
        f"Mode: {mode}",
        f"Repository: {root_p}",
        f"Result: {result_p}",
        *rule_docs_lines(root_p, rule_docs),
        "",
        "## Ticket",
        "```json",
        json.dumps(ticket, ensure_ascii=False, indent=2),
        "```",
    ]

    prompt.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return prompt


def reviewer_prompt(
    root: str | Path,
    *,
    spec_path: str | Path,
    result_path: str | Path,
    base_ref: str = "HEAD",
    diff_stat: str | None = None,
    out_path: str | Path | None = None,
    rule_docs: Iterable[str] | None = None,
) -> Path:
    """Write a reviewer prompt for the single whole-diff review against the approved spec."""
    root_p = Path(root).resolve()
    prompt = Path(out_path) if out_path else root_p / "docs" / "pbi" / "prompts" / "review.md"
    result_p = Path(result_path).resolve()
    prompt.parent.mkdir(parents=True, exist_ok=True)
    result_p.parent.mkdir(parents=True, exist_ok=True)

    template = (_PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")
    lines = [
        "",
        f"Repository: {root_p}",
        f"Approved spec: {Path(spec_path).resolve()}",
        f"Base: {base_ref}",
        *rule_docs_lines(root_p, rule_docs),
    ]
    if diff_stat:
        lines.extend(["Diff stat:", diff_stat])
    lines.append(f"Result: {result_p}")

    prompt.write_text(template + "\n".join(lines) + "\n", encoding="utf-8")
    return prompt
