"""The signatures of the public interfaces and the keys of the files workers also touch, in the lock.

Driven through the CLI in temporary git repositories: when the acceptance tests are locked, docs/pbi/locks.json holds
the Python signature of each public interface (taken with ast), the interfaces whose signature cannot be taken, and
the compared keys of package.json and pyproject.toml (not the whole files). A worker that then changes a locked
signature is stopped by G1 with the reason, and its source file stays; a worker that changes a locked key is stopped
and the key alone goes back to its locked value. TypeScript (tsc) and Go (go doc) are checked when the tools are
there, and skipped when they are not.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import guards, locks, signatures  # noqa: E402
from tests.test_acceptance_red import approve_plan, drive, finish_stage, good, pass_review, pbis_of, saved  # noqa: E402
from tests.test_conductor import run_cli, write_result_for  # noqa: E402
from tests.test_flow_rules import init_git_repo  # noqa: E402
from tests.test_locks import STUB, SUPPORT, lock_plan  # noqa: E402

PYPROJECT = (
    '[project]\nname = "demo"\n\n'
    '[tool.pytest.ini_options]\naddopts = "-q"\ntestpaths = ["tests"]\n\n'
    "[tool.ruff]\nline-length = 100\n"
)
PACKAGE = {"name": "demo", "scripts": {"test": "pytest -q", "lint": "ruff check ."}}
RUBY = {"module": "lib/calc.rb", "name": "Calc"}


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8").rstrip()


def signature_plan() -> dict:
    """lock_plan with a Ruby interface too, and pyproject.toml among the acceptance PBI's targets (the runner's
    settings it shares with the workers)."""
    obj = lock_plan()
    obj["interfaces"] = [*obj["interfaces"], RUBY]
    obj["pbis"][0]["targets"] = [*obj["pbis"][0]["targets"], "pyproject.toml"]
    return obj


def g1(run: dict) -> list[str]:
    return sorted(v["detail"] for v in run["guard_issue"]["violations"] if v["rule"] == "G1")


class LockSignatureCliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {locks.HOME_ENV: home.name})
        env.start()
        self.addCleanup(env.stop)
        # The files workers also touch are there before the run.
        (self.root / "pyproject.toml").write_bytes(PYPROJECT.encode("utf-8"))
        (self.root / "package.json").write_bytes((json.dumps(PACKAGE, indent=2) + "\n").encode("utf-8"))

    def lock_and_launch(self) -> dict:
        """Build the acceptance stage (the stub places app.add), pass the Red check and the planner's review, and
        return the launch of build PBI-3 after the lock."""
        act = approve_plan(self.root, signature_plan())
        (self.root / "app.py").write_bytes(STUB.encode("utf-8"))
        (self.root / SUPPORT).parent.mkdir(parents=True, exist_ok=True)
        (self.root / SUPPORT).write_bytes(b'{"fixture": [1, 2]}\n')
        self.assertEqual("check", finish_stage(self.root, act, {"R1": good("R1")})["action"])
        launch = pass_review(self.root, drive(self.root))
        self.assertEqual(("launch", ["PBI-3"]), (launch["action"], pbis_of(launch)))
        self.assertIsNotNone(saved(self.root)["lock"])
        return launch

    def done(self, launch: dict) -> dict:
        write_result_for(launch, {"status": "ok"})
        code, out = run_cli("done", "--root", str(self.root), "--launch-id", launch["launch_id"])
        self.assertEqual(0, code, out)
        return out

    def test_the_lock_list_holds_the_signatures_the_unextractable_interfaces_and_the_keys(self) -> None:
        self.lock_and_launch()
        run = saved(self.root)
        listed = json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))
        self.assertEqual(
            {"app:add": {"module": "app", "name": "add", "language": "Python", "file": "app.py", "signature": "def add(a, b)"}},
            listed["signatures"],
        )
        self.assertEqual(
            [{"module": "lib/calc.rb", "name": "Calc", "language": "Ruby", "reason": signatures.UNSUPPORTED}],
            listed["unextractable"],
        )
        self.assertEqual(listed["unextractable"], run["lock"]["unextractable"])
        self.assertEqual(
            {"tool.pytest.ini_options": {"addopts": "-q", "testpaths": ["tests"]}, "tool.behave": None},
            listed["keys"]["pyproject.toml"],
        )
        self.assertEqual("pytest -q", listed["keys"]["package.json"]["scripts.test"])
        self.assertNotIn("scripts.lint", listed["keys"]["package.json"])
        # Only the keys are locked: neither file is a locked file nor in the lock commit.
        self.assertEqual(["acceptance/R1.json", SUPPORT, locks.LIST_PATH], run["lock"]["files"])
        committed = git(self.root, "show", "--name-only", "--format=", run["lock"]["commit"]).splitlines()
        self.assertNotIn("pyproject.toml", committed)
        self.assertNotIn("package.json", committed)
        # The copy outside the work tree, which the checks go by, holds the same.
        self.assertEqual(listed, locks.locked_list(self.root, run))

    def test_a_worker_that_changes_a_locked_signature_is_stopped_with_the_reason_and_the_source_stays(self) -> None:
        launch = self.lock_and_launch()
        changed = b"def add(a, b, c=0):\n    return a + b + c\n"
        (self.root / "app.py").write_bytes(changed)
        self.done(launch)
        run = saved(self.root)
        self.assertEqual("asking_guard", run["state"])
        self.assertEqual(["locked signature changed: app add（鍵: def add(a, b) / 今: def add(a, b, c=0)）"], g1(run))
        self.assertEqual(["app add"], run["guard_issue"]["signatures_changed"])
        self.assertIn("公開の口のシグネチャが鍵と違うので止めた（ソースのファイルは戻していない）: app add", run["guard_issue"]["detail"])
        self.assertEqual([], run["guard_issue"]["lock_restored"])
        self.assertFalse(run.get("lock_restored"))
        # The worker's implementation is not taken away.
        self.assertEqual(changed, (self.root / "app.py").read_bytes())

        # A retry that leaves the signature alone is not stopped again for what the first launch did.
        code, again = run_cli("answer", "--root", str(self.root), "--choice", "retry")
        self.assertEqual(0, code, again)
        self.assertEqual(("launch", "PBI-3"), (again["action"], again["pbi"]))
        nxt = self.done(again)
        self.assertEqual(("checking", "check"), (saved(self.root)["state"], nxt["action"]))

    def test_a_worker_that_changes_a_locked_key_is_stopped_and_only_the_key_goes_back(self) -> None:
        launch = self.lock_and_launch()
        (self.root / "app.py").write_bytes(b"def add(a, b):\n    return a + b\n")
        text = PYPROJECT.replace('addopts = "-q"', "addopts = \"-q -k 'not R1'\"").replace("line-length = 100", "line-length = 120")
        (self.root / "pyproject.toml").write_bytes(text.encode("utf-8"))
        package = json.loads(json.dumps(PACKAGE))
        package["scripts"].update(test="pytest -q -x", lint="ruff check src")
        (self.root / "package.json").write_bytes((json.dumps(package, indent=2) + "\n").encode("utf-8"))
        self.done(launch)
        run = saved(self.root)
        self.assertEqual("asking_guard", run["state"])
        self.assertEqual(
            [
                "locked key changed: package.json scripts.test（鍵: pytest -q / 今: pytest -q -x）",
                'locked key changed: pyproject.toml tool.pytest.ini_options（鍵: {"addopts": "-q", "testpaths": ["tests"]} / '
                "今: {\"addopts\": \"-q -k 'not R1'\", \"testpaths\": [\"tests\"]}）",
            ],
            g1(run),
        )
        restored = ["package.json scripts.test", "pyproject.toml tool.pytest.ini_options"]
        self.assertEqual(restored, run["guard_issue"]["keys_restored"])
        self.assertIn("鍵のキーを鍵のコミットの値に戻した: " + ", ".join(restored), run["guard_issue"]["detail"])
        self.assertEqual([{"launch_id": launch["launch_id"], "files": [], "keys": restored}], run["lock_restored"])

        # The keys are back to their locked values; the rest of each file keeps the worker's change.
        now = tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual({"addopts": "-q", "testpaths": ["tests"]}, now["tool"]["pytest"]["ini_options"])
        self.assertEqual(120, now["tool"]["ruff"]["line-length"])
        self.assertEqual("demo", now["project"]["name"])
        scripts = json.loads((self.root / "package.json").read_text(encoding="utf-8"))["scripts"]
        self.assertEqual({"test": "pytest -q", "lint": "ruff check src"}, scripts)
        self.assertIn(b"return a + b", (self.root / "app.py").read_bytes())

    def test_a_change_of_the_body_only_is_not_a_violation(self) -> None:
        launch = self.lock_and_launch()
        (self.root / "app.py").write_bytes(b"# The sum.\ndef add(a, b):\n    total = a + b\n    return total\n")
        nxt = self.done(launch)
        run = saved(self.root)
        self.assertEqual(("checking", "check"), (run["state"], nxt["action"]))
        self.assertFalse(run.get("guard_issue"))


class KeyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_the_scripts_the_test_commands_run_are_locked_keys(self) -> None:
        (self.root / "package.json").write_bytes(
            json.dumps({"scripts": {"test": "vitest run", "test:acc": "cucumber-js", "e2e": "playwright test"}}).encode("utf-8")
        )
        keys, unreadable = signatures.read_keys(self.root, ["package.json"], ["npm run test:acc -- {file}", "pnpm e2e"])
        self.assertEqual([], unreadable)
        self.assertEqual(
            {"scripts.test": "vitest run", "jest": None, "cucumber": None, "mocha": None, "ava": None,
             "scripts.test:acc": "cucumber-js", "scripts.e2e": "playwright test"},
            keys["package.json"],
        )

    def test_an_ini_section_is_put_back_and_the_other_sections_stay(self) -> None:
        path = self.root / "pytest.ini"
        path.write_bytes(b"[pytest]\r\naddopts = -q\r\n\r\n[other]\r\nx = 1\r\n")
        keys, _ = signatures.read_keys(self.root, ["pytest.ini"])
        self.assertEqual({"pytest": {"addopts": "-q"}}, keys["pytest.ini"])
        path.write_bytes(b"[pytest]\r\naddopts = -q -k 'not R1'\r\n\r\n[other]\r\nx = 2\r\n")
        self.assertTrue(signatures.restore_key(self.root, "pytest.ini", "pytest", keys["pytest.ini"]["pytest"]))
        self.assertEqual(keys, signatures.read_keys(self.root, ["pytest.ini"])[0])
        self.assertIn(b"x = 2", path.read_bytes())
        self.assertNotIn(b"\n", path.read_bytes().replace(b"\r\n", b""))


class ToolTests(unittest.TestCase):
    """TypeScript and Go: taken with tsc and go doc when they are there; recorded as unextractable when not."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        init_git_repo(self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "calc.ts").write_bytes(
            b"/** Adds. */\nexport function add(a: number, b: number): number {\n  return a + b;\n}\n"
        )
        (self.root / "go.mod").write_bytes(b"module example.com/demo\n\ngo 1.21\n")
        (self.root / "calc").mkdir()
        (self.root / "calc" / "calc.go").write_bytes(b"package calc\n\n// Add adds.\nfunc Add(a, b int) int {\n\treturn a + b\n}\n")
        self.interfaces = [{"module": "src/calc.ts", "name": "add"}, {"module": "calc", "name": "Add"}]

    def isolate_go(self) -> None:
        """Keep go's caches, settings and telemetry out of the real home directory."""
        tools = Path(tempfile.mkdtemp(prefix="atelier-go-"))
        self.addCleanup(shutil.rmtree, tools, True)
        env = {
            "GOCACHE": str(tools / "cache"), "GOPATH": str(tools / "path"), "GOMODCACHE": str(tools / "mod"),
            "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod", "GOENV": "off", "GOTELEMETRY": "off",
            "HOME": str(tools), "USERPROFILE": str(tools), "APPDATA": str(tools / "appdata"),
            "LOCALAPPDATA": str(tools / "localappdata"), "XDG_CONFIG_HOME": str(tools / "config"),
        }
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_the_tools_the_interfaces_are_recorded_as_unextractable(self) -> None:
        with patch.object(signatures.shutil, "which", return_value=None):
            sigs, unextractable = signatures.extract(self.root, self.interfaces)
        self.assertEqual({}, sigs)
        self.assertEqual(
            [
                {"module": "src/calc.ts", "name": "add", "language": "TypeScript", "reason": f"{signatures.NO_TOOL}: tsc"},
                {"module": "calc", "name": "Add", "language": "Go", "reason": f"{signatures.NO_TOOL}: go"},
            ],
            unextractable,
        )

    def stopped(self, sigs: dict, change: tuple[str, bytes]) -> list[dict]:
        before = guards.snapshot(self.root, locked={"commit": "", "blobs": {}, "signatures": sigs, "keys": {}})
        (self.root / change[0]).write_bytes(change[1])
        return [v for v in guards.compare(self.root, before) if v["rule"] == "G1"]

    @unittest.skipUnless(shutil.which("go"), "go is not installed")
    def test_go_signatures_are_taken_with_go_doc_and_a_change_is_stopped(self) -> None:
        self.isolate_go()
        sigs, _ = signatures.extract(self.root, self.interfaces[1:])
        self.assertEqual("func Add(a, b int) int", sigs["calc:Add"]["signature"])
        body = self.stopped(sigs, ("calc/calc.go", b"package calc\n\n// Add sums.\nfunc Add(a, b int) int {\n\ts := a + b\n\treturn s\n}\n"))
        self.assertEqual([], body)
        changed = self.stopped(sigs, ("calc/calc.go", b"package calc\n\nfunc Add(a, b, c int) int {\n\treturn a + b + c\n}\n"))
        self.assertEqual(["locked signature changed: calc Add（鍵: func Add(a, b int) int / 今: func Add(a, b, c int) int）"],
                         [v["detail"] for v in changed])

    @unittest.skipUnless(shutil.which("tsc"), "tsc is not installed")
    def test_typescript_signatures_are_taken_from_the_declarations_and_a_change_is_stopped(self) -> None:
        sigs, _ = signatures.extract(self.root, self.interfaces[:1])
        self.assertEqual("export declare function add(a: number, b: number): number;", sigs["src/calc.ts:add"]["signature"])
        body = self.stopped(sigs, ("src/calc.ts", b"export function add(a: number, b: number): number {\n  const s = a + b;\n  return s;\n}\n"))
        self.assertEqual([], body)
        changed = self.stopped(sigs, ("src/calc.ts", b"export function add(a: number, b: string): string {\n  return a + b;\n}\n"))
        self.assertEqual(1, len(changed))
        self.assertTrue(changed[0]["detail"].startswith("locked signature changed: src/calc.ts add"))


if __name__ == "__main__":
    unittest.main()
