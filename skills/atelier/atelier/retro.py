"""The retrospective list (振り返りの一覧): everything a run set aside to decide once at the end, in one shape.

After the final review the run gathers the review's findings, the workers' objections, the run's unresolved items and
its plan returns, and one light_review item per requirement whose acceptance test is light (light or light-no-red)
or was left unresolved and run as light, into one list; after E2E, a failed E2E joins it as e2e_failure items (the
command and the end of its output). Each item has the same fields whatever its kind: id, kind, what, why, tried, state,
interpretation, options, recommendation, affects (the PBIs and the files) and source (the record it came from). A
review item also keeps the finding's own fields (file, line, severity, confidence, fix and rule), with the finding's
kind as `category`.

Only review items are left out for a confidence under 80 or for being about style; the other kinds stay on the list
without a confidence. The list is written outside the work tree, to <git common dir>/atelier/<run>/retro.json (the run
folder when the root is not a git work tree), and the run keeps its place and counts in run["retro"]. The planner's
triage reads it and sorts every item once.

The triage may take the lock off locked cases: a decision that fixes or asks an item can carry an `unlock` (the cases,
and whether the scenario or the implementation is fixed). An unlock_request item carries the unlock it asks for
(requested_unlock), which its decision takes when it names none. An objection item names its case's requirement and
whether the case is locked, which an unlock needs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import acceptance, locks, plan, state

SCHEMA = "atelier.retro/1"
FILE_NAME = "retro.json"

KINDS = (
    "review",
    "objection",
    "unresolved",
    "plan_return",
    "requirement_gap",
    "unit_gap",
    "e2e_failure",
    "unlock_request",
    "light_review",
)
KIND_LABELS = {
    "review": "見直しの指摘",
    "objection": "異議",
    "unresolved": "未解決",
    "plan_return": "途中の戻り",
    "requirement_gap": "要件の穴",
    "unit_gap": "ユニットの穴",
    "e2e_failure": "E2E の失敗",
    "unlock_request": "鍵を外す要望",
    "light_review": "軽量の見直し",
}
# The fields every item has, in this order.
FIELDS = (
    "id",
    "kind",
    "what",
    "why",
    "tried",
    "state",
    "interpretation",
    "options",
    "recommendation",
    "affects",
    "source",
)
TEXT_FIELDS = ("what", "why", "tried", "state", "interpretation", "recommendation")
REQUIRED_TEXT = ("what", "why")
# The kinds the confidence and style filter applies to.
FILTERED_KINDS = ("review",)
CATEGORY_LABELS = {"correctness": "正しさ", "spec": "仕様", "rule": "規約", "style": "書き方"}
NONE_TEXT = "なし"


class RetroError(ValueError):
    """An item of the retrospective list has an unknown kind or a wrong shape."""


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _str_list(values: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = _str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def make_item(
    kind: str,
    item_id: str,
    *,
    what: str,
    why: str,
    tried: str = "",
    state: str = "",
    interpretation: str = NONE_TEXT,
    options: Iterable[str] | None = None,
    recommendation: str = "",
    affects_pbis: Iterable[str] | None = None,
    affects_files: Iterable[str] | None = None,
    source: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One item of the retrospective list. An unknown kind is refused (RetroError)."""
    if kind not in KINDS:
        raise RetroError(f"振り返りの一覧の種類 {kind!r} を知りません（{', '.join(KINDS)} のいずれか）。")
    clash = sorted(set(extra) & set(FIELDS))
    if clash:
        raise RetroError(f"振り返りの一覧の欄 {', '.join(clash)} は決まった引数で渡してください。")
    item: dict[str, Any] = {
        "id": _str(item_id),
        "kind": kind,
        "what": _str(what),
        "why": _str(why),
        "tried": _str(tried),
        "state": _str(state),
        "interpretation": _str(interpretation),
        "options": _str_list(options),
        "recommendation": _str(recommendation),
        "affects": {"pbis": _str_list(affects_pbis), "files": _str_list(affects_files)},
        "source": dict(source or {}),
    }
    item.update(extra)
    return item


def validate_items(items: Any) -> list[str]:
    """Japanese errors for a list of items that is not of this shape: a known kind, a unique non-empty id, every field."""
    if not isinstance(items, list):
        return ["振り返りの一覧の items は配列にしてください。"]
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} はオブジェクトにしてください。")
            continue
        iid = item.get("id")
        if not isinstance(iid, str) or not iid.strip():
            errors.append(f"{label} の id は空でない文字列にしてください。")
        elif iid in seen:
            errors.append(f"振り返りの一覧の id {iid!r} が重複しています。")
        else:
            seen.add(iid)
            label = iid
        if item.get("kind") not in KINDS:
            errors.append(f"{label} の kind {item.get('kind')!r} を知りません（{', '.join(KINDS)} のいずれか）。")
        for field in TEXT_FIELDS:
            if not isinstance(item.get(field), str):
                errors.append(f"{label} の {field} は文字列にしてください。")
            elif field in REQUIRED_TEXT and not item[field].strip():
                errors.append(f"{label} の {field} は空でない文字列にしてください。")
        options = item.get("options")
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            errors.append(f"{label} の options は文字列の配列にしてください。")
        affects = item.get("affects")
        if not isinstance(affects, dict) or not all(
            isinstance(affects.get(k), list) and all(isinstance(v, str) for v in affects[k]) for k in ("pbis", "files")
        ):
            errors.append(f"{label} の affects は pbis と files（文字列の配列）を持つオブジェクトにしてください。")
        if not isinstance(item.get("source"), dict):
            errors.append(f"{label} の source はオブジェクトにしてください。")
    return errors


def _targets(run: dict[str, Any], pbi_ids: Iterable[str]) -> list[str]:
    files: list[str] = []
    for pid in pbi_ids:
        pbi = state.get_pbi(run, pid) or {}
        files += [str(t) for t in pbi.get("targets") or [] if str(t) not in files]
    return files


def _owners(run: dict[str, Any], files: Iterable[str]) -> list[str]:
    """The PBIs that have one of the files in their targets, in plan order."""
    wanted = [f for f in files if f]
    return [
        p["id"] for p in run.get("pbis", [])
        if p.get("kind") != "fix" and any(plan.paths_overlap(str(t), f) for t in p.get("targets") or [] for f in wanted)
    ]


def _review_item(run: dict[str, Any], finding: dict[str, Any]) -> dict[str, Any]:
    """A finding of the final review as an item; the finding's own fields stay, its kind as `category`."""
    fid = _str(finding.get("id"))
    files = plan._item_files(finding)
    category = _str(finding.get("kind"))
    severity = _str(finding.get("severity"))
    fix = _str(finding.get("fix"))
    location = f"{finding.get('file')}:{finding.get('line')}" if finding.get("line") else _str(finding.get("file"))
    extra = {k: v for k, v in finding.items() if k not in ("id", "kind", "why", "category", "title", *FIELDS)}
    return make_item(
        "review",
        fid,
        what=f"{location} の{CATEGORY_LABELS.get(category, category)}の指摘（{severity}）",
        why=_str(finding.get("why")),
        tried="最後の見直しで見つかった。まだ直していない",
        state="未修正",
        options=[f"直す: {fix}" if fix else "直す", "見送る（backlog に残す）"],
        recommendation="直す" if severity in ("critical", "major") else "見送る",
        affects_pbis=_owners(run, files),
        affects_files=files,
        source={"record": "最後の見直し", "finding": dict(finding)},
        **extra,
        category=category,
        title=fix or _str(finding.get("why")) or fid,
    )


OBJECTION_TRIED = {
    "green": "書いてあるとおりに作った（ケースは緑）",
    "red": "書いてあるとおりに作れず、ケースを赤のまま残した",
}
OBJECTION_STATE = {"green": "緑（シナリオのとおり）", "red": "赤（ケースが落ちたまま）"}
OBJECTION_OPTIONS = ["シナリオのとおりのままにする", "仮の解釈に合わせてシナリオを直す（鍵を外す）", "実装を直す"]
# What going with the worker's reading takes, by where the case stands: a red case was built to the reading, so only
# the locked lines change (unlock, fix scenario); a green case was built as written, so the lines and the
# implementation change (unlock, fix implementation).
OBJECTION_OPTIONS_BY_STATE = {
    "red": ["シナリオのとおりに実装を直す", "仮の解釈に合わせてシナリオを直す（鍵を外す: シナリオを直す）"],
    "green": ["シナリオのとおりのままにする", "仮の解釈に合わせてシナリオと実装を直す（鍵を外す: 実装を直す）"],
}


def objection_items(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The workers' objections, one item each, in plan order.

    Each item also names the case's requirement and whether the case is locked, which an unlock decision needs.
    """
    items: list[dict[str, Any]] = []
    requirements = acceptance.case_requirements(run)
    locked = locks.locked_cases(run)
    for pbi in run.get("pbis", []):
        for index, obj in enumerate(pbi.get("objections") or []):
            if not isinstance(obj, dict):
                continue
            status = _str(obj.get("state"))
            case = _str(obj.get("case"))
            items.append(make_item(
                "objection",
                f"OB-{len(items) + 1}",
                what=f"{pbi['id']} のケース {case or '（不明）'}: {_str(obj.get('what'))}",
                why=_str(obj.get("why")),
                tried=OBJECTION_TRIED.get(status, status),
                state=OBJECTION_STATE.get(status, status),
                interpretation=_str(obj.get("interpretation")) or NONE_TEXT,
                options=OBJECTION_OPTIONS_BY_STATE.get(status, OBJECTION_OPTIONS),
                recommendation=(
                    "ユーザーに聞く（要件の解釈が変わる）" if status == "red" else "シナリオのとおりのままにする"
                ),
                affects_pbis=[pbi["id"]],
                affects_files=pbi.get("targets") or [],
                source={"record": f"run.json の {pbi['id']} の objections[{index}]", "pbi": pbi["id"], "objection": dict(obj)},
                case=case,
                requirement=requirements.get(case, ""),
                locked=case in locked,
            ))
    return items


def requested_unlock(item: dict[str, Any]) -> dict[str, Any] | None:
    """The unlock an unlock_request item asks for ({"cases", "fix", "requirement"}), or None.

    The triage takes it as its decision's unlock when it fixes or asks the item without naming one of its own.
    """
    unlock = item.get("unlock") if item.get("kind") == "unlock_request" else None
    return dict(unlock) if isinstance(unlock, dict) else None


def _unresolved_what(item: dict[str, Any]) -> str:
    kind = item.get("kind")
    if item.get("requirement"):
        return f"要件 {item['requirement']} の受け入れテスト"
    if kind == acceptance.CARRY_OVER_UNRESOLVED:
        commands = "、".join(item.get("commands") or []) or "PBI の検査"
        return f"持ち越し {item.get('carry_over')} の検査（{commands}）"
    if kind == acceptance.CHECK_UNRESOLVED:
        return f"wave の検査のコマンド {item.get('command') or '（不明）'}"
    if kind == acceptance.WAVE_LOCK_UNRESOLVED:
        return "鍵のファイル・キー・シグネチャの変更"
    if kind == acceptance.WAVE_CASES_UNRESOLVED:
        return "wave の検査で流れなかった受け入れのケース"
    if kind == acceptance.WAVE_CASES_RED_UNRESOLVED:
        return "前の wave で通り、赤に戻った受け入れのケース"
    return _str(kind) or "未解決の件"


def _unresolved_state(item: dict[str, Any]) -> str:
    kind = item.get("kind")
    if item.get("requirement"):
        return "未解決（鍵をかけずに残し、その要件の PBI は軽量と同じ扱いで進めた）"
    if kind == acceptance.CARRY_OVER_UNRESOLVED:
        return "未解決（聞かずに保留にした）"
    return "未解決（持ち主の PBI が決まらない）"


def unresolved_items(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The run's unresolved items, one item each, with the report's line on it as what was tried."""
    items: list[dict[str, Any]] = []
    for entry in run.get("unresolved") or []:
        if not isinstance(entry, dict):
            continue
        lines = [ln for ln in acceptance.unresolved_lines({"unresolved": [entry]}) if ln.startswith("- ")]
        pbis = _str_list([
            *(entry.get("pbis") or []),
            *(entry.get("as_light_pbis") or []),
            *(entry.get("held_pbis") or []),
        ])
        requirement_test = bool(entry.get("requirement"))
        items.append(make_item(
            "unresolved",
            _str(entry.get("id")) or f"UR-{len(items) + 1}",
            what=_unresolved_what(entry),
            why=_str(entry.get("reason")) or "（記載なし）",
            tried=lines[0][2:] if lines else _str(entry.get("reason")),
            state=_unresolved_state(entry),
            options=["直す", "ユーザーに聞く", "見送る（backlog に残す）"],
            recommendation="ユーザーに聞く（要件の受け入れテストを決め直す）" if requirement_test else "直す",
            affects_pbis=pbis,
            affects_files=_targets(run, pbis),
            source={"record": f"run.json の unresolved の {entry.get('id')}", "unresolved": dict(entry)},
        ))
    return items


PLAN_RETURN_STATES = {
    "continue": "異議として続けた（止まった PBI は作ったまま検査に回した）",
    "replan": "計画を直した",
}


def plan_return_items(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The run's plan returns, one item each."""
    items: list[dict[str, Any]] = []
    for record in run.get("plan_returns") or []:
        if not isinstance(record, dict):
            continue
        stops = [s for s in record.get("stops") or [] if isinstance(s, dict)]
        decision = _str(record.get("decision"))
        answer = state.PLAN_RETURN_DECISIONS.get(decision, decision)
        note = _str(record.get("note")).strip()
        pbis = _str_list([*(s.get("pbi") for s in stops), *(record.get("held_pbis") or [])])
        readings = [
            f"{o.get('pbi')}: {o.get('interpretation')}" for o in record.get("objections") or []
            if isinstance(o, dict) and o.get("interpretation")
        ]
        items.append(make_item(
            "plan_return",
            _str(record.get("id")) or f"PR-{len(items) + 1}",
            what="、".join(f"{s.get('pbi')} の stop（例外 {s.get('exception')}: {s.get('label')}）" for s in stops)
            or "作業者の stop",
            why="; ".join(_str(s.get("reason")) for s in stops if s.get("reason")) or "（記載なし）",
            tried=f"作業者が stop を返し、ユーザーが「{answer}」と答えた" + (f"。メモ: {note}" if note else ""),
            state=PLAN_RETURN_STATES.get(decision, decision),
            interpretation="; ".join(readings) or NONE_TEXT,
            options=["計画の前提を直す（公開の口やシナリオを変える）", "このままにする（見送る）"],
            recommendation="見送る（計画は直した）" if decision == "replan" else "ユーザーに聞く（前提が変わる）",
            affects_pbis=pbis,
            affects_files=_targets(run, pbis),
            source={"record": f"run.json の plan_returns の {record.get('id')}", "plan_return": dict(record)},
        ))
    return items


LIGHT_WHY = {
    "light": "要件を軽量（light）で計画した: Gherkin と Examples を書かず、テストの名前だけで受け入れを確かめた",
    acceptance.NO_RED_MODE: "要件を軽量・Red なし（light-no-red）で計画した: テストの名前だけで、Red の確認も流していない",
}
LIGHT_TRIED = {
    "light": "テストの名前で受け入れテストを書き、Red の確認と wave の検査で流した",
    acceptance.NO_RED_MODE: "テストの名前で受け入れテストを書いた。Red の確認は流さず、wave の検査で流しただけ",
}
UNRESOLVED_LIGHT_WHY = "受け入れテストが決まらず未解決に残し、鍵をかけずに軽量と同じ扱いで進めた"
UNRESOLVED_LIGHT_TRIED = "受け入れテストを書いたが Red の確認か見直しで決まらず、そのケースに鍵をかけないまま PBI を作った"
RED_STATES = {"passed": "Red の確認は合格", "unresolved": "Red の確認は未解決", "skipped": "Red の確認は流さない"}


def light_review_items(run: dict[str, Any]) -> list[dict[str, Any]]:
    """One light_review item per light or light-no-red requirement, and per requirement left unresolved and run as
    light, in plan order: its case ids, test names and the PBIs its cases are assigned to.

    A requirement left unresolved is one an unresolved entry names (with the PBIs it put on light, as_light_pbis), or
    one a PBI lists in as_light.
    """
    unresolved: dict[str, list[dict[str, Any]]] = {}
    for entry in run.get("unresolved") or []:
        if isinstance(entry, dict) and entry.get("requirement"):
            unresolved.setdefault(_str(entry["requirement"]), []).append(entry)
    as_light: dict[str, list[str]] = {}
    for pbi in run.get("pbis", []):
        for rid in pbi.get("as_light") or []:
            as_light.setdefault(_str(rid), []).append(pbi["id"])
    red = run.get("red") or {}
    items: list[dict[str, Any]] = []
    for sc in acceptance.scenarios(run):
        rid = sc["id"]
        mode = _str(sc.get("mode"))
        light = mode in LIGHT_WHY
        left = rid in unresolved or rid in as_light
        if not light and not left:
            continue
        cases = [c for c in sc.get("cases") or [] if isinstance(c, dict)]
        case_ids = _str_list(c.get("id") for c in cases)
        test_names = _str_list(
            c.get("test_name") or c.get("name") or (sc.get("text") if light else "") for c in cases
        )
        entries = unresolved.get(rid, [])
        pbis = _str_list([
            *(c.get("pbi") for c in cases),
            *(p for e in entries for p in e.get("as_light_pbis") or []),
            *as_light.get(rid, []),
        ])
        reasons = [LIGHT_WHY[mode]] if light else []
        tried = [LIGHT_TRIED[mode]] if light else []
        if left:
            reasons.append(UNRESOLVED_LIGHT_WHY)
            tried.append(UNRESOLVED_LIGHT_TRIED)
            notes = _str_list(e.get("reason") for e in entries)
            if notes:
                reasons.append("理由: " + "; ".join(notes))
        status = _str((red.get(rid) or {}).get("status"))
        states = [RED_STATES.get(status, f"Red の確認は {status}")] if status else []
        if left:
            states.append("未解決（鍵をかけていない）")
        label = mode if light else "未解決のため軽量と同じ扱い"
        items.append(make_item(
            "light_review",
            f"LR-{len(items) + 1}",
            what=f"要件 {rid}（{label}）: {_str(sc.get('text'))}",
            why="。".join(reasons),
            tried="。".join(tried),
            state="、".join(states) or "軽量のまま（見直していない）",
            options=["このままにする", "通常（normal）の受け入れテストに書き直す", "ユーザーに聞く"],
            recommendation="ユーザーに聞く（受け入れテストを決め直す）" if left else "このままにする",
            affects_pbis=pbis,
            affects_files=_targets(run, pbis),
            source={
                "record": f"run.json の scenarios の {rid}",
                "requirement": rid,
                "mode": mode,
                "unresolved": [_str(e.get("id")) for e in entries],
            },
            requirement=rid,
            mode=mode,
            unresolved=left,
            cases=case_ids,
            test_names=test_names,
        ))
    return items


E2E_TRIED = "最後の見直しの後に E2E を 1 回流した（修正の段には戻していない）"


def e2e_failure_items(run: dict[str, Any]) -> list[dict[str, Any]]:
    """A failed E2E (run["e2e"]), one item per failed command, with the command and the end of the output.

    E2E runs once, after the final review; a failure does not go back to the fix stage but waits here for triage.
    When the check did not say which command failed, one item names every E2E command.
    """
    e2e = run.get("e2e")
    if not isinstance(e2e, dict) or e2e.get("result") != "failed":
        return []
    output = _str(e2e.get("output"))
    failed = [c for c in e2e.get("failed") or [] if isinstance(c, dict) and _str(c.get("command")).strip()]
    if not failed:
        failed = [{"command": "、".join(_str_list(e2e.get("commands") or run.get("plan_e2e"))) or "（E2E のコマンド）"}]
    last_line = next((ln.strip() for ln in reversed(output.splitlines()) if ln.strip()), "")
    e2e_pbis = [p["id"] for p in run.get("pbis", []) if p.get("kind") == "e2e"]
    items: list[dict[str, Any]] = []
    for entry in failed:
        command = _str(entry.get("command")).strip()
        code = entry.get("exit_code")
        pbis = [p["id"] for p in run.get("pbis", []) if command in (p.get("checks") or [])] or e2e_pbis
        items.append(make_item(
            "e2e_failure",
            f"E2E-{len(items) + 1}",
            what=f"E2E のコマンド {command} が落ちた",
            why=last_line or "E2E が通らなかった（出力の記録なし）",
            tried=E2E_TRIED,
            state=f"失敗（終了コード {code}）" if code not in (None, "") else "失敗",
            options=["直す", "見送る（backlog に残す）"],
            recommendation="直す",
            affects_pbis=pbis,
            affects_files=_targets(run, pbis),
            source={"record": "E2E の検査", "command": command, "exit_code": code},
            command=command,
            output=output,
        ))
    return items


def split(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the items into those triage sorts and those it does not.

    Only a review item is left out, for a confidence under 80 (dropped_by 'confidence') or for being about style
    (dropped_by 'style'); the fix approval page still shows it. An item of another kind stays without a confidence.
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in items:
        if item.get("kind") not in FILTERED_KINDS:
            kept.append(item)
            continue
        conf = item.get("confidence")
        if not plan._is_int(conf) or conf < plan.MIN_CONFIDENCE:
            dropped.append({**item, "dropped_by": "confidence"})
        elif item.get("category") == "style":
            dropped.append({**item, "dropped_by": "style"})
        else:
            kept.append(item)
    return kept, dropped


def _unique_ids(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give an item whose id an earlier item has a new one (kind:id, then a number)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        iid = item["id"]
        if iid in seen:
            base = f"{item['kind']}:{iid}"
            iid, n = base, 2
            while iid in seen:
                iid, n = f"{base}-{n}", n + 1
            item = {**item, "id": iid}
        seen.add(iid)
        out.append(item)
    return out


def collect(run: dict[str, Any], findings: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The retrospective list after the final review: (the items triage sorts, the review items left out).

    The review's findings come first, then the objections, the unresolved items, the plan returns, the light reviews
    and the E2E failures.
    """
    items = [_review_item(run, f) for f in findings if isinstance(f, dict)]
    items += objection_items(run) + unresolved_items(run) + plan_return_items(run)
    items += light_review_items(run) + e2e_failure_items(run)
    return split(_unique_ids(items))


def location(root: Path, run: dict[str, Any]) -> Path:
    """Where the list is written: <git common dir>/atelier/<run>/retro.json, or the run folder outside git."""
    try:
        base = locks.outside_dir(Path(root), str(run.get("run_id") or "run"))
    except (state.StateError, OSError):
        base = state.run_dir(Path(root))
    return base / FILE_NAME


def counts(items: Iterable[dict[str, Any]]) -> dict[str, int]:
    """How many items of each kind, in the order of KINDS."""
    by_kind: dict[str, int] = {}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {k: by_kind[k] for k in KINDS if k in by_kind}


def write(root: Path, run: dict[str, Any], items: list[dict[str, Any]], dropped: list[dict[str, Any]]) -> Path:
    """Write the list outside the work tree and keep its place and counts in run["retro"]."""
    errors = validate_items([*items, *dropped])
    if errors:
        raise RetroError(" ".join(errors))
    path = location(root, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema": SCHEMA, "run": run.get("run_id"), "items": items, "dropped": dropped}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    run["retro"] = {"path": str(path.resolve()), "count": len(items), "dropped": len(dropped), "kinds": counts(items)}
    return path


def load(run: dict[str, Any]) -> dict[str, Any]:
    """The list the run wrote; a missing or broken one is refused (state.StateError)."""
    place = (run.get("retro") or {}).get("path")
    if not place:
        raise state.StateError("振り返りの一覧がまだありません。")
    try:
        body = json.loads(Path(place).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise state.StateError(f"振り返りの一覧を読めません: {place}: {exc}") from exc
    errors = [] if isinstance(body, dict) and body.get("schema") == SCHEMA else [f"schema は {SCHEMA} にしてください。"]
    errors += validate_items(body.get("items") if isinstance(body, dict) else None)
    if errors:
        raise state.StateError(f"振り返りの一覧が壊れています: {place}: " + " ".join(errors))
    return body


def load_items(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The items triage sorts."""
    return list(load(run)["items"])


def ensure(root: Path, run: dict[str, Any]) -> Path:
    """The list's path; written again from the run's kept findings when it is missing."""
    place = (run.get("retro") or {}).get("path")
    if place and Path(place).is_file():
        return Path(place)
    kept, dropped = collect(run, run.get("findings") or [])
    return write(root, run, kept, dropped)


def add_e2e_failure(root: Path, run: dict[str, Any]) -> Path:
    """Put the E2E failure (run["e2e"]) on the list the final review wrote, in place of an earlier one."""
    try:
        body = load(run)
    except state.StateError:
        # No list yet (or a broken one): write it again from the run's kept findings, the E2E failure with it.
        kept, dropped = collect(run, run.get("findings") or [])
        return write(root, run, kept, dropped)
    items = [i for i in body["items"] if i.get("kind") != "e2e_failure"] + e2e_failure_items(run)
    dropped = [d for d in body.get("dropped") or [] if isinstance(d, dict)]
    return write(root, run, _unique_ids(items), dropped)
