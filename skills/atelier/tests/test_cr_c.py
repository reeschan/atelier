"""The step definition checks on the forms a review found: nested helpers in a Then, default imports, relative
imports that only end like an interface, import aliases across lines, and strict off in an unrelated package.json
script."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import stepcheck  # noqa: E402

INTERFACES = [{"module": "app", "name": "add"}, {"module": "src/api.ts", "name": "add"}]
VITEST = 'import { expect, test } from "vitest";\n{head}\ntest("R1-1", () => {{\n  expect({call}).toBe(3);\n}});\n'
CUCUMBER = (
    'import { Then } from "@cucumber/cucumber";\nimport assert from "node:assert/strict";\nimport { add } from "../src/api";\n\n'
    'Then("the sum is {int}", function (sum: number) {\n  assert.equal(add(1, 2), sum);\n});\n'
)


class ReviewFormsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def check(self, path: str, text: str, interfaces: list[dict] | None = None) -> list[str]:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        return stepcheck.check_file(self.root, path, INTERFACES if interfaces is None else interfaces)["problems"]

    def assert_fails(self, problems: list[str], rule: str) -> None:
        self.assertTrue(any(stepcheck.RULES[rule] in p for p in problems), problems)

    def vitest(self, head: str, call: str = "add(1, 2)") -> str:
        return VITEST.replace("{head}", head).replace("{call}", call).replace("{{", "{").replace("}}", "}")

    # C1: an assert in a nested function the Then never calls does not count

    def test_uncalled_nested_assert_fails(self) -> None:
        text = (
            "from app import add\n\n\ndef then_sum_is(actual, expected):\n"
            "    def check():\n        assert actual == expected\n    return actual\n"
        )
        self.assert_fails(self.check("tests/acceptance/test_nested.py", text), "then")
        in_class = (
            "from app import add\n\n\ndef test_add():\n    class Check:\n        def run(self):\n            assert add(1, 2) == 3\n"
        )
        self.assert_fails(self.check("tests/acceptance/test_nested_class.py", in_class), "then")

    def test_called_nested_assert_and_own_assert_pass(self) -> None:
        called = (
            "from app import add\n\n\ndef then_sum_is(actual, expected):\n"
            "    def check():\n        assert actual == expected\n    check()\n"
        )
        self.assertEqual([], self.check("tests/acceptance/test_called.py", called))
        own = "from app import add\n\n\ndef then_sum_is(actual, expected):\n    def fmt(x):\n        return str(x)\n    assert fmt(actual) == fmt(expected)\n"
        self.assertEqual([], self.check("tests/acceptance/test_own.py", own))

    # C2: a default import is the export named default

    def test_default_import_needs_a_default_interface(self) -> None:
        text = self.vitest('import other from "../src/api";\n', "other(1, 2)")
        self.assert_fails(self.check("acceptance/d1.test.ts", text), "import")
        mixed = self.vitest('import other, { add } from "../src/api";\n')
        self.assert_fails(self.check("acceptance/d2.test.ts", mixed), "import")
        allowed = [*INTERFACES, {"module": "src/api.ts", "name": "default"}]
        self.assertEqual([], self.check("acceptance/d3.test.ts", text, allowed))

    def test_namespace_and_require_still_check_members(self) -> None:
        star = self.vitest('import * as api from "../src/api";\n', "api.add(1, 2)")
        self.assertEqual([], self.check("acceptance/n1.test.ts", star))
        required = 'const api = require("../src/api");\nconst { expect, test } = require("@jest/globals");\n\ntest("R1-1", () => {\n  expect(api.carry(1)).toBe(0);\n});\n'
        self.assert_fails(self.check("acceptance/n2.test.js", required), "import")
        required_ok = required.replace("api.carry(1)).toBe(0)", "api.add(1, 2)).toBe(3)")
        self.assertEqual([], self.check("acceptance/n3.test.js", required_ok))

    # C5: a relative import names the interface only by its resolved path

    def test_relative_import_must_resolve_to_the_interface(self) -> None:
        wrong = self.vitest('import { add } from "../api";\n')
        self.assert_fails(self.check("tests/acceptance.test.ts", wrong), "import")
        deeper = self.vitest('import { add } from "../lib/src/api";\n')
        self.assert_fails(self.check("tests/deeper.test.ts", deeper), "import")
        right = self.vitest('import { add } from "../src/api.ts";\n')
        self.assertEqual([], self.check("tests/right.test.ts", right))

    def test_aliases_still_resolve_under_src(self) -> None:
        alias = self.vitest('import { add } from "@/api";\n')
        self.assertEqual([], self.check("tests/alias.test.ts", alias))
        bare = [{"module": "api", "name": "add"}]
        self.assertEqual([], self.check("tests/bare.test.ts", self.vitest('import { add } from "../src/api";\n'), bare))

    # C6: whitespace around as

    def test_import_alias_across_lines(self) -> None:
        text = self.vitest('import {\n  add as\n  sum,\n} from "../src/api";\n', "sum(1, 2)")
        self.assertEqual([], self.check("acceptance/a1.test.ts", text))
        mocked = text + "sum = () => 3;\n"
        self.assert_fails(self.check("acceptance/a2.test.ts", mocked), "mock")
        hidden = self.vitest('import {\n  carry as\n  add,\n} from "../src/api";\n')
        self.assert_fails(self.check("acceptance/a3.test.ts", hidden), "import")

    # C4: package.json counts only where cucumber reads it

    def write_package(self, scripts: dict[str, str], extra: dict | None = None) -> None:
        data = {"name": "shop", "scripts": scripts, **(extra or {})}
        (self.root / "package.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def test_unrelated_no_strict_in_package_json_passes(self) -> None:
        self.write_package({"lint:docs": "docs-lint --no-strict", "test:acc": "cucumber-js"}, {"eslintConfig": {"strict": False}})
        self.assertEqual([], self.check("acceptance/s1.steps.ts", CUCUMBER))

    def test_no_strict_for_cucumber_in_package_json_fails(self) -> None:
        cases = {
            "direct": {"test:acc": "cucumber-js --no-strict"},
            "through another script": {"bdd": "cucumber-js", "test:acc": "npm run bdd -- --no-strict"},
        }
        for label, scripts in cases.items():
            with self.subTest(case=label):
                self.write_package(scripts)
                self.assert_fails(self.check("acceptance/s2.steps.ts", CUCUMBER), "undefined")
        self.write_package({"test": "cucumber-js"}, {"cucumber": {"strict": False}})
        self.assert_fails(self.check("acceptance/s3.steps.ts", CUCUMBER), "undefined")


if __name__ == "__main__":
    unittest.main()
