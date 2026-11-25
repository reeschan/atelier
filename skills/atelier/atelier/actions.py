"""Build idempotent next actions, approval pages, and role prompts for each state."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import acceptance, flow, guards, interview, locks, pages, plan, record, runners, state, survey, template

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

ASK_CHOICES: dict[str, tuple[str, list[dict[str, str]], str]] = {
    "awaiting_spec_approval": (
        "spec_approval",
        [
            {"id": "approve", "label": "仕様を承認する"},
            {"id": "revise", "label": "仕様を直す"},
        ],
        "作成された仕様を確認し、承認するか修正するかを選んでください。",
    ),
    "awaiting_plan_approval": (
        "plan_approval",
        [
            {"id": "approve", "label": "計画を承認する"},
            {"id": "revise", "label": "計画を直す"},
            {"id": "sensitive", "label": "sensitive がある"},
        ],
        "PBI の計画を確認し、承認・修正・sensitive の指定を選んでください。",
    ),
    "asking_rebuild": (
        "rebuild",
        [
            {"id": "retry", "label": "同じ経路でもう 1 回"},
            {"id": "retry_up", "label": "1 難度上の経路で"},
            {"id": "hold", "label": "保留にして、依存する PBI は続ける"},
            {"id": "hold_stop", "label": "保留にして、依存する PBI も止める"},
        ],
        "同じ持ち越しの修正の PBI を 2 回作っても検査に落ちました。次の対応を選んでください。",
    ),
    "awaiting_fix_approval": (
        "fix_approval",
        [
            {"id": "approve", "label": "直す一覧を承認する"},
            {"id": "nothing_to_fix", "label": "直すものがない（E2E へ）"},
        ],
        "最後の見直しの仕分け結果を確認し、修正へ進むかを選んでください。",
    ),
    "asking_e2e_limit": (
        "e2e_limit",
        [
            {"id": "continue", "label": "修正を 1 回だけ続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "E2E の失敗回数が上限を超えました。1 回だけ続けるか止めるかを選んでください。",
    ),
    "asking_budget": (
        "budget",
        [
            {"id": "resume", "label": "続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "予算の上限に達しそうです。続けるか止めるかを選んでください。",
    ),
    "asking_plan_broken": (
        "plan_broken",
        [
            {"id": "replan", "label": "計画を組み直す"},
            {"id": "resume", "label": "同じ PBI をやり直す"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "計画の前提が崩れました。計画を組み直すか、同じ PBI をやり直すか、止めるかを選んでください。",
    ),
    "asking_omm": (
        "omm",
        [
            {"id": "scan", "label": "全体を走査してキャッシュを作る"},
            {"id": "skip", "label": "OMM なしで調べる"},
        ],
        "この commit で使える OMM の走査がキャッシュにありません。全体を走査してキャッシュを作ってから調べるか、OMM なしで調べるかを選んでください。",
    ),
    "asking_sensitive": (
        "sensitive",
        [
            {"id": "resume", "label": "外して続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "sensitive な対象が見つかりました。外して続けるか止めるかを選んでください。",
    ),
    "asking_template_switch": (
        "template_switch",
        [
            {"id": "drop", "label": "既定のテンプレートに戻して続ける"},
            {"id": "keep", "label": "切り替えを延長して続ける"},
            {"id": "stop", "label": "止めて報告する"},
        ],
        "テンプレートの切り替え期限が過ぎました。対応を選んでください。",
    ),
}


def active_template(run: dict[str, Any]) -> dict[str, Any]:
    tmpl = run.get("active_template")
    if tmpl is not None:
        return tmpl
    loaded = template.load_template(run.get("template_path"), run.get("agents_path"))
    run["active_template"] = loaded
    run["limits"] = dict(loaded["limits"])
    run["counters"]["e2e_limit"] = loaded["limits"].get("e2e_rounds", 2)
    return loaded


def facts_file_path(run_root: Path, run: dict[str, Any]) -> Path:
    return Path(run.get("facts_file") or (state.run_dir(run_root) / "results" / "survey.json"))


def load_facts(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    path = facts_file_path(run_root, run)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def rule_docs_for(run_root: Path, run: dict[str, Any], tmpl: dict[str, Any]) -> list[str]:
    """Rule documents from the fact sheet, unless the template turns section.rule_docs off."""
    if not template.should_read(tmpl, "section.rule_docs"):
        return []
    return survey.rule_doc_paths(load_facts(run_root, run))


def check_commands(run: dict[str, Any], kind: str) -> list[str]:
    """Commands for a check: the wave's PBIs, the fixed files' PBIs, or the plan's E2E."""
    pbis = run.get("pbis", [])
    plan_checks = list(run.get("plan_checks") or [])
    if kind == "e2e":
        return list(run.get("plan_e2e") or [])
    if kind == "fixcheck":
        files = [f for group in (run.get("fix_groups") or []) for f in _group_files(group)]
        return plan.fix_commands(pbis, plan_checks, files)
    checked = [p for p in pbis if p.get("status") in ("built", "failed")] + state.recheck_held_pbis(run)
    return plan.wave_commands(checked, plan_checks)


def worker_checks(run: dict[str, Any], commands: list[str]) -> list[str]:
    """The commands a worker runs itself: the given ones without the plan's checks.

    The plan's checks are repository-wide (the whole test suite, the type check and the like); the run
    runs them once after the wave or the fix stage, so a worker does not run them again after every fix.
    """
    repository = set(run.get("plan_checks") or [])
    return [cmd for cmd in commands if cmd not in repository]


def _group_files(items: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for item in items:
        for path in [item.get("file"), *(item.get("files") or [])]:
            if isinstance(path, str) and path.strip() and path not in files:
                files.append(path)
    return files


def outside_edit_rule(run: dict[str, Any], role: str, pbi_id: str | None) -> dict[str, Any] | None:
    """What a worker may change outside its paths: existing files, except its do_not_touch (G1).

    A fix PBI may not change test files at all, so it cannot pass its checks by rewriting their expectations.
    """
    if role != "worker":
        return None
    pbi = state.get_pbi(run, pbi_id) if pbi_id else None
    if pbi is None:
        return {"blocked": [], "no_tests": False}
    return {"blocked": list(pbi.get("do_not_touch") or []), "no_tests": pbi.get("kind") == "fix"}


def allowed_paths_for(
    run: dict[str, Any], role: str, pbi_id: str | None, fix_group: int | None = None
) -> list[str] | None:
    if pbi_id:
        pbi = state.get_pbi(run, pbi_id)
        if pbi:
            files = pbi.get("allowed_paths") or pbi.get("targets") or pbi.get("files")
            if files:
                return list(files) + ["docs/pbi/"]
        return None
    if run.get("state") == "fixing" and run.get("fix_groups"):
        groups = run["fix_groups"]
        if fix_group is None:
            left = state.fix_groups_left(run)
            fix_group = left[0] if left else None
        if fix_group is not None and 0 <= fix_group < len(groups):
            group_files = _group_files(groups[fix_group])
            if group_files:
                return group_files + ["docs/pbi/"]
        return None
    if role in ("surveyor", "interviewer", "reviewer", "planner"):
        return ["docs/pbi/"]
    return None


def _git_diff_stat(run_root: Path, base_ref: str) -> str | None:
    check = subprocess.run(
        ["git", "-C", str(run_root), "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode != 0:
        return None
    res = subprocess.run(
        ["git", "-C", str(run_root), "diff", "--stat", base_ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stat = res.stdout.strip()
    return stat or None


def _write_question(run_root: Path, kind: str, body: str) -> Path:
    q_path = state.run_dir(run_root) / "questions" / f"{kind}.txt"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(body + "\n", encoding="utf-8")
    return q_path


FAILURE_LINES = 20


def _pbi_label(pbi: dict[str, Any]) -> str:
    return f"{pbi['id']}（{pbi.get('title') or pbi['id']}）"


def _rebuild_detail(run: dict[str, Any], body: str) -> tuple[str, bool]:
    """The rebuild question: which PBIs failed, which PBIs wait on them, and the end of the check output."""
    targets = state.rebuild_hold_targets(run)
    dependents = state.dependents_of(run, [p["id"] for p in targets])
    lines = [body, ""]
    if targets:
        lines.append("落ちた PBI: " + "、".join(_pbi_label(p) for p in targets))
    if dependents:
        lines += [
            "これに依る PBI（間接を含む）: " + "、".join(dependents),
            "",
            "- 依存する PBI は続ける: 落ちた PBI は変更を作業ツリーに残したまま保留にし、依存する PBI を先に作ります。"
            "依存する PBI の検査で保留した PBI の検査ももう一度流し、通れば通ったとして扱います。",
            "- 依存する PBI も止める: 依存する PBI は作らずに残し、報告に未完了として載せます。",
        ]
    else:
        lines.append("これに依る PBI はありません。保留にすると、変更は作業ツリーに残したまま次へ進みます。")
    failure = str(run.get("last_check_failure") or "").strip()
    if failure:
        tail = failure.splitlines()[-FAILURE_LINES:]
        lines += ["", f"検査の出力（最後の {len(tail)} 行）:", "```", *tail, "```"]
    return "\n".join(lines), bool(dependents)


CARRY_STATUS_LABELS = {
    "open": "残っている",
    "fixed_by_pbi": "修正の PBI で直った",
    "fixed": "修正の段で直った",
    "dropped": "直す一覧から外した",
    "replanned": "計画の組み直しに含めた",
}


def _unfinished_lines(run: dict[str, Any]) -> list[str]:
    """Report lines for PBIs that did not pass, and for held PBIs that dependents went on without."""
    lines: list[str] = []
    unfinished = state.unfinished_pbis(run)
    if unfinished:
        lines += ["", "## 終わっていない PBI", ""]
        for pbi in unfinished:
            status = pbi.get("status", "pending")
            text = f"- {_pbi_label(pbi)}: {state.STATUS_LABELS.get(status, status)}"
            waiting = state.unmet_deps(run, pbi)
            if status == "pending" and waiting:
                text += f"（{'、'.join(waiting)} を待っていて始まっていない）"
            elif status == "held" and pbi.get("held_reason") == "sensitive_dependency":
                text += "（sensitive で外した PBI に依る）"
            elif status == "held" and pbi.get("held_reason") == "stale_dependency":
                text += "（計画の変更で前提が変わった）"
            lines.append(text)
    went_on = sorted({dep for p in run.get("pbis", []) for dep in p.get("waived_deps") or []})
    if went_on:
        lines += ["", "## 依存を免除して先へ進めた PBI", ""]
        for dep in went_on:
            users = [p["id"] for p in run.get("pbis", []) if dep in (p.get("waived_deps") or [])]
            lines.append(f"- {dep} を待たずに: {'、'.join(users)}")
    restored = run.get("eol_restored") or []
    if restored:
        lines += ["", "## 改行を LF に戻したファイル", "", *[f"- {path}" for path in restored]]
    carry_overs = run.get("carry_overs") or []
    if carry_overs:
        lines += ["", "## 持ち越し", ""]
        for co in carry_overs:
            commands = "、".join(co.get("commands") or []) or "（コマンド不明）"
            label = CARRY_STATUS_LABELS.get(co.get("status", "open"), co.get("status", ""))
            lines.append(f"- {co['id']}（{'、'.join(co.get('pbis') or [])}）: {commands} — {label}")
    passed_notes = [(p, n) for p in run.get("pbis", []) if p.get("status") == "passed" for n in p.get("worker_carry_over") or []]
    if passed_notes:
        lines += ["", "## 作業者が直せなかったと返したが、wave の検査は通ったもの", ""]
        lines += [f"- {p['id']}: {n.get('command', '')} — {n.get('why') or '（理由の記載なし）'}" for p, n in passed_notes]
    edits = run.get("outside_edits") or []
    if edits:
        lines += ["", "## 対象の外で変えたファイル", ""]
        lines += [f"- {e['by']}: {e['file']} — {e['why'] or '（理由の記載なし）'}" for e in edits]
    lines += acceptance.unresolved_lines(run)
    lines += _worker_note_lines(run)
    batches = run.get("batches") or []
    if batches:
        lines += ["", "## 一斉起動（同時に動いた起動）", ""]
        lines += [f"- {b['id']}: {'、'.join(b['members'])}" for b in batches]
    return lines


def lock_ticket_fields(run: dict[str, Any], pbi: dict[str, Any]) -> dict[str, Any]:
    """What the ticket of an implementation PBI carries once the acceptance tests are locked.

    Every build, e2e or fix PBI gets the locked files (the worker must not change them). A build or e2e PBI also gets
    the ids of the cases assigned to it and the files marked as unit tests it may write; a fix PBI gets the cases of
    the PBIs its carry-over holds, when there are any (the wave check judges it on them). Nothing before the lock.
    """
    kept = run.get("lock")
    kind = pbi.get("kind") or plan.DEFAULT_KIND
    if not run.get("acceptance_first") or not kept or kind in acceptance.STAGE_KINDS:
        return {}
    fields: dict[str, Any] = {"locked_files": list(kept.get("files") or [])}
    if kind in ("build", "e2e", "fix"):
        # A fix PBI is judged on the cases of the PBIs its carry-over holds.
        owners = {pbi["id"]}
        if kind == "fix":
            co = state.carry_over_of(run, pbi.get("carry_over") or "") or {}
            owners = set(co.get("pbis") or [])
        locked = set(locks.locked_requirements(run))
        cases = [
            str(case["id"])
            for sc in acceptance.scenarios(run)
            if sc["id"] in locked
            for case in sc.get("cases") or []
            if isinstance(case, dict) and case.get("id") and case.get("pbi") in owners
        ]
        if kind != "fix" or cases:
            fields["cases"] = cases
    if kind in ("build", "e2e"):
        fields["unit_tests"] = list(pbi.get("unit_tests") or [])
    return fields


def _worker_note_lines(run: dict[str, Any]) -> list[str]:
    """The report's lines for the objections and stops workers returned, per PBI."""
    lines: list[str] = []
    objections = [(p["id"], o) for p in run.get("pbis", []) for o in p.get("objections") or []]
    if objections:
        lines += ["", "## 作業者の異議", ""]
        for pid, o in objections:
            lines.append(
                f"- {pid} {o.get('case', '')}（{'Green' if o.get('state') == 'green' else '赤のまま'}）: "
                f"{o.get('what', '')} — {o.get('why', '')} / 仮の解釈: {o.get('interpretation', '')}"
            )
    stops = [(p["id"], p["stop"]) for p in run.get("pbis", []) if p.get("stop")]
    if stops:
        lines += ["", "## 作業者の stop", ""]
        for pid, s in stops:
            label = state.STOP_EXCEPTION_LABELS.get(s.get("exception"), "")
            lines.append(f"- {pid}: 例外 {s.get('exception')}（{label}） — {s.get('reason', '')}")
    return lines


def _write_report(run_root: Path, run: dict[str, Any]) -> Path:
    report_path = state.run_dir(run_root) / "REPORT.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_md, _ = record.report(state.record_path(run_root))
    unfinished = state.unfinished_pbis(run)
    status = f"未完了（終わっていない PBI が {len(unfinished)} 件ある）" if unfinished else run["state"]
    header = [
        f"# Atelier v8 報告 ({run['run_id']})",
        "",
        f"- 状態: {status}",
        f"- PBI 数: {len(run.get('pbis', []))}",
        f"- 持ち越し: {len(run.get('carry_overs') or [])} 件（残り {len(state.open_carry_overs(run))} 件）",
        f"- 見直し回数: {run['counters']['review_count']}",
        f"- E2E 失敗回数: {run['counters']['e2e_failures']}",
    ]
    omm = run.get("omm") or {}
    if omm.get("source") == "none" and omm.get("reason") == "tools":
        header.append("- OMM: 道具（node・omm・omm-pr-scan v1.2）が無いため、OMM なしで調べた")
    elif omm.get("source") == "none" and omm.get("reason") == "committed-failed":
        header.append("- OMM: コミット済みの OMM の写しを用意できなかったため、OMM なしで調べた")
    elif omm.get("source") == "committed":
        short = str(omm.get("commit") or "")[:7]
        count = omm.get("count") or 0
        if count:
            header.append(f"- OMM: committed ({short} から {count} ファイル変更)")
        else:
            header.append("- OMM: committed (HEAD に追いついている)")
    elif omm.get("source"):
        header.append(f"- OMM: {omm['source']}")
    if run.get("e2e_skipped"):
        header.append("- E2E: E2E のコマンドが無いため流していない")
    if run.get("acceptance_first") is not None:
        header.append(f"- 部品「受け入れを先に置く」: {pages.acceptance_toggle_text(run.get('acceptance_first'), run.get('acceptance_reason'))}")
    header += acceptance.report_lines(run)
    header += _unfinished_lines(run)
    header.append("")
    report_path.write_text("\n".join(header) + summary_md, encoding="utf-8")
    return report_path


def _git_branch(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    branch = proc.stdout.strip()
    return branch if proc.returncode == 0 and branch and branch != "HEAD" else None


def _write_handoff(run_root: Path, run: dict[str, Any], report_path: Path) -> Path:
    """Write the short text a fresh conversation needs to take the finished run to a pull request."""
    root = run_root.resolve()
    base = run.get("base_commit")
    lines = [
        f"# 引き継ぎ: Atelier の run {run['run_id']} のあと",
        "",
        "次の段は仕上げと PR。この文だけで新しい会話を始められる。",
        "",
        f"- 作業の場所: {root}",
        f"- ブランチ: {_git_branch(root) or '（git のブランチなし）'}",
        f"- 始めたときのコミット: {base or '（なし）'}",
        f"- 目的: {run['goal_file']}",
        f"- 仕様: {run.get('spec_file') or '（なし）'}",
        f"- 報告: {report_path.resolve()}",
        f"- run の状態: {state.state_path(run_root).resolve()}",
    ]
    unfinished = [p["id"] for p in state.unfinished_pbis(run)]
    if unfinished:
        lines.append(f"- 終わっていない PBI: {'、'.join(unfinished)}（報告の「終わっていない PBI」に理由がある）")
    lines += [
        "",
        "次にやること:",
        "1. 報告を読み、終わっていない PBI・残った指摘・E2E の結果を確かめる。",
        f"2. 差分を見る（`git diff {base}`）。" if base else "2. 差分を見る（`git diff`）。",
        "3. 手直しが要れば直し、リポジトリの PR の手順（PR のスキルがあればそれ）で PR を作る。",
        "",
    ]
    handoff_path = state.run_dir(run_root) / "HANDOFF.md"
    handoff_path.write_text("\n".join(lines), encoding="utf-8")
    return handoff_path


def _budget_would_exceed(run: dict[str, Any], prompt_text: str | list[str]) -> bool:
    """Whether the next action's launches would go past the budget; one approval covers the whole next action."""
    if run.pop("budget_approved_next", False):
        return False
    limits = run.get("limits") or active_template(run).get("limits", {})
    budget_tokens = limits.get("budget_tokens")
    budget_usd = limits.get("budget_usd")
    if budget_tokens is None and budget_usd is None:
        return False
    usage = run.get("usage") or {}
    recorded_tokens = usage.get("tokens", 0)
    recorded_cost = usage.get("cost_usd", 0.0)
    texts = [prompt_text] if isinstance(prompt_text, str) else list(prompt_text)
    est_tokens = sum(max(500, len(text) // 4 + 500) for text in texts)
    est_cost = round(est_tokens * 0.00001, 6)
    if budget_tokens is not None and (recorded_tokens + est_tokens) > budget_tokens:
        return True
    if budget_usd is not None and (recorded_cost + est_cost) > budget_usd:
        return True
    return False


def _step_for_launch(run: dict[str, Any], tmpl: dict[str, Any], role: str, pbi_id: str | None) -> dict[str, Any]:
    s = run["state"]
    if s in ("building", "building_acceptance") and pbi_id:
        pbi = state.get_pbi(run, pbi_id)
        assert pbi is not None
        if not pbi.get("route"):
            choice = template.choose_route(tmpl, pbi)
            pbi["route"] = choice["route"]
            pbi["route_rule"] = choice["rule"]
            pbi["route_inputs"] = choice["inputs"]
            pbi["route_template"] = choice["template"]
            pbi["route_index"] = 0
        idx = pbi.get("route_index", 0)
        if idx >= len(pbi["route"]):
            raise state.StateError(f"route exhausted for {pbi_id}")
        step = dict(pbi["route"][idx])
        pbi["last_step"] = step
        return step
    if s == "fixing":
        if "worker_fix" in tmpl.get("roles", {}):
            step = template.role_for(tmpl, "worker_fix")
        else:
            choice = template.choose_route(tmpl, {"level": "L2", "level_reason": "指摘またはE2Eの修正"})
            step = dict(choice["route"][0])
        run["last_fix_model"] = step.get("model", "")
        return step
    return template.role_for(tmpl, role)


def _append_guard_check(
    run_root: Path,
    run: dict[str, Any],
    *,
    pbi_id: str | None,
    rules: list[str],
    model: str,
    level: str,
) -> None:
    event = {
        "type": "check",
        "pbi": pbi_id,
        "rules": list(rules),
        "stage": "guard",
        "model": model,
        "level": level,
        "result": "fail" if rules else "pass",
        "read_launch_ids": [],
        "first_pass": False,
    }
    line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if not guards.scan_secrets(line):
        record.append(state.record_path(run_root), event)


def _strip_worker_rule_line(text: str, rule_id: str) -> str:
    pattern = re.compile(rf"^\s*-\s*`?\[{re.escape(rule_id)}\]`?.*(?:\r?\n)?", re.MULTILINE)
    return pattern.sub("", text)


def _load_facts_map(facts_path: Path) -> dict[str, str]:
    if not facts_path.is_file():
        return {}
    try:
        data = json.loads(facts_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}
    mapping: dict[str, str] = {}
    for field in survey.FIELDS:
        for item in data.get(field) or []:
            if isinstance(item, dict) and item.get("id"):
                mapping[item["id"]] = f"{item.get('text', '')} ({item.get('source', '')})"
    return mapping


def _compose_prompt_parts(
    run_root: Path,
    run: dict[str, Any],
    tmpl: dict[str, Any],
    role: str,
    pbi_id: str | None,
    result_path: Path,
    stage_dir: Path,
    *,
    fix_group: int | None = None,
    alongside: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, int], list[Path]]:
    """Build staged prompt text, filtered token read map, and referenced files.

    alongside lists the other launches of the same launch batch and the files they change, for the worker's ticket.
    """
    s = run["state"]
    facts_path = facts_file_path(run_root, run)
    spec_path = Path(run.get("spec_file") or run["goal_file"])
    referenced: list[Path] = []
    rule_docs = rule_docs_for(run_root, run, tmpl) if s != "surveying" else []
    rule_docs_text = "\n".join(rule_docs)

    if s == "surveying":
        omm = run.get("omm") or survey.omm_context(run_root, run.get("omm_cache_dir"))
        survey_path = survey.survey_prompt(run_root, run["goal_file"], run.get("spec_file"), omm)
        prompt_text = survey_path.read_text(encoding="utf-8")
        surveyor_md = (_PROMPTS_DIR / "surveyor.md").read_text(encoding="utf-8")
        parts_by_id = {
            "role.surveyor": surveyor_md,
            "section.survey_context": prompt_text[len(surveyor_md):] if prompt_text.startswith(surveyor_md) else prompt_text,
        }
        if Path(run["goal_file"]).is_file():
            referenced.append(Path(run["goal_file"]))
        if run.get("spec_file") and Path(run["spec_file"]).is_file():
            referenced.append(Path(run["spec_file"]))
        kept = record.filter_parts(tmpl, parts_by_id)
        return prompt_text, record.measure_read(kept), referenced

    if s == "interviewing":
        before_tree = interview.load_tree(run_root)
        run["pending_tree_snapshot"] = before_tree
        round_num = run["counters"]["round"] + 1
        staged_facts: Path | None = None
        if not template.should_read(tmpl, "section.facts"):
            staged_facts = stage_dir / "empty_facts.json"
            staged_facts.write_text("{}", encoding="utf-8")
        staged_prompt = stage_dir / "interview.md"
        interview.interviewer_prompt(
            run_root,
            round_num,
            facts_path=staged_facts or facts_path,
            result_path=result_path,
            out_path=staged_prompt,
        )
        prompt_text = staged_prompt.read_text(encoding="utf-8")
        interviewer_md = (_PROMPTS_DIR / "interviewer.md").read_text(encoding="utf-8")
        facts_text = facts_path.read_text(encoding="utf-8-sig") if (facts_path.is_file() and staged_facts is None) else ""
        tree_text = json.dumps(before_tree, ensure_ascii=False, indent=2)
        parts_by_id = {
            "role.interviewer": interviewer_md,
            "section.facts": facts_text,
            "section.tree": tree_text,
        }
        if staged_facts is None and facts_path.is_file():
            referenced.append(facts_path)
        kept = record.filter_parts(tmpl, parts_by_id)
        return prompt_text, record.measure_read(kept), referenced

    if s in ("planning", "triaging", "reviewing_acceptance"):
        staged_prompt = stage_dir / f"planner-{s}.md"
        mode = {"planning": run.get("planner_mode", "plan"), "triaging": "triage"}.get(s, "acceptance_review")
        plan_file = state.run_dir(run_root) / "plan.json"
        findings_path = state.run_dir(run_root) / "results" / "findings_kept.json"
        if s == "triaging" and not findings_path.is_file():
            findings_path.parent.mkdir(parents=True, exist_ok=True)
            findings_path.write_text(
                json.dumps({"schema": plan.SCHEMA_FINDINGS, "findings": run.get("findings", [])}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        plan.planner_prompt(
            run_root,
            mode,
            spec_path=spec_path,
            facts_path=facts_path,
            result_path=result_path,
            plan_path=plan_file if mode == "revise" and plan_file.is_file() else None,
            request_text=run.get("plan_revision_request", "") if mode == "revise" else None,
            findings_path=findings_path if mode == "triage" else None,
            levels=template.load_levels() if mode in ("plan", "revise") else None,
            out_path=staged_prompt,
            rule_docs=rule_docs,
            passed_pbis=[p["id"] for p in run.get("pbis", []) if p.get("status") == "passed"],
            acceptance_first=bool(run.get("acceptance_first")),
            scenarios=acceptance.review_scenarios(run) if mode == "acceptance_review" else None,
            interfaces=[dict(i) for i in run.get("interfaces") or []] if mode == "acceptance_review" else None,
            acceptance_files=acceptance.review_files(run) if mode == "acceptance_review" else None,
        )
        prompt_text = staged_prompt.read_text(encoding="utf-8")
        planner_md = (_PROMPTS_DIR / "planner.md").read_text(encoding="utf-8")
        parts_by_id = {
            "role.planner": planner_md,
            "section.spec": spec_path.read_text(encoding="utf-8-sig") if spec_path.is_file() else "",
            "section.facts": facts_path.read_text(encoding="utf-8-sig") if facts_path.is_file() else "",
            "section.rule_docs": rule_docs_text,
        }
        if mode == "revise":
            parts_by_id["section.request"] = run.get("plan_revision_request", "")
            if plan_file.is_file():
                parts_by_id["section.plan"] = plan_file.read_text(encoding="utf-8-sig")
                referenced.append(plan_file)
        if mode == "triage" and findings_path.is_file():
            parts_by_id["section.findings"] = findings_path.read_text(encoding="utf-8-sig")
            referenced.append(findings_path)
        for p in (spec_path, facts_path):
            if p.is_file():
                referenced.append(p)
        if mode == "acceptance_review":
            # The planner reads these test files, so the G4 scan before an external send covers them too.
            for entry in acceptance.review_files(run):
                test_file = Path(run_root) / str(entry["file"])
                if test_file.is_file():
                    referenced.append(test_file)
        kept = record.filter_parts(tmpl, parts_by_id)
        return prompt_text, record.measure_read(kept), referenced

    if s in ("building", "building_acceptance", "fixing"):
        staged_prompt = stage_dir / f"worker-{s}.md"
        if s != "fixing":
            mode = "pbi"
            pbi = state.get_pbi(run, pbi_id or "") or {}
            ticket = {
                k: pbi[k]
                for k in (
                    "id",
                    "title",
                    "goal",
                    "level",
                    "level_reason",
                    "depends_on",
                    "targets",
                    "do_not_touch",
                    "checks",
                    "facts",
                    "acceptance",
                )
                if k in pbi
            }
            if "targets" not in ticket and pbi.get("files"):
                ticket["targets"] = list(pbi["files"])
            if pbi.get("data_needs"):
                ticket["data"] = [{"path": need["dest"], "why": need["why"]} for need in pbi["data_needs"]]
            if pbi.get("kind") == "fix":
                ticket["carry_over"] = state.carry_over_ticket(run, pbi)
            ticket["checks"] = worker_checks(run, pbi.get("checks") or [])
            ticket["repository_checks"] = list(run.get("plan_checks") or [])
            ticket.update(acceptance.ticket_fields(run, pbi))
            ticket.update(lock_ticket_fields(run, pbi))
            if alongside:
                ticket["running_alongside"] = alongside
        else:
            mode = "fix"
            # The fix stage's check and E2E run once after every fix; the worker runs only what its fix touches.
            after_fix = check_commands(run, "fixcheck") + list(run.get("plan_e2e") or [])
            groups = run.get("fix_groups") or []
            idx = fix_group if fix_group is not None else (state.fix_groups_left(run) or [-1])[0]
            if groups and 0 <= idx < len(groups):
                group = groups[idx]
                ticket = {
                    "id": f"FIX-{idx + 1}",
                    "items": group,
                    "files": _group_files(group),
                    "checks": worker_checks(run, plan.related_commands(run.get("pbis", []), _group_files(group))),
                    "repository_checks": after_fix,
                }
                if alongside:
                    ticket["running_alongside"] = alongside
            else:
                ticket = {
                    "id": "FIX-E2E",
                    "failure": run.get("last_e2e_failure") or run.get("last_check_failure") or "",
                    "checks": [],
                    "repository_checks": after_fix,
                }
            if run.get("acceptance_first") and run.get("lock"):
                # The fix stage's worker must not change the locked files either (G1 puts them back).
                ticket["locked_files"] = list(run["lock"].get("files") or [])

        plan.worker_prompt(
            run_root,
            mode=mode,
            ticket=ticket,
            result_path=result_path,
            out_path=staged_prompt,
            rule_docs=rule_docs,
        )
        prompt_text = staged_prompt.read_text(encoding="utf-8")
        worker_md = (_PROMPTS_DIR / "worker.md").read_text(encoding="utf-8")
        g12_line = next((ln for ln in worker_md.splitlines() if "[G12]" in ln), "")
        g13_line = next((ln for ln in worker_md.splitlines() if "[G13]" in ln), "")
        base_worker_md = _strip_worker_rule_line(_strip_worker_rule_line(worker_md, "G12"), "G13")
        parts_by_id: dict[str, str] = {
            "role.worker": base_worker_md,
            "G12": g12_line,
            "G13": g13_line,
            "section.ticket": json.dumps(ticket, ensure_ascii=False, indent=2),
            "section.rule_docs": rule_docs_text,
        }
        if mode == "pbi":
            facts_map = _load_facts_map(facts_path)
            for fid in ticket.get("facts") or []:
                parts_by_id[str(fid)] = facts_map.get(str(fid), str(fid))

        kept = record.filter_parts(tmpl, parts_by_id)
        if "G12" not in kept:
            prompt_text = _strip_worker_rule_line(prompt_text, "G12")
        if "G13" not in kept:
            prompt_text = _strip_worker_rule_line(prompt_text, "G13")
        return prompt_text, record.measure_read(kept), referenced

    if s == "reviewing":
        staged_prompt = stage_dir / "review.md"
        base_ref = run.get("base_commit") or "HEAD"
        diff_stat = _git_diff_stat(run_root, base_ref)
        plan.reviewer_prompt(
            run_root,
            spec_path=spec_path,
            result_path=result_path,
            base_ref=base_ref,
            diff_stat=diff_stat,
            out_path=staged_prompt,
            rule_docs=rule_docs,
        )
        prompt_text = staged_prompt.read_text(encoding="utf-8")
        reviewer_md = (_PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")
        parts_by_id = {
            "role.reviewer": reviewer_md,
            "section.spec": spec_path.read_text(encoding="utf-8-sig") if spec_path.is_file() else "",
            "section.diff_stat": diff_stat or "",
            "section.rule_docs": rule_docs_text,
        }
        if spec_path.is_file():
            referenced.append(spec_path)
        kept = record.filter_parts(tmpl, parts_by_id)
        return prompt_text, record.measure_read(kept), referenced

    target = f" for {pbi_id}" if pbi_id else ""
    prompt_text = f"# Role: {role}{target}\n\nFollow the instructions for {role}{target}.\n"
    parts_by_id = {f"role.{role}": prompt_text}
    kept = record.filter_parts(tmpl, parts_by_id)
    return prompt_text, record.measure_read(kept), referenced


def _forbidden_args_for_launch(
    run: dict[str, Any],
    role: str,
    step: dict[str, Any],
    spec: Any,
) -> list[str]:
    if not isinstance(spec, list):
        return []
    components = run.get("components") or {}
    blocked: list[str] = []
    for comp in (components.get(step.get("runner")), components.get(role), step):
        if isinstance(comp, dict) and "forbidden_args" in comp:
            for item in guards.forbidden_args(comp, list(spec)):
                if item not in blocked:
                    blocked.append(item)
    return blocked


def _make_launch(
    run_root: Path,
    run: dict[str, Any],
    role: str,
    *,
    step: dict[str, Any],
    prompt_path: Path,
    result_path: Path,
    pbi_id: str | None = None,
    launch_id: str,
    fix_group: int | None = None,
) -> dict[str, Any]:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    spec = runners.launch_spec(role, step, prompt_path, result_path, run_root, agents_path=run.get("agents_path"))
    extra = {"fix_group": fix_group} if fix_group is not None else {}
    if step["runner"] == "agent":
        return {
            "action": "launch",
            **spec,
            "pbi": pbi_id,
            "launch_id": launch_id,
            **extra,
        }
    return {
        "action": "launch",
        "role": role,
        "runner": step["runner"],
        "model": step.get("model", ""),
        "effort": step.get("effort"),
        "prompt": str(prompt_path.resolve()),
        "result": str(result_path.resolve()),
        "command": list(spec),
        "pbi": pbi_id,
        "launch_id": launch_id,
        **extra,
    }


def _raise_prelaunch_guard(
    run_root: Path,
    run: dict[str, Any],
    *,
    rule: str,
    role: str,
    pbi_id: str | None,
    runner: str,
    model: str,
    level: str,
    violations: list[dict[str, str]],
) -> dict[str, Any]:
    _append_guard_check(run_root, run, pbi_id=pbi_id, rules=[rule], model=model, level=level)
    run["guard_issue"] = {
        "launch_id": None,
        "rule": rule,
        "role": role,
        "pbi": pbi_id,
        "runner": runner,
        "violations": violations,
        "detail": "; ".join(f"{v['rule']}: {v['detail']}" for v in violations),
    }
    flow.apply_event(run, "guard_violation")
    return ensure_pending_action(run_root, run)


def _make_check(run_root: Path, run: dict[str, Any], kind: str) -> dict[str, Any]:
    commands = check_commands(run, kind)
    if not commands:
        raise state.StateError(f"no check commands for {kind}: the plan and its PBIs name none")
    return _check_action(run_root, run, kind, commands)


def _check_action(run_root: Path, run: dict[str, Any], prefix: str, commands: list[str], **extra: Any) -> dict[str, Any]:
    """A check action running commands, unless the check component forbids one of their arguments (G11)."""
    comp = (run.get("components") or {}).get("check")
    if isinstance(comp, dict) and "forbidden_args" in comp:
        blocked = [arg for cmd in commands for arg in guards.forbidden_args(comp, cmd.split())]
        if blocked:
            return _raise_prelaunch_guard(
                run_root,
                run,
                rule="G11",
                role="check",
                pbi_id=None,
                runner="check",
                model="script",
                level="check",
                violations=[{"rule": "G11", "detail": f"forbidden args in check: {', '.join(blocked)}"}],
            )
    launch_id = state.next_launch_id(run, prefix)
    result_path = state.run_dir(run_root) / "results" / f"{launch_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "action": "check",
        "commands": commands,
        "result": str(result_path.resolve()),
        "launch_id": launch_id,
        **extra,
    }


def append_red_records(run_root: Path, run: dict[str, Any], verdicts: list[dict[str, Any]]) -> None:
    """Write the Red check's verdicts to the run record, each acceptance PBI with its reasons.

    Every event is scanned (G4) before any is written, so a refusal leaves none behind for a retry to write twice;
    the first-pass marks stay on the run only when all of them pass.
    """
    marks = list(run.get("first_pass_checked_pbis") or [])
    events = acceptance.record_events(run, verdicts)
    for event in events:
        line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        hits = guards.scan_secrets(line)
        if hits:
            run["first_pass_checked_pbis"] = marks
            raise state.StateError(f"G4: secret detected in Red check record ({hits[0]['kind']})")
    for event in events:
        record.append(state.record_path(run_root), event)


def _make_red_check(run_root: Path, run: dict[str, Any]) -> dict[str, Any] | None:
    """The Red check: each requirement's acceptance test file run with the run's acceptance command.

    Each command writes JUnit XML under docs/pbi/results/<check id>/, which done reads. When no requirement can
    be run (none left, or none has a file), the verdicts are applied here and None is returned.
    """
    check_id = f"red-{run['counters']['launch_seq'] + 1:04d}"
    results_dir = f"{state.run_dir(Path('.')).as_posix()}/results/{check_id}"
    groups, unrun = acceptance.red_check_plan(run, results_dir)
    if not groups:
        append_red_records(run_root, run, unrun)
        flow.apply_event(run, acceptance.settle_red(run, unrun, None))
        return None
    (run_root / results_dir).mkdir(parents=True, exist_ok=True)
    for group in groups:
        stale = run_root / group["junit"]
        if stale.exists():
            stale.unlink()
    return _check_action(run_root, run, "red", [g["command"] for g in groups], stage="red", red=groups, red_unrun=unrun)


def locked_wave(run: dict[str, Any]) -> bool:
    """True when the wave check goes by the lock: the run puts acceptance first and its tests are locked."""
    return bool(run.get("acceptance_first")) and bool(run.get("lock"))


def wave_pbis(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The PBIs a wave check judges: the built and failed ones, and the held ones checked again with them."""
    return [p for p in run.get("pbis", []) if p.get("status") in ("built", "failed")] + state.recheck_held_pbis(run)


LOCK_COPY_STOP = "鍵の仕組みの故障なので wave の検査を止めた（作業者への差し戻しにはしない）: "


def _settle_wave_lock(run_root: Path, run: dict[str, Any], pbis: list[dict[str, Any]], check_id: str) -> None:
    """The wave check's step (1): put the locked files and keys that differ from the lock back, and keep whose
    failure each difference is (the PBIs of the wave with its path in their targets) or leave it unresolved."""
    changes = locks.wave_changes(run_root, run)
    if not changes:
        return
    files = locks.restore(run_root, run, locks.violated(changes))
    keys, failed = locks.restore_keys(run_root, run, changes)
    entry: dict[str, Any] = {"check": check_id, "files": files}
    if keys:
        entry["keys"] = keys
    if failed:
        entry["keys_not_restored"] = failed
    sigs = locks.changed_signatures(changes)
    if sigs:
        entry["signatures"] = sigs
    run.setdefault("lock_restored", []).append(entry)
    owned, unowned = locks.change_owners(changes, pbis)
    kept = run.setdefault("wave_check", {}).setdefault("lock_failures", {})
    for pid, details in owned.items():
        kept.setdefault(pid, [])
        kept[pid] += [d for d in details if d not in kept[pid]]
    if unowned:
        acceptance.add_unresolved(
            run, acceptance.WAVE_LOCK_UNRESOLVED, "; ".join(unowned), check=check_id, changes=unowned, restored=[*files, *keys]
        )


def _make_wave_check(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    """The check at the end of a wave.

    Without a lock it runs the wave's PBIs' checks and the plan's checks, as before. In a locked run it goes in order:
    (0) the copy of the lock list outside the work tree against its recorded sha256 (a difference stops the check and
    asks the user; no PBI is carried over), (1) the locked files, keys and signatures against the lock (put back, and
    laid on the PBI that has the path in its targets, or left unresolved), then (2) and (3) in one check: the locked
    cases with the acceptance command and JUnit XML, then the PBIs' checks and the plan's checks.
    """
    if not locked_wave(run):
        return _make_check(run_root, run, "check")
    problem = locks.copy_problem(run_root, run)
    if problem:
        return _raise_prelaunch_guard(
            run_root,
            run,
            rule=locks.COPY_RULE,
            role="check",
            pbi_id=None,
            runner="check",
            model="script",
            level="check",
            violations=[{"rule": locks.COPY_RULE, "detail": LOCK_COPY_STOP + problem}],
        )
    check_id = f"check-{run['counters']['launch_seq'] + 1:04d}"
    pbis = wave_pbis(run)
    _settle_wave_lock(run_root, run, pbis, check_id)
    listed = locks.locked_list(run_root, run)
    results_dir = f"{state.run_dir(Path('.')).as_posix()}/results/{check_id}"
    groups, unrun = acceptance.wave_case_plan(run, [str(r) for r in listed.get("requirements") or []], results_dir)
    commands = [g["command"] for g in groups]
    commands += [c for c in plan.wave_commands(pbis, run.get("plan_checks") or []) if c not in commands]
    if not commands:
        raise state.StateError("no check commands for check: the plan, its PBIs and the acceptance tests name none")
    (run_root / results_dir).mkdir(parents=True, exist_ok=True)
    for group in groups:
        stale = run_root / group["junit"]
        if stale.exists():
            stale.unlink()
    wave = {
        "groups": groups,
        "unrun": unrun,
        "cases": [str(c) for c in listed.get("cases") or []],
        "assignments": {str(k): str(v) for k, v in (listed.get("assignments") or {}).items()},
        "pbis": [p["id"] for p in pbis],
    }
    return _check_action(run_root, run, "check", commands, stage="wave", wave=wave)


def judge_wave(run_root: Path, run: dict[str, Any], pending: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """A locked run's wave check result, judged on the cases and the lock as well as on the commands.

    The acceptance commands' exit codes are left out (the cases of PBIs not built yet are meant to be red). The
    missing and failed cases, and the lock's differences found before the check, fail the PBIs they are laid on
    (case_failed_pbis); missing cases with no PBI of the wave to hold them fail every non-fix PBI of the wave, or are
    left unresolved when there is none. The other commands, the PBIs' checks and the plan's, stay in "commands" to be
    judged as before. The output starts with the reasons.
    """
    wave = pending.get("wave") or {}
    groups = list(wave.get("groups") or [])
    acceptance_commands = {g["command"] for g in groups}
    commands = [c for c in result.get("commands") or [] if isinstance(c, dict)]
    rest = [c for c in commands if str(c.get("command")) not in acceptance_commands]
    wave_ids = [str(p) for p in wave.get("pbis") or []]
    cases = [str(c) for c in wave.get("cases") or []]
    verdict = acceptance.judge_wave_cases(run_root, run, groups, cases, dict(wave.get("assignments") or {}), wave_ids)
    kept = run.pop("wave_check", None) or {}
    lock_failures = kept.get("lock_failures") or {}
    by_pbi: dict[str, list[str]] = {}
    for pid, details in lock_failures.items():
        by_pbi.setdefault(pid, []).extend(f"鍵と違った: {d}" for d in details)
    for pid, reasons in verdict["by_pbi"].items():
        by_pbi.setdefault(pid, []).extend(reasons)
    check_id = str(pending.get("launch_id") or "")
    case_failed = bool(verdict["by_pbi"])
    if verdict["wave_reasons"]:
        builders = [pid for pid in wave_ids if (state.get_pbi(run, pid) or {}).get("kind") != "fix"]
        for pid in builders:
            by_pbi.setdefault(pid, []).extend(verdict["wave_reasons"])
        case_failed = case_failed or bool(builders)
        if not builders:
            acceptance.add_unresolved(
                run, acceptance.WAVE_CASES_UNRESOLVED, "; ".join(verdict["wave_reasons"]), check=check_id, cases=list(verdict["missing"])
            )
    failing = [c for c in rest if c.get("exit_code")]
    rules: list[str] = []
    if lock_failures:
        rules.append(locks.WAVE_RULE)
    if case_failed:
        rules.append(acceptance.WAVE_CASES_RULE)
    if failing:
        rules.append("G9")
    lines: list[str] = []
    if by_pbi:
        lines.append("wave の検査（受け入れのケースと鍵）で落ちた PBI:")
        lines += [f"- {pid}: {reason}" for pid, reasons in by_pbi.items() for reason in reasons]
    unrun = [*wave.get("unrun", []), *verdict["notes"]]
    if unrun:
        lines.append("流せなかった受け入れテスト:")
        lines += [f"- {note}" for note in unrun]
    output = "\n".join([*lines, str(result.get("output") or "")]).strip()
    exit_code = next((c["exit_code"] for c in failing), 1 if by_pbi else 0)
    run.setdefault("wave_cases", []).append({
        "check": check_id,
        "total": len(cases),
        "ran": list(verdict["ran"]),
        "missing": list(verdict["missing"]),
        "failed": dict(verdict["failed"]),
        "excused": list(verdict["excused"]),
        "sent_back": sorted(by_pbi),
    })
    return {
        **result,
        "exit_code": exit_code,
        "output": output,
        "commands": rest,
        "case_failed_pbis": sorted(by_pbi),
        "rules": rules if exit_code else [],
    }


REVIEW_SKILL_INSTRUCTIONS = (
    "Run the skill with the Skill tool on the diff from base to the working tree, with args. "
    "Write its findings to result as atelier.findings/1: id, file, line, severity (critical|major|minor), "
    "confidence (0-100), kind (correctness|spec|rule|style), why, fix; rule (where the rule is written) for kind rule. "
    "When the skill gives no confidence, use 90 for a finding you verified in the code and 50 for one you could not. "
    "Then run conductor.py done."
)


def _make_caller_action(run: dict[str, Any], run_root: Path, action: str, prefix: str, **fields: Any) -> dict[str, Any]:
    launch_id = state.next_launch_id(run, prefix)
    result_path = state.run_dir(run_root) / "results" / f"{launch_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    return {"action": action, **fields, "result": str(result_path.resolve()), "launch_id": launch_id}


def _data_copies(run_root: Path, run: dict[str, Any]) -> list[dict[str, str]]:
    root = run_root.resolve()
    copies: list[dict[str, str]] = []
    for pbi in run.get("pbis", []):
        if pbi.get("status") == "passed":
            continue
        for need in pbi.get("data_needs") or []:
            copies.append({
                "pbi": pbi["id"],
                "source": need["source"],
                "dest": str((root / need["dest"]).resolve()),
                "why": need["why"],
            })
    return copies


def missing_data(run_root: Path, run: dict[str, Any]) -> list[str]:
    """Destinations of approved data that are not in place yet."""
    return [c["dest"] for c in _data_copies(run_root, run) if not Path(c["dest"]).exists()]


def _review_skill_action(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    review = run["final_review"]
    tmpl = active_template(run)
    return _make_caller_action(
        run,
        run_root,
        "review_skill",
        "reviewer",
        role="reviewer",
        skill=review["skill"],
        args=review.get("args", ""),
        base=run.get("base_commit") or "HEAD",
        repository=str(run_root.resolve()),
        spec=str(Path(run.get("spec_file") or run["goal_file"]).resolve()),
        rule_docs=[str((run_root / d).resolve()) for d in rule_docs_for(run_root, run, tmpl)],
        instructions=REVIEW_SKILL_INSTRUCTIONS,
    )


def _make_ask(run_root: Path, kind: str, choices: list[dict[str, str]], body: str) -> dict[str, Any]:
    q_path = _write_question(run_root, kind, body)
    return {
        "action": "ask",
        "kind": kind,
        "question": str(q_path.resolve()),
        "page": None,
        "choices": choices,
    }


def _plan_sends(run: dict[str, Any]) -> dict[str, list[str]]:
    sends: dict[str, list[str]] = {}
    for pbi in run.get("pbis", []):
        runners_for_pbi: list[str] = []
        for step in pbi.get("route") or []:
            r = step.get("runner")
            if r and r != "agent" and r not in runners_for_pbi:
                runners_for_pbi.append(r)
        if runners_for_pbi:
            sends[pbi["id"]] = runners_for_pbi
    return sends


def _build_presentation(run: dict[str, Any], page_kind: str) -> tuple[dict[str, Any], list[str]]:
    tmpl = active_template(run)
    pbis = run.get("pbis", [])
    attachments: list[str] = []
    presentation: dict[str, Any] = {
        "template": tmpl,
        "pbis": pbis,
        "estimate": run.get("estimate") or f"PBI {len(pbis)} 件",
        "decisions": [],
    }
    if page_kind == "spec":
        spec_path = run.get("spec_file")
        spec_text = "未確認"
        if spec_path and Path(spec_path).is_file():
            spec_text = Path(spec_path).read_text(encoding="utf-8-sig")
            attachments.append(str(Path(spec_path).resolve()))
        presentation.update({
            "conclusion": "仕様をまとめました。内容を確認してください。",
            "before": "調べる・仕様を詰める",
            "after": "計画の作成（PBI 分割）→ 計画の承認",
            "spec": spec_text,
        })
    elif page_kind == "plan":
        decisions = [
            {
                "question": q,
                "options": [
                    {"label": "このまま進める", "note": "既定の解釈で進む", "recommended": True},
                    {"label": "修正で明記する", "note": "計画を直す"},
                ],
            }
            for q in (run.get("open_questions") or [])
        ]
        plan_file = state.run_dir(Path(run["root"])) / "plan.json"
        if plan_file.is_file():
            attachments.append(str(plan_file.resolve()))
        presentation.update({
            "plan_checks": list(run.get("plan_checks") or []),
            "plan_e2e": list(run.get("plan_e2e") or []),
            "final_review": run.get("final_review"),
            "acceptance_first": run.get("acceptance_first"),
            "acceptance_reason": run.get("acceptance_reason"),
            "scenarios": list(run.get("scenarios") or []) if run.get("acceptance_first") else [],
            "interfaces": list(run.get("interfaces") or []),
            "conclusion": f"PBI {len(pbis)} 件の計画を作成しました。",
            "before": "仕様の承認",
            "after": ("受け入れテストを作る → Red の確認 → " if run.get("acceptance_first") else "")
            + "wave ごとの実装 → 機械の検査 → 最後の見直し → E2E → 報告",
            "decisions": decisions,
        })
    else:
        raw_fixes = run.get("fix_list") or []
        fixes = [
            {
                "id": f.get("id", ""),
                "title": f.get("title") or f.get("fix") or f.get("summary") or f.get("why") or f.get("id", ""),
                "reason": f.get("reason") or f.get("why") or f.get("summary") or "",
            }
            for f in raw_fixes
        ]
        ask_items = run.get("triage_ask_items") or []
        decisions = [
            {
                "question": f"{item.get('id', '')}: {item.get('reason', '')}",
                "options": [
                    {"label": "修正に含める", "note": "今回直す"},
                    {"label": "見送る", "note": "今回は直さない", "recommended": True},
                ],
            }
            for item in ask_items
        ]
        labels = {"skip": "見送ると仕分けた", "confidence": "確信度が 80 未満", "style": "書き方だけの指摘"}
        candidates = [
            {
                "id": f.get("id", ""),
                "kind": labels["skip"],
                "title": f.get("fix") or f.get("why") or f.get("id", ""),
                "reason": f.get("reason") or "",
            }
            for f in run.get("triage_skipped") or []
        ] + [
            {
                "id": f.get("id", ""),
                "kind": labels.get(f.get("dropped_by"), f.get("dropped_by", "")),
                "title": f.get("fix") or f.get("why") or f.get("id", ""),
                "reason": f"{f.get('severity', '')}・確信度 {f.get('confidence', '')}・{f.get('why', '')}",
            }
            for f in run.get("findings_dropped") or []
        ]
        presentation.update({
            "candidates": candidates,
            "conclusion": f"修正対象 {len(fixes)} 件を確認してください。" if fixes else "修正対象の指摘はありません。",
            "before": "最後の見直し → 指摘の仕分け",
            "after": "修正の実装と検査 → E2E → 報告" if fixes else "E2E → 報告",
            "decisions": decisions,
            "fix_list": fixes,
            "outside_tests": [e for e in run.get("outside_edits") or [] if guards.is_test_file(e["file"])],
        })
    return presentation, attachments


def _make_approval_ask(
    run_root: Path,
    run: dict[str, Any],
    page_kind: str,
    ask_kind: str,
    choices: list[dict[str, str]],
) -> dict[str, Any]:
    presentation, attachments = _build_presentation(run, page_kind)
    draft_path = state.run_dir(run_root) / "questions" / f"{ask_kind}.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)

    draft_res = pages.draft(page_kind, presentation)
    draft_path.write_text(draft_res["text"], encoding="utf-8")
    try:
        page_path = pages.render(draft_path)
    except pages.PageError:
        # Regenerate once before surfacing G10 refusal
        draft_res = pages.draft(page_kind, presentation)
        draft_path.write_text(draft_res["text"], encoding="utf-8")
        page_path = pages.render(draft_path)

    if page_kind == "plan":
        run["pending_plan_sends"] = _plan_sends(run)

    summary_lines = list(draft_res["summary"])
    q_path = _write_question(run_root, ask_kind, "\n".join(summary_lines))
    all_attachments = [str(draft_path.resolve()), *attachments]
    return {
        "action": "ask",
        "kind": ask_kind,
        "question": str(q_path.resolve()),
        "page": str(page_path.resolve()) if page_path else None,
        "draft": str(draft_path.resolve()),
        "summary": summary_lines,
        "chat": _chat_lines(summary_lines),
        "attachments": all_attachments,
        "choices": choices,
    }


def _chat_lines(summary_lines: list[str]) -> list[str]:
    """Return the lines the caller writes in chat: the conclusion and the decision only."""
    if len(summary_lines) <= 2:
        return list(summary_lines)
    return [summary_lines[0], summary_lines[-1]]


def _launch_for_state(
    run_root: Path,
    run: dict[str, Any],
    role: str,
    pbi_id: str | None = None,
    *,
    fix_group: int | None = None,
    alongside: list[dict[str, Any]] | None = None,
    check_budget: bool = True,
) -> dict[str, Any]:
    """One launch of role for the current state, after the checks that come before a launch.

    In a launch batch the caller checks the budget once for all its launches (check_budget False).
    """
    tmpl = active_template(run)
    step = _step_for_launch(run, tmpl, role, pbi_id)
    pbi_obj = state.get_pbi(run, pbi_id) if pbi_id else None
    level = (pbi_obj.get("level") if pbi_obj else None) or ("L2" if run["state"] == "fixing" else "none")
    runner = step["runner"]
    model = step.get("model", "")

    next_seq = run["counters"]["launch_seq"] + 1
    tentative_lid = f"{role}-{next_seq:04d}"
    result_path = (
        state.run_dir(run_root) / "results" / "survey.json"
        if run["state"] == "surveying"
        else state.run_dir(run_root) / "results" / f"{tentative_lid}.json"
    )
    final_prompt_path = (
        state.run_dir(run_root) / "prompts" / "survey.md"
        if run["state"] == "surveying"
        else state.run_dir(run_root) / "prompts" / f"{tentative_lid}.md"
    )

    with tempfile.TemporaryDirectory() as td:
        prompt_text, read_map, referenced = _compose_prompt_parts(
            run_root, run, tmpl, role, pbi_id, result_path, Path(td), fix_group=fix_group, alongside=alongside
        )

    def _cleanup_survey_prompt() -> None:
        if run["state"] == "surveying" and final_prompt_path.exists():
            final_prompt_path.unlink()

    if runner != "agent":
        # G3: external sends must be approved
        approved = (
            guards.sends_approved(run, pbi_obj or pbi_id, step)
            if pbi_id
            else (
                guards.sends_approved(run, role, step)
                or runner in (run.get("approvals", {}).get("role_sends") or {}).get(role, [])
            )
        )
        if not approved:
            _cleanup_survey_prompt()
            return _raise_prelaunch_guard(
                run_root,
                run,
                rule="G3",
                role=role,
                pbi_id=pbi_id,
                runner=runner,
                model=model,
                level=level,
                violations=[{"rule": "G3", "detail": f"external send to {runner} for {pbi_id or role} is not approved"}],
            )

        # G4: scan prompt and referenced files BEFORE writing prompt for external runner
        secret_hits = guards.scan_secrets(prompt_text)
        for ref in referenced:
            try:
                ref_hits = guards.scan_secrets(ref.read_text(encoding="utf-8-sig"))
                secret_hits.extend(ref_hits)
            except (OSError, UnicodeError):
                pass
        if secret_hits:
            _cleanup_survey_prompt()
            return _raise_prelaunch_guard(
                run_root,
                run,
                rule="G4",
                role=role,
                pbi_id=pbi_id,
                runner=runner,
                model=model,
                level=level,
                violations=[{"rule": "G4", "detail": f"secret ({h['kind']}) detected at line {h['line']}"} for h in secret_hits],
            )

        # G11: forbidden arguments configured on component
        spec_preview = runners.launch_spec(role, step, final_prompt_path, result_path, run_root, agents_path=run.get("agents_path"))
        blocked = _forbidden_args_for_launch(run, role, step, spec_preview)
        if blocked:
            _cleanup_survey_prompt()
            return _raise_prelaunch_guard(
                run_root,
                run,
                rule="G11",
                role=role,
                pbi_id=pbi_id,
                runner=runner,
                model=model,
                level=level,
                violations=[{"rule": "G11", "detail": f"forbidden args: {', '.join(blocked)}"}],
            )

    if check_budget and _budget_would_exceed(run, prompt_text):
        _cleanup_survey_prompt()
        flow.apply_event(run, "budget_exceeded")
        return ensure_pending_action(run_root, run)

    launch_id = state.next_launch_id(run, role)
    final_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    if run["state"] != "surveying":
        final_prompt_path.write_text(prompt_text, encoding="utf-8")
    run.setdefault("launch_reads", {})[launch_id] = read_map
    return _make_launch(
        run_root,
        run,
        role,
        step=step,
        prompt_path=final_prompt_path,
        result_path=result_path,
        pbi_id=pbi_id,
        launch_id=launch_id,
        fix_group=fix_group,
    )


def _owned_paths(run: dict[str, Any], pbi: dict[str, Any] | None = None, fix_group: int | None = None) -> list[str]:
    """The files one launch changes, for the overlap check: a PBI's targets, or a fix group's files."""
    if pbi is not None:
        return [p for p in (allowed_paths_for(run, "worker", pbi["id"]) or []) if p != "docs/pbi/"]
    if fix_group is not None:
        return _group_files((run.get("fix_groups") or [])[fix_group])
    return []


def _runner_kind(step: dict[str, Any]) -> str:
    return "agent" if step.get("runner") == "agent" else "external"


def _pick_together(candidates: list[Any], owned, kind_of, limit: int) -> list[Any]:
    """The candidates to start together, in order: the first one, then each later one of the same kind of
    runner whose files overlap none already picked, up to limit. A candidate with no known files goes alone."""
    chosen: list[Any] = []
    kind = None
    for item in candidates:
        if len(chosen) >= limit:
            break
        item_kind = kind_of(item)
        if item_kind is None:
            continue
        paths = owned(item)
        if not chosen:
            chosen.append(item)
            kind = item_kind
            if not paths:
                break
            continue
        if item_kind != kind or not paths:
            continue
        if any(plan.targets_overlap(paths, owned(other)) for other in chosen):
            continue
        chosen.append(item)
    return chosen


def _pick_building(run: dict[str, Any], tmpl: dict[str, Any]) -> list[dict[str, Any]]:
    """The PBIs of the wave to start together next (see Launch batch in the glossary)."""

    def kind_of(pbi: dict[str, Any]) -> str | None:
        try:
            return _runner_kind(_step_for_launch(run, tmpl, "worker", pbi["id"]))
        except state.StateError:
            return None

    candidates = acceptance.stage_buildable(run) if run["state"] == "building_acceptance" else state.buildable_pbis(run)
    picked = _pick_together(candidates, lambda pbi: _owned_paths(run, pbi=pbi), kind_of, template.parallel_limit(run))
    return picked or candidates[:1]


def _pick_fixing(run: dict[str, Any]) -> list[int]:
    """The fix groups to start together next; every group runs the same worker route."""
    return _pick_together(
        state.fix_groups_left(run), lambda idx: _owned_paths(run, fix_group=idx), lambda idx: "worker", template.parallel_limit(run)
    )


def _member_label(run: dict[str, Any], pbi_id: str | None, fix_group: int | None) -> str:
    return pbi_id if pbi_id else f"FIX-{(fix_group or 0) + 1}"


def _drop_launches(run: dict[str, Any], launches: list[dict[str, Any]]) -> None:
    """Undo launches built for a launch batch that is not given out after all."""
    for act in launches:
        (run.get("launch_reads") or {}).pop(act["launch_id"], None)
        pbi = state.get_pbi(run, act["pbi"]) if act.get("pbi") else None
        if pbi is not None and pbi.get("status") == "building":
            pbi["status"] = "pending"


def _launch_batch(run_root: Path, run: dict[str, Any], members: list[tuple[str | None, int | None]]) -> dict[str, Any]:
    """One launch, or a launch batch of worker launches for members (PBI id or fix group index).

    Each launch passes the checks before a launch on its own. When one fails, the batch is undone and that
    failure's question comes next. The budget is checked once for the whole batch.
    """
    if len(members) == 1:
        pbi_id, group = members[0]
        return _launch_for_state(run_root, run, "worker", pbi_id, fix_group=group)
    owned = {
        _member_label(run, pbi_id, group): _owned_paths(run, pbi=state.get_pbi(run, pbi_id) if pbi_id else None, fix_group=group)
        for pbi_id, group in members
    }
    launches: list[dict[str, Any]] = []
    for pbi_id, group in members:
        me = _member_label(run, pbi_id, group)
        alongside = [{"id": label, "files": files} for label, files in owned.items() if label != me]
        act = _launch_for_state(run_root, run, "worker", pbi_id, fix_group=group, alongside=alongside, check_budget=False)
        if act.get("action") != "launch":
            _drop_launches(run, launches)
            return act
        pbi = state.get_pbi(run, pbi_id) if pbi_id else None
        if pbi is not None:
            pbi["status"] = "building"
        launches.append(act)
    texts = [Path(act["prompt"]).read_text(encoding="utf-8") for act in launches]
    if _budget_would_exceed(run, texts):
        _drop_launches(run, launches)
        flow.apply_event(run, "budget_exceeded")
        return ensure_pending_action(run_root, run)
    batch_id = state.next_launch_id(run, "batch")
    for act in launches:
        act["batch"] = batch_id
    return {
        "action": "launches",
        "launch_id": batch_id,
        "runner": _runner_kind(launches[0]),
        "launches": launches,
    }


def _guard_ask(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    issue = run.get("guard_issue") or {}
    several = len(issue.get("launches") or []) > 1
    choices = [{"id": "retry", "label": "違反した起動をもう一度試す" if several else "同じ設定でもう 1 回試す"}]
    pbi_id = issue.get("pbi")
    pbi = state.get_pbi(run, pbi_id) if pbi_id else None
    if pbi and pbi.get("route") and (pbi.get("route_index", 0) + 1) < len(pbi["route"]):
        choices.append({"id": "next_step", "label": "経路の次の候補で試す"})
    choices.append({"id": "stop", "label": "止めて報告する"})
    detail = issue.get("detail") or "実行前後またはランナーの検査で問題が見つかりました。"
    return _make_ask(run_root, "guard", choices, detail)


def ensure_pending_action(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    """Build (if absent) and return the idempotent next action for run['state']."""
    if run.get("pending_action") is not None:
        return run["pending_action"]

    s = run["state"]
    sub = run.get("subphase")

    if s == "surveying":
        if run.get("omm") is None:
            run["omm"] = survey.omm_context(run_root, run.get("omm_cache_dir"))
        if run["omm"].get("source") == "miss" and not run.get("omm_asked"):
            run["omm_asked"] = True
            flow.apply_event(run, "omm_missing")
            return ensure_pending_action(run_root, run)
        action = _launch_for_state(run_root, run, "surveyor")
    elif s == "scanning_omm":
        action = _make_caller_action(
            run, run_root, "omm_scan", "omm", **survey.omm_scan_steps(run_root, run.get("omm_cache_dir"))
        )
    elif s == "preparing_data":
        action = _make_caller_action(run, run_root, "prepare", "prepare", copies=_data_copies(run_root, run))
    elif s == "asking_interview":
        q = interview.entry_question(run.get("spec_file"))
        action = _make_ask(run_root, q["kind"], q["choices"], q["question"])
    elif s == "interviewing":
        if sub == "ask":
            pending_iq = run.get("pending_interview_questions") or {}
            q_file = pending_iq.get("question_file")
            if q_file and Path(q_file).is_file():
                body = Path(q_file).read_text(encoding="utf-8")
            else:
                tree = interview.load_tree(run_root)
                frontier_qs = interview.frontier(tree)
                body = interview.format_questions(frontier_qs, run["counters"]["round"]) if frontier_qs else "詰める工程の質問に回答してください。"
            action = _make_ask(
                run_root,
                "interview_round",
                [{"id": "reply", "label": "回答する"}],
                body,
            )
            if q_file and Path(q_file).is_file():
                action["attachments"] = [str(Path(q_file).resolve())]
        else:
            action = _launch_for_state(run_root, run, "interviewer")
    elif s == "awaiting_spec_approval":
        kind, choices, _ = ASK_CHOICES[s]
        action = _make_approval_ask(run_root, run, "spec", kind, choices)
    elif s == "planning":
        action = _launch_for_state(run_root, run, "planner")
    elif s == "awaiting_plan_approval":
        kind, choices, _ = ASK_CHOICES[s]
        action = _make_approval_ask(run_root, run, "plan", kind, choices)
    elif s in ("building", "building_acceptance"):
        if s == "building" and locks.lock_pending(run):
            # The planner's review is done: lock the acceptance tests before any build or e2e PBI is launched.
            locks.lock(run_root, run)
        picked = _pick_building(run, active_template(run))
        pbi = picked[0] if picked else None
        if pbi is None and s == "building_acceptance":
            flow.apply_event(run, "acceptance_built")
            return ensure_pending_action(run_root, run)
        if pbi is None:
            has_passed = any(p.get("status") == "passed" for p in run.get("pbis", []))
            if has_passed and run["counters"]["review_count"] == 0:
                flow.apply_event(run, "no_buildable_pbis_review")
            else:
                flow.apply_event(run, "no_buildable_pbis_report")
            return ensure_pending_action(run_root, run)
        for chosen in picked:
            if chosen.get("status") == "passed":
                raise state.StateError(f"G8: refusing to launch passed PBI {chosen['id']}")
        action = _launch_batch(run_root, run, [(chosen["id"], None) for chosen in picked])
        if action.get("action") == "launch":
            pbi["status"] = "building"
        # never BuildBeforeLock: a build or e2e PBI is not launched before the acceptance tests are locked.
        flow.check_invariants(run)
    elif s == "checking":
        action = _make_wave_check(run_root, run)
    elif s == "checking_red":
        red_check = _make_red_check(run_root, run)
        if red_check is None:
            return ensure_pending_action(run_root, run)
        action = red_check
    elif s == "reviewing":
        if run.get("final_review"):
            action = _review_skill_action(run_root, run)
        else:
            action = _launch_for_state(run_root, run, "reviewer")
    elif s in ("triaging", "reviewing_acceptance"):
        action = _launch_for_state(run_root, run, "planner")
    elif s == "awaiting_fix_approval":
        kind, choices, _ = ASK_CHOICES[s]
        action = _make_approval_ask(run_root, run, "fix_list", kind, choices)
    elif s == "fixing":
        if sub == "check":
            action = _make_check(run_root, run, "fixcheck")
        elif run.get("fix_groups"):
            groups = _pick_fixing(run)
            if not groups:
                run["subphase"] = "check"
                return ensure_pending_action(run_root, run)
            action = _launch_batch(run_root, run, [(None, group) for group in groups])
        else:
            action = _launch_for_state(run_root, run, "worker")
    elif s == "running_e2e":
        if not run.get("plan_e2e"):
            run["e2e_skipped"] = True
            flow.apply_event(run, "e2e_skipped")
            return ensure_pending_action(run_root, run)
        action = _make_check(run_root, run, "e2e")
    elif s == "asking_guard":
        action = _guard_ask(run_root, run)
    elif s == "asking_plan_broken":
        kind, choices, body = ASK_CHOICES[s]
        if run.get("return_state") not in flow.REPLAN_FROM:
            choices = [c for c in choices if c["id"] != "replan"]
        change = run.get("pending_plan_change") or {}
        detail = "\n".join(
            line
            for line in (
                body,
                f"理由: {change['reason']}" if change.get("reason") else "",
                f"触る必要があったファイル: {', '.join(change['needed_files'])}" if change.get("needed_files") else "",
            )
            if line
        )
        action = _make_ask(run_root, kind, choices, detail)
    elif s == "asking_rebuild":
        kind, choices, body = ASK_CHOICES[s]
        detail, has_dependents = _rebuild_detail(run, body)
        if not has_dependents:
            choices = [c for c in choices if c["id"] != "hold_stop"]
        action = _make_ask(run_root, kind, choices, detail)
    elif s in ASK_CHOICES:
        kind, choices, body = ASK_CHOICES[s]
        action = _make_ask(run_root, kind, choices, body)
    elif s in ("reporting", "done"):
        report_path = _write_report(run_root, run)
        if s == "reporting":
            flow.apply_event(run, "report_written")
            report_path = _write_report(run_root, run)
        handoff_path = _write_handoff(run_root, run, report_path)
        unfinished = [p["id"] for p in state.unfinished_pbis(run)]
        action = {
            "action": "done",
            "report": str(report_path.resolve()),
            "handoff": str(handoff_path.resolve()),
            "complete": not unfinished,
            "unfinished_pbis": unfinished,
        }
    else:
        raise state.StateError(f"unhandled state: {s}")

    run["pending_action"] = action
    return action
