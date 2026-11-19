"""Build idempotent next actions, approval pages, and role prompts for each state."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import flow, guards, interview, pages, plan, record, runners, state, survey, template

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
        "1 回自動修正しても検査に落ちました。次の対応を選んでください。",
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


def _group_files(items: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for item in items:
        for path in [item.get("file"), *(item.get("files") or [])]:
            if isinstance(path, str) and path.strip() and path not in files:
                files.append(path)
    return files


def allowed_paths_for(run: dict[str, Any], role: str, pbi_id: str | None) -> list[str] | None:
    if pbi_id:
        pbi = state.get_pbi(run, pbi_id)
        if pbi:
            files = pbi.get("allowed_paths") or pbi.get("targets") or pbi.get("files")
            if files:
                return list(files) + ["docs/pbi/"]
        return None
    if run.get("state") == "fixing" and run.get("fix_groups"):
        idx = run.get("fix_group_index", 0)
        groups = run["fix_groups"]
        if 0 <= idx < len(groups):
            group_files = _group_files(groups[idx])
            if group_files:
                return group_files + ["docs/pbi/"]
        return None
    if role in ("surveyor", "interviewer", "reviewer", "planner"):
        return ["docs/pbi/"]
    return None


def _git_diff_text(run_root: Path) -> str:
    check = subprocess.run(
        ["git", "-C", str(run_root), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check.returncode != 0:
        return "(no git repository)"
    has_head = subprocess.run(
        ["git", "-C", str(run_root), "rev-parse", "--verify", "HEAD"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    cmd = ["git", "-C", str(run_root), "diff"]
    if has_head:
        cmd.append("HEAD")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    diff = res.stdout.strip()
    return diff if diff else "(empty diff)"


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
        f"- 自動修正回数: {run['counters']['auto_fix_count']}",
        f"- 見直し回数: {run['counters']['review_count']}",
        f"- E2E 失敗回数: {run['counters']['e2e_failures']}",
    ]
    omm = run.get("omm") or {}
    if omm.get("source") == "none" and omm.get("reason") == "tools":
        header.append("- OMM: 道具（node・omm・omm-pr-scan v1.2）が無いため、OMM なしで調べた")
    elif omm.get("source"):
        header.append(f"- OMM: {omm['source']}")
    if run.get("e2e_skipped"):
        header.append("- E2E: E2E のコマンドが無いため流していない")
    header += _unfinished_lines(run)
    header.append("")
    report_path.write_text("\n".join(header) + summary_md, encoding="utf-8")
    return report_path


def _budget_would_exceed(run: dict[str, Any], prompt_text: str) -> bool:
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
    est_tokens = max(500, len(prompt_text) // 4 + 500)
    est_cost = round(est_tokens * 0.00001, 6)
    if budget_tokens is not None and (recorded_tokens + est_tokens) > budget_tokens:
        return True
    if budget_usd is not None and (recorded_cost + est_cost) > budget_usd:
        return True
    return False


def _step_for_launch(run: dict[str, Any], tmpl: dict[str, Any], role: str, pbi_id: str | None) -> dict[str, Any]:
    s = run["state"]
    if s == "building" and pbi_id:
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
    if s == "auto_fixing" and pbi_id:
        pbi = state.get_pbi(run, pbi_id)
        if pbi is not None:
            if pbi.get("last_step"):
                return dict(pbi["last_step"])
            if pbi.get("route"):
                return dict(pbi["route"][pbi.get("route_index", 0)])
            choice = template.choose_route(tmpl, pbi)
            return dict(choice["route"][0])
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
) -> tuple[str, dict[str, int], list[Path]]:
    """Build staged prompt text, filtered token read map, and referenced files."""
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

    if s in ("planning", "triaging"):
        staged_prompt = stage_dir / f"planner-{s}.md"
        mode = run.get("planner_mode", "plan") if s == "planning" else "triage"
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
        kept = record.filter_parts(tmpl, parts_by_id)
        return prompt_text, record.measure_read(kept), referenced

    if s in ("building", "auto_fixing", "fixing"):
        staged_prompt = stage_dir / f"worker-{s}.md"
        failure_output: str | None = None
        diff_text: str | None = None
        if s == "building":
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
        elif s == "auto_fixing":
            mode = "auto_fix"
            pbi = state.get_pbi(run, pbi_id or "") or {}
            ticket = {
                "id": pbi.get("id", pbi_id or "auto_fix"),
                "targets": list(pbi.get("targets") or pbi.get("files") or []),
                "do_not_touch": list(pbi.get("do_not_touch") or []),
                "checks": plan.wave_commands([pbi], run.get("plan_checks") or []),
            }
            failure_output = run.get("last_check_failure") or "(no failure output)"
            diff_text = _git_diff_text(run_root)
        else:
            mode = "fix"
            groups = run.get("fix_groups") or []
            idx = run.get("fix_group_index", 0)
            if groups and 0 <= idx < len(groups):
                group = groups[idx]
                ticket = {
                    "id": f"FIX-{idx + 1}",
                    "items": group,
                    "files": _group_files(group),
                    "checks": plan.fix_commands(run.get("pbis", []), run.get("plan_checks") or [], _group_files(group)),
                }
            else:
                ticket = {
                    "id": "FIX-E2E",
                    "failure": run.get("last_e2e_failure") or run.get("last_check_failure") or "",
                    "checks": check_commands(run, "fixcheck") + list(run.get("plan_e2e") or []),
                }

        plan.worker_prompt(
            run_root,
            mode=mode,
            ticket=ticket,
            result_path=result_path,
            failure_output=failure_output,
            diff_text=diff_text,
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
        if mode == "auto_fix":
            parts_by_id["section.failure_output"] = failure_output or ""
            parts_by_id["section.diff"] = diff_text or ""

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
) -> dict[str, Any]:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    spec = runners.launch_spec(role, step, prompt_path, result_path, run_root, agents_path=run.get("agents_path"))
    if step["runner"] == "agent":
        return {
            "action": "launch",
            **spec,
            "pbi": pbi_id,
            "launch_id": launch_id,
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
    launch_id = state.next_launch_id(run, kind)
    result_path = state.run_dir(run_root) / "results" / f"{launch_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "action": "check",
        "commands": commands,
        "result": str(result_path.resolve()),
        "launch_id": launch_id,
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
            "conclusion": f"PBI {len(pbis)} 件の計画を作成しました。",
            "before": "仕様の承認",
            "after": "wave ごとの実装 → 機械の検査 → 最後の見直し → E2E → 報告",
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


def _launch_for_state(run_root: Path, run: dict[str, Any], role: str, pbi_id: str | None = None) -> dict[str, Any]:
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
            run_root, run, tmpl, role, pbi_id, result_path, Path(td)
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

    if _budget_would_exceed(run, prompt_text):
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
    )


def _guard_ask(run_root: Path, run: dict[str, Any]) -> dict[str, Any]:
    issue = run.get("guard_issue") or {}
    choices = [{"id": "retry", "label": "同じ設定でもう 1 回試す"}]
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
    elif s == "building":
        pbi = state.next_buildable_pbi(run)
        if pbi is None:
            has_passed = any(p.get("status") == "passed" for p in run.get("pbis", []))
            if has_passed and run["counters"]["review_count"] == 0:
                flow.apply_event(run, "no_buildable_pbis_review")
            else:
                flow.apply_event(run, "no_buildable_pbis_report")
            return ensure_pending_action(run_root, run)
        if pbi.get("status") == "passed":
            raise state.StateError(f"G8: refusing to launch passed PBI {pbi['id']}")
        action = _launch_for_state(run_root, run, "worker", pbi_id=pbi["id"])
        if action.get("action") == "launch":
            pbi["status"] = "building"
    elif s == "checking":
        action = _make_check(run_root, run, "check")
    elif s == "auto_fixing":
        target = None
        if run.get("auto_fix_pbi"):
            target = state.get_pbi(run, run["auto_fix_pbi"])
        if target is None:
            failed = [p for p in run.get("pbis", []) if p.get("status") == "failed"]
            if not failed:
                failed = [p for p in run.get("pbis", []) if p.get("status") == "built"]
            if not failed:
                raise state.StateError("no failed PBI found for auto_fixing")
            target = failed[0]
        if target.get("status") == "passed":
            raise state.StateError(f"G8: refusing to auto-fix passed PBI {target['id']}")
        action = _launch_for_state(run_root, run, "worker", pbi_id=target["id"])
    elif s == "reviewing":
        if run.get("final_review"):
            action = _review_skill_action(run_root, run)
        else:
            action = _launch_for_state(run_root, run, "reviewer")
    elif s == "triaging":
        action = _launch_for_state(run_root, run, "planner")
    elif s == "awaiting_fix_approval":
        kind, choices, _ = ASK_CHOICES[s]
        action = _make_approval_ask(run_root, run, "fix_list", kind, choices)
    elif s == "fixing":
        if sub == "check":
            action = _make_check(run_root, run, "fixcheck")
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
        unfinished = [p["id"] for p in state.unfinished_pbis(run)]
        action = {
            "action": "done",
            "report": str(report_path.resolve()),
            "complete": not unfinished,
            "unfinished_pbis": unfinished,
        }
    else:
        raise state.StateError(f"unhandled state: {s}")

    run["pending_action"] = action
    return action
