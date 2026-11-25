"""The step definition checks of the Red check, driven through the CLI on Python and JS/TS acceptance test files.

The fake acceptance command stands in for pytest, vitest and cucumber-js: every case id written in the file becomes
a testcase that fails with an assertion, so every requirement passes the runner's half of the Red check. Whether a
requirement passes is then up to the step definition checks alone.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import actions, pages, state, stepcheck  # noqa: E402
from tests.test_acceptance_red import ACCEPTANCE, GHERKIN, approve_plan, drive, pass_review, pbis_of, red_records, saved  # noqa: E402
from tests.test_acceptance_toggle import UNIT  # noqa: E402
from tests.test_conductor import run_cli, write_result_for  # noqa: E402
from tests.test_integration import make_pbi, make_plan  # noqa: E402
from tests.test_worker_checks import ticket_of  # noqa: E402

FAKE_RUNNER = """import re, sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
ids = list(dict.fromkeys(re.findall(r"\\bR\\d+-\\d+\\b", text)))
rows = "".join('<testcase classname="acc" name="case[%s]"><failure message="assert 0"/></testcase>' % i for i in ids)
junit = Path(sys.argv[2])
junit.parent.mkdir(parents=True, exist_ok=True)
junit.write_text('<?xml version="1.0"?><testsuites><testsuite name="acc">' + rows + "</testsuite></testsuites>", encoding="utf-8")
raise SystemExit(1)
"""
INTERFACES = [
    {"module": "app", "name": "add"},
    {"module": "src/api.ts", "name": "add"},
    {"module": "calc/add.go", "name": "Add"},
]

PY_GOOD_PLAIN = '''"""Cases R1-1 and R1-2."""
import pytest

from app import add


def then_sum_is(actual, expected):
    assert actual == expected


@pytest.mark.parametrize("case_id, a, b, total", [("R1-1", 1, 2, 3), ("R1-2", 2, 2, 4)])
def test_add(case_id, a, b, total):
    then_sum_is(add(a, b), total)
'''
PY_BDD = '''"""Cases {rid}-1 and {rid}-2."""
import pytest
from pytest_bdd import parsers, scenarios, then, when

import app

scenarios("add.feature")


@when(parsers.parse("add {{a:d}} and {{b:d}}"), target_fixture="result")
def do_add(a, b):
    try:
        return app.add(a, b)
    except ValueError:
        return "error"


@then(parsers.parse("the sum is {{total:d}}"))
def check_sum(result, total):
{then_body}
'''
PY_GOOD_THEN = "    assert result == total"


def py_bdd(rid: str, then_body: str = PY_GOOD_THEN, extra: str = "") -> str:
    return PY_BDD.format(rid=rid, then_body=then_body) + extra


JS_VITEST = '''import {{ expect, test{vi} }} from "vitest";
import {{ {names} }} from "../src/api";
{head}
const rows = [
  {{ id: "{rid}-1", a: 1, b: 2, sum: 3 }},
  {{ id: "{rid}-2", a: 2, b: 2, sum: 4 }},
];

function thenSumIs(actual: number, expected: number): void {{
  expect(actual).toBe(expected);
}}

function whenAdd(a: number, b: number): number {{
{when}
}}

test.each(rows)("{rid} $id", ({{ a, b, sum }}) => {{
  thenSumIs(whenAdd(a, b), sum);
}});
'''
JS_GOOD_WHEN = "  return add(a, b);"


def js_vitest(rid: str, *, names: str = "add", head: str = "", when: str = JS_GOOD_WHEN, vi: bool = False) -> str:
    return JS_VITEST.format(rid=rid, names=names, head=head, when=when, vi=", vi" if vi else "")


JS_CUCUMBER = '''import {{ Then, When }} from "@cucumber/cucumber";
import assert from "node:assert/strict";
import {{ add }} from "{spec}";

// Cases {rid}-1 and {rid}-2.
When("add {{int}} and {{int}}", function (a: number, b: number) {{
  try {{
    this.result = add(a, b);
  }} catch (error) {{
    if (error instanceof RangeError) {{
      this.result = "error";
      return;
    }}
    throw error;
  }}
}});

Then("the sum is {{int}}", function (sum: number) {{
{then_body}
}});
'''
JS_GOOD_THEN = "  assert.equal(this.result, sum);"


def js_cucumber(rid: str, then_body: str = JS_GOOD_THEN, spec: str = "../src/api") -> str:
    return JS_CUCUMBER.format(rid=rid, then_body=then_body, spec=spec)


GO_TEST = "package acceptance\n\n// Cases R15-1 and R15-2.\nfunc TestAdd(t *testing.T) {}\n"

# Requirement -> (file, good content, hollowed content or None, the check the hollowed one breaks).
FILES: dict[str, tuple[str, str, str | None, str]] = {
    "R1": ("acceptance/test_r1.py", PY_GOOD_PLAIN, None, ""),
    "R2": ("acceptance/r2_steps.py", py_bdd("R2"), None, ""),
    "R3": ("acceptance/r3_steps.py", py_bdd("R3"), py_bdd("R3", "    pass"), "then"),
    "R4": (
        "acceptance/test_r4.py",
        PY_GOOD_PLAIN.replace("R1", "R4"),
        PY_GOOD_PLAIN.replace("R1", "R4").replace("import pytest\n", "from unittest.mock import patch\n\nimport pytest\n").replace(
            '@pytest.mark.parametrize', '@patch("app.add", return_value=3)\n@pytest.mark.parametrize'
        ),
        "mock",
    ),
    "R5": (
        "acceptance/r5_steps.py",
        py_bdd("R5"),
        py_bdd("R5").replace("except ValueError:", "except Exception:"),
        "except",
    ),
    "R6": (
        "acceptance/test_r6.py",
        PY_GOOD_PLAIN.replace("R1", "R6"),
        PY_GOOD_PLAIN.replace("R1", "R6").replace("from app import add", "from app import add, _carry"),
        "import",
    ),
    "R7": (
        "acceptance/r7_steps.py",
        py_bdd("R7"),
        py_bdd("R7", extra='\n\ndef pytest_bdd_step_func_lookup_error(request, feature, scenario, step, exception):\n    pytest.skip("undefined step")\n'),
        "undefined",
    ),
    "R8": ("acceptance/r8.test.ts", js_vitest("R8"), None, ""),
    "R9": ("acceptance/r9.steps.ts", js_cucumber("R9"), None, ""),
    "R10": ("acceptance/r10.steps.ts", js_cucumber("R10"), js_cucumber("R10", "  // nothing to check"), "then"),
    "R11": (
        "acceptance/r11.test.ts",
        js_vitest("R11"),
        js_vitest("R11", vi=True, head='\nvi.mock("../src/api", () => ({ add: () => 3 }));\n'),
        "mock",
    ),
    "R12": (
        "acceptance/r12.test.ts",
        js_vitest("R12"),
        js_vitest("R12", when="  try {\n    return add(a, b);\n  } catch {}\n  return 0;"),
        "except",
    ),
    "R13": (
        "acceptance/r13.test.ts",
        js_vitest("R13"),
        js_vitest("R13", head='import { carry } from "../src/internal/carry";\n'),
        "import",
    ),
    "R14": ("acceptance/loose/r14.steps.ts", js_cucumber("R14", spec="../../src/api"), None, "undefined"),
    "R15": ("acceptance/r15_test.go", GO_TEST, None, ""),
}
# R14's file is fine; the cucumber config next to it lets undefined steps pass.
LOOSE_CONFIG = ("acceptance/loose/cucumber.js", "module.exports = { default: { strict: false, paths: ['acceptance/loose'] } };\n")
BAD = {rid: rule for rid, (_, _, bad, rule) in FILES.items() if rule}
GOOD = [rid for rid in FILES if rid not in BAD]


def step_plan() -> dict:
    """Acceptance PBI-1 writes every requirement's file and the runner config; stub PBI-2 places the interfaces; build PBI-3 needs both."""
    acc = {**make_pbi("PBI-1", targets=[*(path for path, *_ in FILES.values()), LOOSE_CONFIG[0]]), "kind": "acceptance"}
    stub = {**make_pbi("PBI-2", targets=["app.py", "src/api.ts", "calc/add.go"]), "kind": "stub"}
    build = make_pbi("PBI-3", depends_on=["PBI-1", "PBI-2"], targets=["app.py", "src/api.ts", "calc/add.go"])
    obj = make_plan([acc, stub, build])
    obj["interfaces"] = INTERFACES
    obj["scenarios"] = [
        {
            "id": rid,
            "text": f"要件 {rid}",
            "mode": "normal",
            "gherkin": GHERKIN,
            "cases": [{"id": f"{rid}-{n}", "values": {"a": n, "b": 2, "sum": n + 2}, "pbi": "PBI-3", "needs": []} for n in (1, 2)],
            "spare_count": 0,
        }
        for rid in FILES
    ]
    return obj


def write_files(root: Path, hollow: bool) -> None:
    for path, good, bad, _ in FILES.values():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bad if hollow and bad is not None else good, encoding="utf-8", newline="\n")
    config = root / LOOSE_CONFIG[0]
    if hollow:
        config.write_text(LOOSE_CONFIG[1], encoding="utf-8", newline="\n")
    elif config.exists():
        config.unlink()


def return_launches(root: Path, act: dict) -> dict:
    files = {rid: path for rid, (path, *_) in FILES.items()}
    for launch in act["launches"] if act["action"] == "launches" else [act]:
        write_result_for(launch, {"status": "ok", "acceptance_files": files} if launch["pbi"] == "PBI-1" else {"status": "ok"})
    code, nxt = run_cli("done", "--root", str(root), "--launch-id", act["launch_id"])
    assert code == 0, nxt
    return nxt


class StepChecksThroughTheRedCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_hollowed_files_fail_the_red_check_and_good_ones_pass(self) -> None:
        act = approve_plan(self.root, step_plan(), [UNIT, ACCEPTANCE])
        (self.root / "fake_red.py").write_text(FAKE_RUNNER, encoding="utf-8", newline="\n")
        self.assertEqual(["PBI-1", "PBI-2"], pbis_of(act))

        # The plan approval page names the language the checks skip, from the interfaces' module extensions.
        page = (state.run_dir(self.root) / "questions" / "plan_approval.md").read_text(encoding="utf-8")
        self.assertIn("| ステップ定義の機械の検査 | Python・JS/TS で流す（飛ばす言語: Go（calc/add.go）） |", page)

        write_files(self.root, hollow=True)
        check = return_launches(self.root, act)
        self.assertEqual("check", check.get("action"), check)
        self.assertEqual(len(FILES), len(check["commands"]))

        rebuild = drive(self.root)
        run = saved(self.root)
        self.assertEqual("building_acceptance", run["state"])
        self.assertEqual(["PBI-1"], pbis_of(rebuild))
        for rid in GOOD:
            with self.subTest(requirement=rid):
                self.assertEqual([True], [h["passed"] for h in run["red"][rid]["history"]], run["red"][rid])
        for rid, rule in BAD.items():
            with self.subTest(requirement=rid):
                entry = run["red"][rid]
                # A failed step check counts as a failed Red check: one failure, rebuilt by its acceptance PBI.
                self.assertEqual(("failed", 1), (entry["status"], entry["failures"]))
                path = FILES[rid][0]
                expected = f"ステップ定義の検査: {path}"
                self.assertTrue(
                    any(r.startswith(expected) and stepcheck.RULES[rule] in r for r in entry["reasons"]), entry["reasons"]
                )
        self.assertTrue(any("acceptance/loose/cucumber.js:1" in r for r in run["red"]["R14"]["reasons"]))
        (event,) = red_records(self.root)
        self.assertEqual(("fail", ["red_check"]), (event["result"], event["rules"]))
        self.assertEqual(sorted(BAD), sorted(item["requirement"] for item in ticket_of(rebuild)["red_failure"]))
        self.assertEqual(
            {"checked": 14, "failed": len(BAD), "skipped": {"Go": ["acceptance/r15_test.go"]}},
            {**run["step_checks"], "checked": len(run["step_checks"]["checked"]), "failed": len(run["step_checks"]["failed"])},
        )
        report = actions._write_report(self.root, run).read_text(encoding="utf-8")
        self.assertIn(
            "- ステップ定義の機械の検査: 流した 14 ファイル（落ちた 10）、飛ばした言語: Go（calc/add.go、acceptance/r15_test.go）", report
        )

        # The rebuilt files are good: every requirement passes and the build PBI starts.
        write_files(self.root, hollow=False)
        write_result_for(rebuild, {"status": "ok"})
        code, check = run_cli("done", "--root", str(self.root), "--launch-id", rebuild["launch_id"])
        self.assertEqual((0, "check"), (code, check["action"]))
        nxt = pass_review(self.root, drive(self.root))
        run = saved(self.root)
        self.assertEqual("building", run["state"])
        self.assertEqual(["PBI-3"], pbis_of(nxt))
        self.assertEqual({"passed"}, {e["status"] for e in run["red"].values()})
        self.assertEqual([], run["unresolved"])
        report = actions._write_report(self.root, run).read_text(encoding="utf-8")
        self.assertIn("流した 14 ファイル（落ちた 0）", report)


def check(root: Path, path: str, text: str, interfaces: list[dict] | None = None) -> list[str]:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return stepcheck.check_file(root, path, INTERFACES if interfaces is None else interfaces)["problems"]


class StepCheckFileTests(unittest.TestCase):
    """The checks on single files: what passes, what fails, and which languages are skipped."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def assert_fails(self, problems: list[str], rule: str) -> None:
        self.assertTrue(any(stepcheck.RULES[rule] in p for p in problems), problems)

    def test_good_files_pass(self) -> None:
        for rid in GOOD:
            path, good, *_ = FILES[rid]
            with self.subTest(requirement=rid):
                self.assertEqual([], check(self.root, path, good))

    def test_python_hollowed_forms(self) -> None:
        cases = {
            "then": "from app import add\n\n\ndef then_sum_is(actual, expected):\n    return actual == expected\n",
            "mock": "import app\n\n\ndef test_add(monkeypatch):\n    monkeypatch.setattr(app, 'add', lambda a, b: 3)\n    assert app.add(1, 2) == 3\n",
            "except": "from app import add\n\n\ndef when_add(a, b):\n    try:\n        return add(a, b)\n    except:\n        return None\n",
            "import": "import app\n\n\ndef test_add():\n    assert app._carry(1) == 0\n",
            "undefined": "from pytest_bdd.exceptions import StepDefinitionNotFoundError\n\ntry:\n    import steps\nexcept StepDefinitionNotFoundError:\n    pass\n",
        }
        for rule, text in cases.items():
            with self.subTest(rule=rule):
                self.assert_fails(check(self.root, f"tests/acceptance/test_{rule}.py", text), rule)
        rebinding = "from app import add\n\nadd = lambda a, b: 3\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        self.assert_fails(check(self.root, "tests/acceptance/test_rebind.py", rebinding), "mock")
        suppress = "import contextlib\n\nfrom app import add\n\n\ndef when_add():\n    with contextlib.suppress(Exception):\n        return add(1, 2)\n"
        self.assert_fails(check(self.root, "tests/acceptance/test_suppress.py", suppress), "except")

    def test_python_allowed_forms(self) -> None:
        (self.root / "shop").mkdir()
        (self.root / "shop" / "models.py").write_text("x = 1\n", encoding="utf-8")
        text = (
            "import json\nfrom unittest import mock\n\nimport pytest\n\nfrom app import add\nfrom tests.acceptance.support import make_store\n\n\n"
            "def test_add():\n    with mock.patch('time.time', return_value=0):\n        try:\n            add(1, 2)\n        except Exception:\n            raise\n"
            "    with pytest.raises(ValueError):\n        add('a', 2)\n"
        )
        self.assertEqual([], check(self.root, "tests/acceptance/test_ok.py", text))
        internal = "from shop.models import x\n\n\ndef test_x():\n    assert x == 1\n"
        self.assert_fails(check(self.root, "tests/acceptance/test_internal.py", internal), "import")

    def test_js_hollowed_forms(self) -> None:
        spy = js_vitest("X1", vi=True, names="add", head='import * as api from "../src/api";\nvi.spyOn(api, "add");\n')
        self.assert_fails(check(self.root, "acceptance/x1.test.ts", spy), "mock")
        member = js_vitest("X2", head='import * as api from "../src/api";\nconst carry = api.carry;\n')
        self.assert_fails(check(self.root, "acceptance/x2.test.ts", member), "import")
        empty_promise = js_vitest("X3", head="Promise.reject(new Error('x')).catch(() => {});\n")
        self.assert_fails(check(self.root, "acceptance/x3.test.ts", empty_promise), "except")
        no_expect = 'import { test } from "vitest";\nimport { add } from "../src/api";\n\ntest("X4", () => {\n  add(1, 2);\n});\n'
        self.assert_fails(check(self.root, "acceptance/x4.test.ts", no_expect), "then")
        pending = js_cucumber("X5").replace("  try {", "  return 'pending';\n  try {", 1)
        self.assert_fails(check(self.root, "acceptance/x5.steps.ts", pending), "undefined")
        required = 'const { add, carry } = require("../src/api");\nconst { expect, test } = require("@jest/globals");\n\ntest("X6", () => {\n  expect(add(1, 2)).toBe(3);\n});\n'
        self.assert_fails(check(self.root, "acceptance/x6.test.js", required), "import")

    def test_js_strings_and_comments_do_not_count(self) -> None:
        text = js_vitest("X7", head='// vi.mock("../src/api")\nconst note = "catch {} and import x from \\"../src/internal\\"";\n')
        self.assertEqual([], check(self.root, "acceptance/x7.test.ts", text))

    def test_other_languages_are_skipped(self) -> None:
        result = stepcheck.check_file(self.root, "acceptance/r15_test.go", INTERFACES)
        self.assertEqual(("Go", False, []), (result["language"], result["checked"], result["problems"]))
        self.assertEqual({"Go": ["calc/add.go"]}, stepcheck.interface_languages(INTERFACES))
        self.assertEqual({}, stepcheck.interface_languages([{"module": "shop.api", "name": "add"}]))

    def test_plan_page_row(self) -> None:
        self.assertEqual("流さない（受け入れを先に置かない）", pages.step_check_text(False, INTERFACES))
        self.assertEqual("Python・JS/TS で流す（飛ばす言語なし）", pages.step_check_text(True, INTERFACES[:2]))
        text = pages.draft("plan", {"pbis": [], "acceptance_first": True, "interfaces": INTERFACES})["text"]
        self.assertIn("| ステップ定義の機械の検査 | Python・JS/TS で流す（飛ばす言語: Go（calc/add.go）） |", text)
        self.assertNotIn("?" * 3, text)


if __name__ == "__main__":
    unittest.main()
