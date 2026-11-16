"""Spec section check, entry question, design tree (atelier.tree/1), and interviewer prompt."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_TREE = "atelier.tree/1"
REQUIRED_SPEC_SECTIONS: tuple[str, ...] = ("目的", "範囲", "受け入れ条件", "範囲外")
TO_SPEC_SECTIONS: tuple[str, ...] = (
    "Problem Statement",
    "Solution",
    "User Stories",
    "Implementation Decisions",
    "Testing Decisions",
    "Out of Scope",
    "Further Notes",
)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "範囲外": ("out of scope", "non-goals", "non goals", "範囲外", "対象外"),
    "目的": ("problem statement", "purpose", "goal", "objective", "目的", "課題", "背景と目的", "背景・目的"),
    "範囲": ("scope", "in scope", "solution", "範囲", "対象範囲", "スコープ", "解決策", "方針"),
    "受け入れ条件": (
        "acceptance criteria",
        "user stories",
        "acceptance",
        "受け入れ条件",
        "受入条件",
        "ユーザーストーリー",
        "完了条件",
    ),
}

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


class InterviewError(ValueError):
    """Raised when a design tree or user answer fails validation."""


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_single_line(value: Any) -> bool:
    return _is_nonempty_str(value) and "\n" not in value and "\r" not in value


def _extract_headings(text: str) -> list[str]:
    headings: list[str] = []
    fence_char: str | None = None
    for line in text.splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence_char is None:
                fence_char = marker
            elif fence_char == marker:
                fence_char = None
            continue
        if fence_char is not None:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        heading = re.sub(r"\s*\{[^}]*\}\s*$", "", m.group(1))
        heading = re.sub(r"^\d+[.)]\s*", "", heading).strip().rstrip("#").strip()
        if heading:
            headings.append(heading)
    return headings


def _normalize_heading(heading: str) -> str:
    return re.sub(r"\s+", " ", heading.lower()).strip(" :：")


def _match_required_section(heading: str) -> str | None:
    norm = _normalize_heading(heading)
    parts = [p.strip() for p in re.split(r"[/|()（）・]+", norm) if p.strip()]
    candidates = [norm, *parts]
    for section in ("範囲外", "目的", "範囲", "受け入れ条件"):
        aliases = _SECTION_ALIASES[section]
        if any(cand in aliases for cand in candidates):
            return section
    return None


def spec_sections(path: str | Path | None) -> list[str]:
    """Return missing required sections from a spec file."""
    if path is None:
        return list(REQUIRED_SPEC_SECTIONS)
    spec_path = Path(path)
    if not spec_path.is_file():
        return list(REQUIRED_SPEC_SECTIONS)
    text = spec_path.read_text(encoding="utf-8-sig")
    found: set[str] = set()
    for heading in _extract_headings(text):
        matched = _match_required_section(heading)
        if matched is not None:
            found.add(matched)
    return [sec for sec in REQUIRED_SPEC_SECTIONS if sec not in found]


def validate_to_spec(text_or_path: str | Path) -> list[str]:
    """Return missing to-spec headings from spec text or a spec file path."""
    if isinstance(text_or_path, Path) or (
        isinstance(text_or_path, str) and "\n" not in text_or_path and Path(text_or_path).is_file()
    ):
        text = Path(text_or_path).read_text(encoding="utf-8-sig")
    else:
        text = str(text_or_path)
    present = {_normalize_heading(h) for h in _extract_headings(text)}
    return [sec for sec in TO_SPEC_SECTIONS if _normalize_heading(sec) not in present]


def entry_question(
    spec_path: str | Path | None = None,
    missing: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the single Japanese entry question asking whether to run the grill."""
    has_spec = spec_path is not None and Path(spec_path).is_file()
    if not has_spec:
        missing_list = list(REQUIRED_SPEC_SECTIONS) if missing is None else list(missing)
        return {
            "kind": "interview_or_skip",
            "has_spec": False,
            "missing": missing_list,
            "recommended": "interview",
            "question": "仕様ファイルがありません。仕様を詰める工程（grill）を開始します。",
            "choices": [{"id": "interview", "label": "仕様を詰める（開始する）"}],
        }

    missing_list = spec_sections(spec_path) if missing is None else list(missing)
    if missing_list:
        missing_text = "・".join(missing_list)
        return {
            "kind": "interview_or_skip",
            "has_spec": True,
            "missing": missing_list,
            "recommended": "interview",
            "question": (
                f"渡された仕様で次の節が不足しています（{missing_text}）。"
                "今の仕様で進めますか、それとも先に仕様を詰めますか？"
            ),
            "choices": [
                {"id": "interview", "label": "仕様を詰める"},
                {"id": "skip", "label": "今の仕様で進める"},
            ],
        }

    return {
        "kind": "interview_or_skip",
        "has_spec": True,
        "missing": [],
        "recommended": "skip",
        "question": (
            "渡された仕様に必要な節（目的・範囲・受け入れ条件・範囲外）がそろっています。"
            "今の仕様で PBI 作成へ進みますか、それとも先に仕様を詰めますか？"
        ),
        "choices": [
            {"id": "skip", "label": "今の仕様で進める"},
            {"id": "interview", "label": "仕様を詰める"},
        ],
    }


def tree_path(root: str | Path) -> Path:
    return Path(root).resolve() / "docs" / "pbi" / "interview" / "tree.json"


def empty_tree() -> dict[str, Any]:
    return {"schema": SCHEMA_TREE, "decided": [], "open": []}


def _check_acyclic(open_items: list[dict[str, Any]], known_ids: set[str], errors: list[str]) -> None:
    open_by_id = {item["id"]: item for item in open_items if _is_single_line(item.get("id"))}
    dep_valid = True
    for item in open_items:
        qid = item.get("id")
        deps = item.get("depends_on")
        if not _is_single_line(qid) or not isinstance(deps, list):
            dep_valid = False
            continue
        for dep in deps:
            if not _is_single_line(dep):
                dep_valid = False
                continue
            if dep == qid:
                errors.append(f"{qid} が自分自身に依存しています。")
                dep_valid = False
            elif dep not in known_ids:
                errors.append(f"{qid} が存在しない問い {dep!r} に依存しています。")
                dep_valid = False
    if not dep_valid:
        return

    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node_id: str) -> bool:
        if node_id in visited:
            return True
        if node_id in visiting:
            return False
        visiting.add(node_id)
        for dep in open_by_id[node_id]["depends_on"]:
            if dep in open_by_id and not dfs(dep):
                return False
        visiting.remove(node_id)
        visited.add(node_id)
        return True

    for qid in open_by_id:
        if not dfs(qid):
            errors.append("未決の問いの依存関係に循環があります。")
            break


def validate_tree(obj: Any) -> list[str]:
    """Validate an atelier.tree/1 design tree and return Japanese error messages."""
    if not isinstance(obj, dict):
        return ["設計の木は JSON オブジェクトにしてください。"]

    errors: list[str] = []
    extra_keys = set(obj) - {"schema", "decided", "open"}
    if extra_keys:
        errors.append(f"設計の木に不要な項目があります: {', '.join(sorted(extra_keys))}")
    if obj.get("schema") != SCHEMA_TREE:
        errors.append(f"schema は {SCHEMA_TREE} にしてください。")

    seen_ids: set[str] = set()

    decided = obj.get("decided")
    if not isinstance(decided, list):
        errors.append("decided は配列にしてください。")
    else:
        for idx, item in enumerate(decided):
            label = f"decided[{idx}]"
            if not isinstance(item, dict):
                errors.append(f"{label} はオブジェクトにしてください。")
                continue
            if set(item) != {"id", "text"}:
                errors.append(f"{label} は id と text だけにしてください。")
            qid = item.get("id")
            if not _is_single_line(qid):
                errors.append(f"{label} の id は空でない 1 行の文字列にしてください。")
            elif qid in seen_ids:
                errors.append(f"問い id {qid!r} が重複しています。")
            else:
                seen_ids.add(qid)
            if not _is_single_line(item.get("text")):
                errors.append(f"{label} の text は空でない 1 行の文字列にしてください。")

    open_items = obj.get("open")
    if not isinstance(open_items, list):
        errors.append("open は配列にしてください。")
        return errors

    valid_open: list[dict[str, Any]] = []
    for idx, item in enumerate(open_items):
        label = f"open[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{label} はオブジェクトにしてください。")
            continue
        if set(item) != {"id", "question", "recommended", "depends_on"}:
            errors.append(f"{label} は id, question, recommended, depends_on だけにしてください。")
        qid = item.get("id")
        if not _is_single_line(qid):
            errors.append(f"{label} の id は空でない 1 行の文字列にしてください。")
        elif qid in seen_ids:
            errors.append(f"問い id {qid!r} が重複しています。")
        else:
            seen_ids.add(qid)
            label = qid
        for key in ("question", "recommended"):
            if not _is_nonempty_str(item.get(key)):
                errors.append(f"{label} の {key} は空でない文字列にしてください。")
        deps = item.get("depends_on")
        if not isinstance(deps, list):
            errors.append(f"{label} の depends_on は配列にしてください。")
        else:
            for dep_idx, dep in enumerate(deps):
                if not _is_single_line(dep):
                    errors.append(f"{label}.depends_on[{dep_idx}] は空でない 1 行の文字列にしてください。")
        valid_open.append(item)

    if len(valid_open) == len(open_items):
        _check_acyclic(valid_open, seen_ids, errors)

    return errors


def validate_interviewer_tree_update(before: dict[str, Any], after: Any) -> list[str]:
    """Verify interviewer output is a valid tree and did not alter decided[] (G6)."""
    errors = validate_tree(after)
    if not errors and after.get("decided") != before.get("decided"):
        errors.append("G6: 詰める役は decided を変更できません。")
    return errors


def load_tree(root: str | Path) -> dict[str, Any]:
    path = tree_path(root)
    if not path.exists():
        return empty_tree()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InterviewError(f"設計の木を読み込めません: {exc}") from exc
    errors = validate_tree(data)
    if errors:
        raise InterviewError(" ".join(errors))
    return data


def save_tree(root: str | Path, tree: dict[str, Any]) -> Path:
    errors = validate_tree(tree)
    if errors:
        raise InterviewError(" ".join(errors))
    path = tree_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def frontier(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Return open questions whose dependencies are all already in decided."""
    errors = validate_tree(tree)
    if errors:
        raise InterviewError(" ".join(errors))
    decided_ids = {item["id"] for item in tree["decided"]}
    return [
        copy.deepcopy(item)
        for item in tree["open"]
        if all(dep in decided_ids for dep in item["depends_on"])
    ]


def _normalize_answer_map(
    answers: Mapping[str, str] | Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if isinstance(answers, Mapping):
        items = list(answers.items())
    elif isinstance(answers, Sequence) and not isinstance(answers, (str, bytes)):
        items = []
        for entry in answers:
            if not isinstance(entry, Mapping) or "id" not in entry or "text" not in entry:
                raise InterviewError("回答の形式は {id, text} にしてください。")
            items.append((entry["id"], entry["text"]))
    else:
        raise InterviewError("回答は対応表または配列にしてください。")

    for qid, raw_text in items:
        if not _is_single_line(qid):
            raise InterviewError("回答対象の問い id が不正です。")
        if not isinstance(raw_text, str):
            raise InterviewError(f"{qid} の回答は文字列にしてください。")
        line = re.sub(r"[\r\n]+", " ", raw_text).strip()
        if not line:
            raise InterviewError(f"{qid} の回答が空です。")
        if qid in normalized:
            raise InterviewError(f"{qid} の回答が重複しています。")
        normalized[qid] = line
    return normalized


def apply_answers(
    tree: dict[str, Any],
    answers: Mapping[str, str] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Move answered questions from open to decided using ONLY user-provided text (G6)."""
    errors = validate_tree(tree)
    if errors:
        raise InterviewError(" ".join(errors))

    answer_map = _normalize_answer_map(answers)
    open_ids = {item["id"] for item in tree["open"]}
    for qid in answer_map:
        if qid not in open_ids:
            raise InterviewError(f"未決の問いに存在しない id です: {qid}")

    new_decided = copy.deepcopy(tree["decided"])
    new_open: list[dict[str, Any]] = []
    for item in tree["open"]:
        qid = item["id"]
        if qid in answer_map:
            new_decided.append({"id": qid, "text": answer_map[qid]})
        else:
            new_open.append(copy.deepcopy(item))

    updated = {"schema": SCHEMA_TREE, "decided": new_decided, "open": new_open}
    post_errors = validate_tree(updated)
    if post_errors:
        raise InterviewError(" ".join(post_errors))
    return updated


def format_questions(questions: Sequence[Mapping[str, Any]], round_num: int | None = None) -> str:
    """Format frontier questions in Japanese, numbered, with each recommended answer."""
    title = f"# 設計の確認（第 {round_num} 巡）" if round_num is not None else "# 設計の確認"
    lines = [title, ""]
    for idx, q in enumerate(questions, start=1):
        lines.append(f"{idx}. [{q['id']}] {q['question']}")
        lines.append(f"   - おすすめの答え: {q['recommended']}")
    return "\n".join(lines) + "\n"


def interviewer_prompt(
    root: str | Path,
    round: int,  # noqa: A002 - matches ticket contract interviewer_prompt(root, round)
    *,
    facts_path: str | Path | None = None,
    tree_file: str | Path | None = None,
    questions_path: str | Path | None = None,
    spec_path: str | Path | None = None,
    result_path: str | Path | None = None,
    out_path: str | Path | None = None,
) -> Path:
    """Write the per-round interviewer prompt in cache-friendly fixed order."""
    root_p = Path(root).resolve()
    f_path = Path(facts_path).resolve() if facts_path else root_p / "docs" / "pbi" / "results" / "survey.json"
    t_path = Path(tree_file).resolve() if tree_file else tree_path(root_p)
    q_path = Path(questions_path).resolve() if questions_path else root_p / "docs" / "pbi" / "interview" / f"round-{round}-questions.md"
    s_path = Path(spec_path).resolve() if spec_path else root_p / "docs" / "pbi" / "interview" / "spec.md"
    r_path = Path(result_path).resolve() if result_path else root_p / "docs" / "pbi" / "results" / f"interview-{round}.json"
    p_path = Path(out_path).resolve() if out_path else root_p / "docs" / "pbi" / "prompts" / f"interview-{round}.md"

    for path in (p_path, q_path, s_path, r_path, t_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    steps_text = (_PROMPTS_DIR / "interviewer.md").read_text(encoding="utf-8").rstrip()
    if f_path.is_file():
        facts_text = f_path.read_text(encoding="utf-8-sig").strip()
    else:
        facts_text = "{}"

    if t_path.is_file():
        try:
            tree_obj = json.loads(t_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InterviewError(f"設計の木を読み込めません: {exc}") from exc
        errs = validate_tree(tree_obj)
        if errs:
            raise InterviewError(" ".join(errs))
    else:
        tree_obj = empty_tree()
    tree_text = json.dumps(tree_obj, ensure_ascii=False, indent=2)

    body = "\n".join(
        [
            steps_text,
            "",
            "## Fact Sheet",
            "```json",
            facts_text,
            "```",
            "",
            "## Design Tree",
            "```json",
            tree_text,
            "```",
            "",
            f"Round: {round}",
            f"Tree file: {t_path}",
            f"Questions file: {q_path}",
            f"Spec file: {s_path}",
            f"Result: {r_path}",
            "",
        ]
    )
    p_path.write_text(body, encoding="utf-8")
    return p_path
