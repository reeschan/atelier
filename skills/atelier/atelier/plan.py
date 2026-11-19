"""PBI plan, wave ordering, finding validation/filtering, triage, fix grouping, and role prompts."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .survey import inside_repo

SCHEMA_PLAN = "atelier.plan/1"
SCHEMA_FINDINGS = "atelier.findings/1"
SCHEMA_TRIAGE = "atelier.triage/1"

LEVELS = ("L1", "L2", "L3", "L4", "L5")
SEVERITIES = ("critical", "major", "minor")
FINDING_KINDS = ("correctness", "spec", "rule", "style")
EVIDENCE_PATTERN = re.compile(r".+:[1-9][0-9]*")
# Fields a re-planned PBI that already passed must keep unchanged.
FROZEN_FIELDS = ("id", "goal", "targets", "checks")
TRIAGE_DECISIONS = ("fix", "skip", "ask")
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
        changed = [f for f in FROZEN_FIELDS if new.get(f) != old.get(f)]
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


def validate_plan(obj: Any) -> list[str]:
    """Validate an atelier.plan/1 object and return Japanese error strings."""
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
        targets = _validate_str_list(pbi.get("targets"), f"{label}.targets", errors, require_nonempty_list=True)
        dnt = _validate_str_list(pbi.get("do_not_touch"), f"{label}.do_not_touch", errors)
        if targets is not None and dnt is not None:
            overlap = {_norm_path(p) for p in targets} & {_norm_path(p) for p in dnt}
            if overlap:
                errors.append(f"{label} の targets と do_not_touch が重なっています: {', '.join(sorted(overlap))}")

        _validate_str_list(pbi.get("checks"), f"{label}.checks", errors, require_nonempty_list=True)
        _validate_str_list(pbi.get("facts"), f"{label}.facts", errors)
        _validate_str_list(pbi.get("acceptance"), f"{label}.acceptance", errors, require_nonempty_list=True)
        evidence = _validate_str_list(pbi.get("evidence"), f"{label}.evidence", errors, require_nonempty_list=True)
        for place in evidence or []:
            if not EVIDENCE_PATTERN.fullmatch(place.strip()):
                errors.append(f"{label}.evidence の {place!r} は path:行番号 にしてください。")
        _validate_data_needs(pbi.get("data_needs"), f"{label}.data_needs", errors)

        if _is_nonempty_str(pid):
            valid_pbi_items.append(pbi)

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
                _compute_waves(pbis)
            except ValueError as exc:
                errors.append(str(exc))

    return errors


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


def fix_commands(pbis: list[dict[str, Any]], plan_checks: list[str], files: Iterable[str]) -> list[str]:
    """The plan's checks and the checks of PBIs that own a fixed file.

    With neither, every PBI's checks, so a fix never goes unchecked.
    """
    fixed = {_norm_path(f) for f in files if _is_nonempty_str(f)}
    owners = [p for p in pbis if fixed & {_norm_path(t) for t in (p.get("targets") or []) if _is_nonempty_str(t)}]
    commands = wave_commands(owners, plan_checks)
    return commands or wave_commands(pbis, [])


def validate_triage(obj: Any, expected_ids: Iterable[str] | None = None) -> list[str]:
    """Validate an atelier.triage/1 object and return Japanese error strings."""
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
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} はオブジェクトにしてください。")
            continue

        fid = item.get("id")
        if not _is_nonempty_str(fid):
            errors.append(f"{label} の id は空でない文字列にしてください。")
        elif fid in seen_ids:
            errors.append(f"仕分け対象 id {fid!r} が重複しています。")
        else:
            seen_ids.add(fid)
            label = fid

        if item.get("decision") not in TRIAGE_DECISIONS:
            errors.append(f"{label} の decision は {', '.join(TRIAGE_DECISIONS)} のいずれかにしてください。")

        if not _is_nonempty_str(item.get("reason")):
            errors.append(f"{label} の reason は空でない文字列にしてください。")

    if expected_ids is not None:
        expected = set(expected_ids)
        missing = sorted(expected - seen_ids)
        extra = sorted(seen_ids - expected)
        if missing:
            errors.append(f"仕分け結果に不足している指摘があります: {', '.join(missing)}")
        if extra:
            errors.append(f"仕分け結果に未知の指摘 id があります: {', '.join(extra)}")

    return errors


def _item_files(item: dict[str, Any]) -> list[str]:
    raw = [item.get("file"), *(item.get("files") or [])]
    out: list[str] = []
    for path in raw:
        if _is_nonempty_str(path):
            norm = _norm_path(path)
            if norm not in out:
                out.append(norm)
    return out


def fix_groups(findings: list[dict[str, Any]], max_workers: int = 3) -> list[list[dict[str, Any]]]:
    """Group findings by shared files and pack into at most max_workers disjoint groups."""
    if max_workers < 1:
        raise ValueError("max_workers は 1 以上にしてください。")
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
    levels: list[dict[str, Any]] | None = None,
    out_path: str | Path | None = None,
    rule_docs: Iterable[str] | None = None,
    passed_pbis: Iterable[str] | None = None,
) -> Path:
    """Write a planner prompt for mode ('plan', 'revise', or 'triage') using only approved spec and facts."""
    if mode not in ("plan", "revise", "triage"):
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
    if mode == "revise":
        if plan_path is not None:
            lines.append(f"Current plan: {Path(plan_path).resolve()}")
        lines.append(f"User revision request: {request_text or ''}")
        lines.append(f"Passed PBIs: {', '.join(passed_pbis or []) or 'none'}")
    if mode == "triage":
        if findings_path is not None:
            lines.append(f"Findings: {Path(findings_path).resolve()}")
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
