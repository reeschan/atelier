"""The lock's signatures and keys at their edges: TOML tables written as dotted keys or after an indented header,
key files that are links, generic Python signatures, TypeScript literal types, tsc that wants --ignoreConfig, the
copy of the lock list that does not match its hash, and locked paths that look like globs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the TOML keys are not taken.
    tomllib = None  # type: ignore[assignment]

NEEDS_TOMLLIB = unittest.skipIf(tomllib is None, "pyproject.toml keys need tomllib (Python 3.11 or later)")

SKILLS_DIR = Path(__file__).resolve().parent.parent
if str(SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR))

from atelier import locks, signatures, state  # noqa: E402
from tests.test_flow_rules import init_git_repo  # noqa: E402

LOCKED = {"addopts": "-q", "testpaths": ["tests"]}
KEY = "tool.pytest.ini_options"


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8").rstrip()


class TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()


@NEEDS_TOMLLIB
class TomlRestoreTests(TempRoot):
    def restore(self, text: str) -> tuple[bool, dict, str]:
        path = self.root / "pyproject.toml"
        path.write_bytes(text.encode("utf-8"))
        ok = signatures.restore_key(self.root, "pyproject.toml", KEY, LOCKED)
        now = path.read_text(encoding="utf-8")
        return ok, tomllib.loads(now), now

    def test_an_explicit_table_is_put_back_and_the_rest_stays(self) -> None:
        ok, data, _ = self.restore('[project]\nname = "demo"\n\n[tool.pytest.ini_options]\naddopts = "-x"\n\n[tool.ruff]\nx = 1\n')
        self.assertTrue(ok)
        self.assertEqual(LOCKED, data["tool"]["pytest"]["ini_options"])
        self.assertEqual(({"name": "demo"}, {"x": 1}), (data["project"], data["tool"]["ruff"]))

    def test_a_table_written_as_dotted_keys_under_a_parent_table_is_put_back(self) -> None:
        ok, data, _ = self.restore('[tool.pytest]\nini_options.addopts = "-x"\nother = 1\n\n[project]\nname = "demo"\n')
        self.assertTrue(ok)
        self.assertEqual(LOCKED, data["tool"]["pytest"]["ini_options"])
        self.assertEqual((1, "demo"), (data["tool"]["pytest"]["other"], data["project"]["name"]))

    def test_a_table_written_as_dotted_keys_at_the_top_is_put_back(self) -> None:
        ok, data, _ = self.restore('tool.pytest.ini_options.addopts = "-x"\n\n[project]\nname = "demo"\n')
        self.assertTrue(ok)
        self.assertEqual(LOCKED, data["tool"]["pytest"]["ini_options"])
        self.assertEqual("demo", data["project"]["name"])

    def test_a_table_inside_an_inline_value_of_a_parent_is_left_alone(self) -> None:
        text = '[tool]\npytest = { ini_options = { addopts = "-x" }, other = 1 }\n'
        ok, _, now = self.restore(text)
        self.assertFalse(ok)
        self.assertEqual(text, now)

    def test_an_indented_table_after_the_key_is_kept(self) -> None:
        ok, data, _ = self.restore('[tool.pytest.ini_options]\naddopts = "-x"\n\n  [project]\n  name = "demo"\n')
        self.assertTrue(ok)
        self.assertEqual(LOCKED, data["tool"]["pytest"]["ini_options"])
        self.assertEqual({"name": "demo"}, data["project"])

    def test_a_header_like_line_inside_a_multi_line_value_is_not_a_header(self) -> None:
        ok, data, _ = self.restore('[tool.pytest.ini_options]\naddopts = """\n[project]\n"""\n\n[project]\nname = "demo"\n')
        self.assertTrue(ok)
        self.assertEqual(LOCKED, data["tool"]["pytest"]["ini_options"])
        self.assertEqual({"name": "demo"}, data["project"])


class KeyRestoreGuardTests(TempRoot):
    def test_a_rewrite_that_loses_other_data_is_not_written(self) -> None:
        path = self.root / "pytest.ini"
        text = b"[pytest]\naddopts = -x\n\n[other]\nx = 1\n"
        path.write_bytes(text)
        with patch.object(signatures, "_set_ini", return_value="[pytest]\naddopts = -q\n"):
            self.assertFalse(signatures.restore_key(self.root, "pytest.ini", "pytest", {"addopts": "-q"}))
        self.assertEqual(text, path.read_bytes())

    def test_a_key_file_that_is_a_link_is_not_written_through(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "elsewhere.ini"
        text = b"[pytest]\naddopts = -x\n"
        target.write_bytes(text)
        try:
            os.symlink(target, self.root / "pytest.ini")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot make a symlink here: {exc}")
        self.assertFalse(signatures.restore_key(self.root, "pytest.ini", "pytest", {"addopts": "-q"}))
        self.assertEqual(text, target.read_bytes())


class SignatureTests(TempRoot):
    @unittest.skipIf(sys.version_info < (3, 12), "type parameters need Python 3.12 or later")
    def test_python_type_parameters_are_part_of_the_signature(self) -> None:
        (self.root / "gen.py").write_bytes(b"def f[T: int](x: T) -> T:\n    return x\n\nclass Box[T]:\n    pass\n")
        self.assertEqual("def f[T: int](x: T) -> T", signatures.take(self.root, "gen", "f")["signature"])
        self.assertEqual("class Box[T]", signatures.take(self.root, "gen", "Box")["signature"])

    def test_a_plain_python_function_keeps_its_signature(self) -> None:
        (self.root / "plain.py").write_bytes(b"def g(a, b=1) -> int:\n    return a\n")
        self.assertEqual("def g(a, b=1) -> int", signatures.take(self.root, "plain", "g")["signature"])

    def test_whitespace_inside_a_typescript_literal_type_is_kept(self) -> None:
        one = signatures._ts_declaration('export declare type T = "a  b";\n', "T")
        two = signatures._ts_declaration('export declare   type T =\n  "a b";\n', "T")
        self.assertEqual(['export declare type T = "a  b";'], one)
        self.assertEqual(['export declare type T = "a b";'], two)


FAKE_TSC = r'''
import sys
from pathlib import Path
args = sys.argv[1:]
with open(sys.argv[0] + ".log", "a", encoding="utf-8") as log:
    log.write(" ".join(a for a in args if a.startswith("--ignore")) + "|\n")
if "--ignoreConfig" not in args and Path("tsconfig.json").is_file():
    print("error TS5112: tsconfig.json is present but will not be loaded if files are specified on commandline. "
          "Use '--ignoreConfig' to skip this error.")
    sys.exit(1)
out = Path(args[args.index("--outDir") + 1])
rel = Path([a for a in args if a.endswith(".ts")][-1]).relative_to(Path(args[args.index("--rootDir") + 1]))
dest = out / rel.parent / (rel.name.rsplit(".", 1)[0] + ".d.ts")
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text("export declare function add(a: number, b: number): number;\n", encoding="utf-8")
'''


class FakeTscTests(TempRoot):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "src").mkdir()
        (self.root / "src" / "calc.ts").write_bytes(b"export function add(a: number, b: number): number { return a + b; }\n")
        bin_dir = self.root / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        script = self.root / "fake_tsc.py"
        script.write_text(FAKE_TSC, encoding="utf-8")
        self.log = Path(str(script) + ".log")
        if os.name == "nt":
            (bin_dir / "tsc.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            tsc = bin_dir / "tsc"
            tsc.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            tsc.chmod(0o755)

    def test_with_a_tsconfig_tsc_is_asked_again_with_ignore_config(self) -> None:
        (self.root / "tsconfig.json").write_bytes(b"{}\n")
        got = signatures.take(self.root, "src/calc.ts", "add")
        self.assertEqual("export declare function add(a: number, b: number): number;", got["signature"])
        self.assertEqual(["|", "--ignoreConfig|"], self.log.read_text(encoding="utf-8").splitlines())

    def test_without_a_tsconfig_tsc_is_run_once(self) -> None:
        got = signatures.take(self.root, "src/calc.ts", "add")
        self.assertEqual("export declare function add(a: number, b: number): number;", got["signature"])
        self.assertEqual(["|"], self.log.read_text(encoding="utf-8").splitlines())


class LockCopyTests(TempRoot):
    def setUp(self) -> None:
        super().setUp()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {locks.HOME_ENV: home.name})
        env.start()
        self.addCleanup(env.stop)
        init_git_repo(self.root)

    def run_state(self, targets: list[str], own: dict[str, str]) -> dict:
        run = state.new_run_state(self.root, self.root / "goal.md", None)
        run["acceptance_first"] = True
        run["pbis"] = [
            {"id": "PBI-1", "kind": "acceptance", "targets": targets, "acceptance_files": own, "status": "passed"},
            {"id": "PBI-2", "kind": "build", "targets": ["app.py"], "status": "pending"},
        ]
        run["scenarios"] = [{"id": rid, "mode": "normal", "cases": [{"id": f"{rid}-1", "pbi": "PBI-2"}]} for rid in own]
        return run

    def locked(self) -> tuple[dict, dict]:
        (self.root / "acc").mkdir()
        (self.root / "acc" / "R1.json").write_bytes(b"{}\n")
        run = self.run_state(["acc/R1.json"], {"R1": "acc/R1.json"})
        run["interfaces"] = []
        locks.lock(self.root, run)
        return run, json.loads((self.root / locks.LIST_PATH).read_text(encoding="utf-8"))

    def test_a_copy_that_does_not_match_its_hash_is_not_used_and_the_lock_commit_list_is(self) -> None:
        run, listed = self.locked()
        copy = Path(run["lock"]["outside"]) / locks.COPY_NAME
        copy.write_bytes(json.dumps({**listed, "signatures": {}, "keys": {}}).encode("utf-8"))
        self.assertEqual(listed, locks.locked_list(self.root, run))
        self.assertEqual(listed["keys"], locks.guard_entry(self.root, run)["keys"])

    def test_a_lock_whose_lists_both_fail_the_hash_is_a_fault(self) -> None:
        run, _ = self.locked()
        run["lock"]["sha256"] = hashlib.sha256(b"something else").hexdigest()
        with self.assertRaises(state.StateError):
            locks.locked_list(self.root, run)
        with self.assertRaises(state.StateError):
            locks.guard_entry(self.root, run)

    def test_an_unreadable_copy_is_a_lock_fault_not_an_exception(self) -> None:
        run, _ = self.locked()
        copy = Path(run["lock"]["outside"]) / locks.COPY_NAME
        real = Path.read_bytes

        def read_bytes(self: Path) -> bytes:
            if self == copy:
                raise PermissionError("denied")
            return real(self)

        with patch.object(Path, "read_bytes", read_bytes):
            problem = locks.copy_problem(self.root, run)
        self.assertIsNotNone(problem)
        self.assertIn("読めない", problem)

    def test_a_locked_path_that_looks_like_a_glob_takes_only_itself(self) -> None:
        (self.root / "acc").mkdir()
        (self.root / "acc" / "case[1].json").write_bytes(b"{}\n")
        (self.root / "acc" / "case1.json").write_bytes(b"other\n")
        commit = locks._commit_only(self.root, ["acc/case[1].json"], "lock")
        self.assertEqual(["acc/case[1].json"], git(self.root, "show", "--name-only", "--format=", commit).splitlines())
        self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))
        self.assertEqual(["acc/case[1].json"], sorted(locks.tree_blobs(self.root, commit, ["acc/case[1].json"])))
        (self.root / "acc" / "case[1].json").write_bytes(b"changed\n")
        (self.root / "acc" / "case1.json").write_bytes(b"other, changed\n")
        locks.restore(self.root, {"lock": {"commit": commit, "files": ["acc/case[1].json"]}}, ["acc/case[1].json"])
        self.assertEqual(b"{}\n", (self.root / "acc" / "case[1].json").read_bytes().replace(b"\r\n", b"\n"))
        self.assertEqual(b"other, changed\n", (self.root / "acc" / "case1.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
