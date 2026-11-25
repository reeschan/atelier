"""The machine checks of step definitions, run by the Red check on Python and JS/TS acceptance test files.

The checks read the file with regular expressions and a light parse (Python's ast; for JS/TS a scanner that masks
comments and strings and matches brackets). No outside tool is run. A file passes when:

- it imports only the modules and names in the plan's interfaces (third-party and test support code are free);
- each Then (a then step, a then* helper, a test function) has an assert or an expect;
- it does not mock an interface (patch, monkeypatch, vi.mock, jest.spyOn, reassigning the imported name);
- it does not swallow a broad exception (except: / except Exception without raise, a catch that neither rethrows
  nor tells the errors apart, an empty .catch callback);
- undefined and pending steps still fail (no strict: false or --no-strict in the runner's config, no hook that
  skips an undefined step, no step that returns skipped or pending).

Files in other languages are skipped; the languages skipped go to the plan approval page and the report.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Any

PYTHON = "Python"
JS = "JS/TS"
CHECKED_LANGUAGES = (PYTHON, JS)
LANGUAGES = {
    "py": PYTHON,
    "js": JS, "jsx": JS, "mjs": JS, "cjs": JS, "ts": JS, "tsx": JS, "mts": JS, "cts": JS,
    "go": "Go", "rs": "Rust", "rb": "Ruby", "java": "Java", "kt": "Kotlin", "kts": "Kotlin", "php": "PHP",
    "cs": "C#", "fs": "F#", "swift": "Swift", "scala": "Scala", "ex": "Elixir", "exs": "Elixir", "dart": "Dart",
    "vue": "Vue", "svelte": "Svelte", "c": "C", "cc": "C++", "cpp": "C++", "lua": "Lua", "clj": "Clojure",
}
_JS_EXTENSIONS = tuple(ext for ext, lang in LANGUAGES.items() if lang == JS)

RULES = {
    "syntax": "構文を読めない",
    "import": "公開の口でないものを import している",
    "then": "Then に assert も expect も無い",
    "mock": "公開の口を mock している",
    "except": "広い例外を握りつぶしている",
    "undefined": "未定義のステップが失敗にならない",
}

# Directory names and file names that hold test code; importing them is free.
TEST_DIRS = frozenset({
    "test", "tests", "__tests__", "spec", "specs", "features", "acceptance", "e2e", "steps", "step_definitions",
    "support", "fixtures", "testing",
})
_TEST_FILE = re.compile(r"(^test_)|(^conftest$)|([_.-](test|tests|spec|steps)$)", re.IGNORECASE)
_THEN_NAME = re.compile(r"^then(_|[A-Z]|$)")
_PY_ASSERT_CALL = re.compile(r"^(assert|expect)", re.IGNORECASE)
_PY_TEST_ASSERT_CALL = re.compile(r"^(assert|expect|then(_|[A-Z]|$))", re.IGNORECASE)
_BROAD = frozenset({"Exception", "BaseException"})
_SKIP_HOOKS = frozenset({"pytest_bdd_step_func_lookup_error", "before_step", "before_scenario", "before_feature", "before_all"})
_SKIP_CALLS = frozenset({"skip", "xfail", "mark_skipped", "importorskip"})
_STEP_OWNERS = frozenset({"step", "scenario", "feature"})

_STRICT_OFF = re.compile(r"--no-strict\b|(?<![\w-])[\"']?strict[\"']?\s*[:=]\s*[\"']?(?:false|no|off|0)\b", re.IGNORECASE)
_CUCUMBER_CONFIGS = (
    "cucumber.js", "cucumber.cjs", "cucumber.mjs", "cucumber.ts", "cucumber.json", "cucumber.yaml", "cucumber.yml",
    "package.json",
)

_JS_IMPORT_FROM = re.compile(
    r"(?<![\w$.])(?:import|export)\s+(?:type\s+)?(?P<clause>[^;'\"`]*?)\s*\bfrom\s*(?P<q>['\"])(?P<spec>[^'\"]+)(?P=q)"
)
_JS_IMPORT_BARE = re.compile(r"(?<![\w$.])import\s*(?P<q>['\"])(?P<spec>[^'\"]+)(?P=q)")
_JS_DYNAMIC = re.compile(r"(?<![\w$.])(?:import|require)\s*\(\s*(?P<q>['\"`])(?P<spec>[^'\"`]+)(?P=q)\s*\)")
_JS_REQUIRE_BIND = re.compile(
    r"(?<![\w$.])(?:const|let|var)\s+(?P<bind>\{[^}]*\}|[\w$]+)\s*=\s*require\s*\(\s*(?P<q>['\"])(?P<spec>[^'\"]+)(?P=q)\s*\)"
)
_JS_ASSERT = re.compile(r"(?<![\w$.])(?:expect|assert)(?:\s*\.\s*[\w$]+)*\s*\(|\.\s*should\b|(?<![\w$.])assert\s*\.")
_JS_THEN_HELPER_CALL = re.compile(r"(?<![\w$.])then[A-Z_$][\w$]*\s*\(")
_JS_THEN_STEP = re.compile(r"(?<![\w$.])Then\s*\(")
_JS_TEST = re.compile(r"(?<![\w$.])(?:test|it)\s*(?P<chain>(?:\.\s*[A-Za-z_$][\w$]*\s*)*)\(")
_JS_THEN_FUNCTION = re.compile(r"(?<![\w$.])function\s+(?P<name>then[A-Z_$][\w$]*)\s*\(")
_JS_THEN_BINDING = re.compile(r"(?<![\w$.])(?:const|let|var)\s+(?P<name>then[A-Z_$][\w$]*)\s*(?::[^=]*)?=(?![=>])")
_JS_MOCK_MODULE = re.compile(
    r"(?<![\w$.])(?:vi|jest)\s*\.\s*(?:mock|doMock|unstable_mockModule|setMock|createMockFromModule)\s*\(\s*"
    r"(?P<q>['\"`])(?P<spec>[^'\"`]+)(?P=q)"
)
_JS_SPY = re.compile(
    r"(?<![\w$.])(?:(?:vi|jest|sinon|td)\s*\.\s*(?:spyOn|stub|replace)|Object\s*\.\s*defineProperty)\s*\(\s*(?P<obj>[\w$]+)"
)
_JS_CATCH = re.compile(r"(?<![\w$.])catch\s*(?:\([^)]*\))?\s*\{")
_JS_PROMISE_CATCH = re.compile(r"\.\s*catch\s*\(")
_JS_EMPTY_CALLBACK = re.compile(
    r"(?:async\s+)?(?:\(\s*[\w$]*\s*\)|[\w$]+)\s*=>\s*(?:\{\s*\}|undefined|null|void\s+0)"
    r"|(?:async\s+)?function\s*[\w$]*\s*\([^)]*\)\s*\{\s*\}"
)
_JS_RETURN_SKIPPED = re.compile(r"(?<![\w$.])return\s+(?P<q>['\"])(?:skipped|pending)(?P=q)")
_JS_RETHROW = re.compile(r"(?<![\w$.])throw\b|\binstanceof\b")


def language_of(path: str) -> str | None:
    """The language of a file from its extension, or None when it is not a source file."""
    name = PurePosixPath(str(path).replace("\\", "/")).name
    _, dot, ext = name.rpartition(".")
    return LANGUAGES.get(ext.lower()) if dot else None


def skipped_languages(paths: list[str]) -> dict[str, list[str]]:
    """Language -> the paths in a language the checks skip (a source language other than Python and JS/TS)."""
    out: dict[str, list[str]] = {}
    for path in paths:
        lang = language_of(path)
        if lang and lang not in CHECKED_LANGUAGES and path not in out.get(lang, []):
            out.setdefault(lang, []).append(path)
    return out


def interface_languages(interfaces: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The languages the checks skip, as the plan's interfaces' module extensions show them."""
    modules = [str(i.get("module") or "").strip() for i in interfaces or [] if isinstance(i, dict)]
    return skipped_languages([m for m in modules if m])


def skipped_text(skipped: dict[str, list[str]]) -> str:
    """Go（pkg/add.go）、Ruby（lib/x.rb） or なし."""
    return "、".join(f"{lang}（{'、'.join(paths)}）" for lang, paths in sorted(skipped.items())) or "なし"


_RUNNER_CONFIG = re.compile(r"^(?:vitest|vite|jest|cucumber|playwright)\.config\.[cm]?[jt]s$")


def is_runner_config(path: str) -> bool:
    """True for a test runner's config file; the checks read it for strict, not as step definitions."""
    name = PurePosixPath(str(path).replace("\\", "/")).name
    return name in _CUCUMBER_CONFIGS or bool(_RUNNER_CONFIG.match(name))


def is_test_support(path: str) -> bool:
    """True when a repository path is test code: under a test directory, or named like a test or conftest."""
    parts = PurePosixPath(path).parts
    if any(part.lower() in TEST_DIRS for part in parts[:-1]):
        return True
    if not parts:
        return False
    last = parts[-1]
    stem = last.rsplit(".", 1)[0] if language_of(last) else last
    return last.lower() in TEST_DIRS or bool(_TEST_FILE.search(stem))


class Interfaces:
    """The plan's public interfaces, keyed for Python (dotted module) and JS/TS (path without extension)."""

    def __init__(self, interfaces: list[dict[str, Any]]) -> None:
        self.py: dict[str, set[str]] = {}
        self.js: dict[str, set[str]] = {}
        for item in interfaces or []:
            if not isinstance(item, dict):
                continue
            module = str(item.get("module") or "").strip().replace("\\", "/")
            name = str(item.get("name") or "").strip().split(".")[0]
            if not module:
                continue
            lang = language_of(module)
            if lang in (None, PYTHON):
                for dotted in _py_modules(module, lang):
                    self.py.setdefault(dotted, set()).add(name)
            if lang in (None, JS):
                self.js.setdefault(_js_module(module, lang), set()).add(name)

    def py_key(self, dotted: str) -> str | None:
        return dotted if dotted in self.py else None

    def js_key(self, stem: str) -> str | None:
        for key in self.js:
            if stem == key or stem.endswith("/" + key) or key.endswith("/" + stem):
                return key
        return None


def _py_modules(module: str, lang: str | None) -> list[str]:
    path = module.removeprefix("./").strip("/")
    if lang == PYTHON:
        path = path[: -len(".py")]
    parts = [p for p in (path.split("/") if "/" in path or lang == PYTHON else path.split(".")) if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    dotted = ".".join(parts)
    out = [dotted] if dotted else []
    if dotted.startswith("src."):
        out.append(dotted[len("src."):])
    return out


def _strip_js(path: str) -> str:
    path = path.removeprefix("./").strip("/")
    head, dot, ext = path.rpartition(".")
    if dot and "/" not in ext and ext.lower() in _JS_EXTENSIONS:
        path = head
    if path.endswith("/index"):
        path = path[: -len("/index")]
    return path


def _js_module(module: str, lang: str | None) -> str:
    if lang is None and "/" not in module:
        module = module.replace(".", "/")
    return _strip_js(module)


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# --- Python ---------------------------------------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else ""
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _last(node: ast.AST) -> str:
    return _dotted(node).rsplit(".", 1)[-1]


def _raises(nodes: list[ast.stmt]) -> bool:
    return any(isinstance(n, ast.Raise) for stmt in nodes for n in ast.walk(stmt))


def _fails(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    return isinstance(node, ast.Call) and (bool(_PY_ASSERT_CALL.match(_last(node.func))) or _last(node.func) == "fail")


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return []
    items = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [_last(item) for item in items]


class _PythonCheck:
    def __init__(self, root: Path, rel: str, ifaces: Interfaces) -> None:
        self.root = root
        self.rel = rel
        self.ifaces = ifaces
        self.module_aliases: dict[str, str] = {}
        self.name_aliases: set[str] = set()
        self.problems: list[tuple[int | None, str, str]] = []

    def add(self, node: ast.AST | None, rule: str, detail: str) -> None:
        self.problems.append((getattr(node, "lineno", None), rule, detail))

    def run(self, text: str) -> list[tuple[int | None, str, str]]:
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            self.problems.append((exc.lineno, "syntax", str(exc.msg)))
            return self.problems
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._import(node)
            elif isinstance(node, ast.ImportFrom):
                self._import_from(node)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self._attribute(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(node)
            elif isinstance(node, ast.Call):
                self._call(node)
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                self._assign(node)
            elif isinstance(node, ast.ExceptHandler):
                self._handler(node)
        return self.problems

    # imports

    def _project_path(self, dotted: str) -> str | None:
        """The repository path a dotted module is in, when it is the project's own code."""
        parts = dotted.split(".")
        top = parts[0]
        for prefix in ("", "src/"):
            base = self.root / f"{prefix}{top}"
            if base.with_suffix(".py").is_file() or base.is_dir():
                return prefix + "/".join(parts)
        if any(key.split(".")[0] == top for key in self.ifaces.py):
            return "/".join(parts)
        return None

    def _import(self, node: ast.Import) -> None:
        for alias in node.names:
            key = self.ifaces.py_key(alias.name)
            if key:
                self.module_aliases[alias.asname or alias.name] = key
                continue
            path = self._project_path(alias.name)
            if path is not None and not is_test_support(path):
                self.add(node, "import", f"import {alias.name}")

    def _from_interface(self, node: ast.ImportFrom, module: str, key: str) -> None:
        for alias in node.names:
            if alias.name == "*":
                self.add(node, "import", f"from {module} import *")
            elif alias.name in self.ifaces.py[key]:
                self.name_aliases.add(alias.asname or alias.name)
            elif self.ifaces.py_key(f"{key}.{alias.name}"):
                self.module_aliases[alias.asname or alias.name] = f"{key}.{alias.name}"
            else:
                self.add(node, "import", f"from {module} import {alias.name}")

    def _import_from(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            base = PurePosixPath(self.rel).parent
            for _ in range(node.level - 1):
                base = base.parent
            path = (base / module.replace(".", "/")).as_posix() if module else base.as_posix()
            dotted_forms = _py_modules(path, None) if path not in ("", ".") else []
            key = next((self.ifaces.py_key(d) for d in dotted_forms if self.ifaces.py_key(d)), None)
            if key:
                self._from_interface(node, "." * node.level + module, key)
            elif not is_test_support(path):
                for alias in node.names:
                    sub = next((self.ifaces.py_key(f"{d}.{alias.name}") for d in dotted_forms if self.ifaces.py_key(f"{d}.{alias.name}")), None)
                    if sub:
                        self.module_aliases[alias.asname or alias.name] = sub
                    else:
                        self.add(node, "import", f"from {'.' * node.level}{module} import {alias.name}")
            return
        key = self.ifaces.py_key(module)
        if key:
            self._from_interface(node, module, key)
            return
        path = self._project_path(module)
        for alias in node.names:
            sub = self.ifaces.py_key(f"{module}.{alias.name}")
            if sub:
                self.module_aliases[alias.asname or alias.name] = sub
            elif path is not None and not is_test_support(path):
                self.add(node, "import", f"from {module} import {alias.name}")

    def _attribute(self, node: ast.Attribute) -> None:
        owner = _dotted(node.value)
        key = self.module_aliases.get(owner)
        if key and node.attr not in self.ifaces.py.get(key, set()) and not self.ifaces.py_key(f"{key}.{node.attr}"):
            self.add(node, "import", f"{owner}.{node.attr}")

    # then steps and tests

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        decorators = [_last(d) for d in node.decorator_list]
        if node.name in _SKIP_HOOKS:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and _last(inner.func) in _SKIP_CALLS:
                    self.add(inner, "undefined", f"{node.name} がステップを飛ばす")
        is_then = any(d in ("then", "Then") for d in decorators) or bool(_THEN_NAME.match(node.name))
        is_test = node.name.startswith("test") and not any(d in ("scenario", "scenarios") for d in decorators)
        if not (is_then or is_test):
            return
        pattern = _PY_ASSERT_CALL if is_then else _PY_TEST_ASSERT_CALL
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assert):
                return
            if isinstance(inner, ast.Raise) and inner.exc is not None and _last(inner.exc) == "AssertionError":
                return
            if isinstance(inner, ast.Call) and (pattern.match(_last(inner.func)) or _last(inner.func) in ("fail", "raises")):
                return
        self.add(node, "then", node.name)

    # mocks

    def _names_interface(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            for key in self.ifaces.py:
                if text == key or text.startswith(key + "."):
                    return text
            return None
        dotted = _dotted(node)
        if dotted and (dotted in self.module_aliases or dotted in self.name_aliases or dotted.split(".")[0] in self.name_aliases):
            return dotted
        return None

    def _call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        last = dotted.rsplit(".", 1)[-1]
        if last == "suppress" and any(_last(arg) in _BROAD for arg in node.args):
            self.add(node, "except", f"{dotted}({', '.join(_last(a) for a in node.args)})")
            return
        is_mock = last in ("patch", "setattr", "setitem", "delattr", "delitem", "spy") or any(
            dotted == f"patch.{m}" or dotted.endswith(f".patch.{m}") for m in ("object", "multiple", "dict")
        )
        if not is_mock:
            return
        for arg in [*node.args, *(k.value for k in node.keywords if k.arg in ("target", "name"))]:
            hit = self._names_interface(arg)
            if hit:
                self.add(node, "mock", f"{dotted}({hit})")
                return

    def _assign(self, node: ast.Assign | ast.AugAssign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for item in target.elts if isinstance(target, ast.Tuple) else [target]:
                if isinstance(item, ast.Name) and item.id in self.name_aliases:
                    self.add(node, "mock", f"{item.id} を置き換えている")
                elif isinstance(item, ast.Attribute):
                    owner = _dotted(item.value)
                    if owner in self.module_aliases or owner.split(".")[0] in self.name_aliases:
                        self.add(node, "mock", f"{owner}.{item.attr} を置き換えている")
                    elif item.attr == "status" and owner.rsplit(".", 1)[-1] in _STEP_OWNERS:
                        self.add(node, "undefined", f"{owner}.status を書き換えている")
                elif isinstance(item, ast.Subscript) and _dotted(item.value) == "sys.modules":
                    hit = self._names_interface(item.slice)
                    if hit:
                        self.add(node, "mock", f"sys.modules[{hit!r}] を置き換えている")

    # exceptions

    def _handler(self, node: ast.ExceptHandler) -> None:
        names = _handler_names(node)
        # A handler that raises again or fails the test does not swallow the exception.
        if _raises(node.body) or any(_fails(n) for stmt in node.body for n in ast.walk(stmt)):
            return
        if node.type is None:
            self.add(node, "except", "except:")
        elif any(name in _BROAD for name in names):
            self.add(node, "except", f"except {', '.join(names)}")
        elif "StepDefinitionNotFoundError" in names:
            self.add(node, "undefined", "StepDefinitionNotFoundError を握りつぶしている")


# --- JS/TS ----------------------------------------------------------------------------------------------------------


def _mask(text: str) -> tuple[str, str]:
    """(text with comments blanked, text with comments and string contents blanked). Offsets are kept."""
    no_comments = list(text)
    code = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] in "/*":
            end = text.find("\n", i) if text[i + 1] == "/" else text.find("*/", i + 2)
            end = n if end < 0 else (end if text[i + 1] == "/" else end + 2)
            for j in range(i, end):
                if text[j] != "\n":
                    no_comments[j] = code[j] = " "
            i = end
            continue
        if ch in "'\"`":
            j = i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n" and ch != "`":
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if text[k] != "\n":
                    code[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(no_comments), "".join(code)


_PAIRS = {"(": ")", "{": "}", "[": "]"}


def _close(code: str, start: int) -> int:
    """The index of the bracket that closes the one at start (or the end of the text)."""
    stack: list[str] = []
    for i in range(start, len(code)):
        ch = code[i]
        if ch in _PAIRS:
            stack.append(_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return i
    return len(code) - 1


def _next_char(code: str, index: int) -> int:
    while index < len(code) and code[index].isspace():
        index += 1
    return index


class _JsCheck:
    def __init__(self, root: Path, rel: str, ifaces: Interfaces) -> None:
        self.root = root
        self.rel = rel
        self.ifaces = ifaces
        self.module_aliases: dict[str, str] = {}
        self.name_aliases: set[str] = set()
        self.problems: list[tuple[int | None, str, str]] = []
        self.own_package = self._own_package()

    def _own_package(self) -> str:
        try:
            data = json.loads((self.root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(data.get("name") or "") if isinstance(data, dict) else ""

    def add(self, offset: int | None, rule: str, detail: str) -> None:
        self.problems.append((None if offset is None else _line(self.text, offset), rule, detail))

    def run(self, text: str) -> list[tuple[int | None, str, str]]:
        self.text = text
        self.plain, self.code = _mask(text)
        self._imports()
        self._members()
        self._thens()
        self._mocks()
        self._catches()
        self._undefined()
        return self.problems

    def _in_code(self, offset: int) -> bool:
        return self.code[offset] == self.plain[offset]

    # imports

    def _target(self, spec: str) -> tuple[str, str]:
        """(kind, path): kind is project, external or outside (a relative path out of the repository)."""
        if spec.startswith("."):
            path = posixpath.normpath(posixpath.join(posixpath.dirname(self.rel), spec))
            return ("outside", path) if path.startswith("..") else ("project", _strip_js(path))
        if spec.startswith("/"):
            return "project", _strip_js(spec)
        if spec.startswith(("@/", "~/", "#")):
            return "project", _strip_js(spec.lstrip("@~#/"))
        if self.own_package and (spec == self.own_package or spec.startswith(self.own_package + "/")):
            return "project", _strip_js(spec[len(self.own_package):] or "index")
        return "external", spec

    def _interface_of(self, spec: str) -> str | None:
        kind, path = self._target(spec)
        if kind == "external":
            return path if path in self.ifaces.js else None
        if kind == "outside":
            return None
        return self.ifaces.js_key(path) or (self.ifaces.js_key("src/" + path) if spec.startswith(("@/", "~/", "#")) else None)

    def _check_spec(self, offset: int, spec: str, clause: str | None) -> None:
        key = self._interface_of(spec)
        if key is not None:
            if clause is not None:
                self._bind(offset, spec, key, clause)
            return
        kind, path = self._target(spec)
        if kind == "outside" or (kind == "project" and not is_test_support(path)):
            self.add(offset, "import", spec)

    def _bind(self, offset: int, spec: str, key: str, clause: str) -> None:
        names = self.ifaces.js[key]
        braces = re.search(r"\{([^}]*)\}", clause)
        if braces:
            for item in braces.group(1).split(","):
                item = re.sub(r"^\s*type\s+", "", item).strip()
                if not item:
                    continue
                name, _, alias = item.partition(" as ")
                name, alias = name.strip(), (alias or name).strip()
                if name in names:
                    self.name_aliases.add(alias)
                else:
                    self.add(offset, "import", f"{name} from {spec}")
        rest = re.sub(r"\{[^}]*\}", "", clause)
        for item in rest.split(","):
            item = item.strip()
            star = re.match(r"\*\s+as\s+([\w$]+)$", item)
            if star:
                self.module_aliases[star.group(1)] = key
            elif re.fullmatch(r"[\w$]+", item):
                self.module_aliases[item] = key

    def _imports(self) -> None:
        seen: set[int] = set()
        for match in _JS_IMPORT_FROM.finditer(self.plain):
            if self._in_code(match.start()):
                seen.add(match.start("spec"))
                clause = match.group("clause")
                self._check_spec(match.start(), match.group("spec"), clause if match.group(0).lstrip().startswith("import") else None)
        for match in _JS_REQUIRE_BIND.finditer(self.plain):
            if self._in_code(match.start()):
                seen.add(match.start("spec"))
                bind = match.group("bind")
                self._check_spec(match.start(), match.group("spec"), bind.replace(":", " as ") if bind.startswith("{") else bind)
        for pattern in (_JS_IMPORT_BARE, _JS_DYNAMIC):
            for match in pattern.finditer(self.plain):
                if self._in_code(match.start()) and match.start("spec") not in seen:
                    self._check_spec(match.start(), match.group("spec"), None)

    def _members(self) -> None:
        for alias, key in self.module_aliases.items():
            for match in re.finditer(rf"(?<![\w$.]){re.escape(alias)}\s*\.\s*([\w$]+)", self.code):
                if match.group(1) not in self.ifaces.js[key]:
                    self.add(match.start(), "import", f"{alias}.{match.group(1)}")

    # then steps, tests and then helpers

    def _asserts(self, body: str, helpers: bool) -> bool:
        return bool(_JS_ASSERT.search(body) or (helpers and _JS_THEN_HELPER_CALL.search(body)))

    def _thens(self) -> None:
        code = self.code
        for match in _JS_THEN_STEP.finditer(code):
            start = match.end() - 1
            if not self._asserts(code[start:_close(code, start) + 1], False):
                self.add(match.start(), "then", "Then(" + self._first_string(start) + ")")
        for match in _JS_TEST.finditer(code):
            chain = re.findall(r"[\w$]+", match.group("chain"))
            if {"skip", "todo"} & set(chain):
                continue
            start = match.end() - 1
            end = _close(code, start)
            body = code[start:end + 1]
            nxt = _next_char(code, end + 1)
            while nxt < len(code) and code[nxt] == "(":
                end = _close(code, nxt)
                body += code[nxt:end + 1]
                nxt = _next_char(code, end + 1)
            if not self._asserts(body, True):
                self.add(match.start(), "then", "test(" + self._first_string(start) + ")")
        for match in _JS_THEN_FUNCTION.finditer(code):
            params_end = _close(code, match.end() - 1)
            brace = code.find("{", params_end)
            body = code[brace:_close(code, brace) + 1] if brace >= 0 else ""
            if not self._asserts(body, False):
                self.add(match.start(), "then", match.group("name"))
        for match in _JS_THEN_BINDING.finditer(code):
            body = self._binding_body(match.end())
            if not self._asserts(body, False):
                self.add(match.start(), "then", match.group("name"))

    def _binding_body(self, index: int) -> str:
        code = self.code
        arrow = code.find("=>", index)
        function = re.compile(r"\s*(?:async\s+)?function\b").match(code, index)
        if function:
            brace = code.find("{", _close(code, code.find("(", function.end())))
            return code[brace:_close(code, brace) + 1] if brace >= 0 else ""
        if arrow < 0:
            return ""
        start = _next_char(code, arrow + 2)
        if start < len(code) and code[start] == "{":
            return code[start:_close(code, start) + 1]
        end = code.find("\n", start)
        return code[start:end if end >= 0 else len(code)]

    def _first_string(self, start: int) -> str:
        found = re.compile(r"\s*(['\"`])([^'\"`]*)\1").match(self.plain, start + 1)
        return repr(found.group(2)) if found else "…"

    # mocks

    def _mocks(self) -> None:
        for match in _JS_MOCK_MODULE.finditer(self.plain):
            if self._in_code(match.start()) and self._interface_of(match.group("spec")) is not None:
                self.add(match.start(), "mock", match.group(0).split("(")[0].replace(" ", "") + f"({match.group('spec')!r})")
        for match in _JS_SPY.finditer(self.code):
            obj = match.group("obj")
            if obj in self.module_aliases or obj in self.name_aliases:
                self.add(match.start(), "mock", match.group(0).split("(")[0].replace(" ", "") + f"({obj})")
        for alias in [*self.module_aliases, *self.name_aliases]:
            for match in re.finditer(rf"(?<![\w$.]){re.escape(alias)}(?:\s*\.\s*[\w$]+)*\s*=(?![=>])", self.code):
                if re.search(r"(?<![\w$.])(?:const|let|var)\s+$", self.code[max(0, match.start() - 12):match.start()]):
                    continue
                self.add(match.start(), "mock", f"{match.group(0).rstrip('= ').strip()} を置き換えている")

    # exceptions

    def _catches(self) -> None:
        code = self.code
        for match in _JS_CATCH.finditer(code):
            brace = match.end() - 1
            body = code[brace + 1:_close(code, brace)]
            if not (_JS_RETHROW.search(body) or _JS_ASSERT.search(body)):
                self.add(match.start(), "except", "catch" + (" {}" if not body.strip() else " で投げ直さず、例外も見分けない"))
        for match in _JS_PROMISE_CATCH.finditer(code):
            start = match.end() - 1
            inner = code[start + 1:_close(code, start)].strip()
            if not inner or _JS_EMPTY_CALLBACK.fullmatch(inner):
                self.add(match.start(), "except", f".catch({inner})")

    # undefined and pending steps

    def _undefined(self) -> None:
        for match in _JS_RETURN_SKIPPED.finditer(self.plain):
            if self._in_code(match.start()):
                self.add(match.start(), "undefined", match.group(0))
        for match in _STRICT_OFF.finditer(self.plain):
            if self._in_code(match.start()) or match.group(0).startswith("--"):
                self.add(match.start(), "undefined", match.group(0).strip())
        directory = PurePosixPath(self.rel).parent
        for folder in [directory, *directory.parents]:
            for name in _CUCUMBER_CONFIGS:
                config = (folder / name).as_posix().removeprefix("./")
                if config == self.rel:
                    continue
                try:
                    text = (self.root / config).read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                plain, code = _mask(text) if language_of(name) == JS else (text, text)
                for match in _STRICT_OFF.finditer(plain):
                    if code[match.start()] == plain[match.start()] or match.group(0).startswith("--") or language_of(name) != JS:
                        self.problems.append((None, "undefined", f"{config}:{_line(text, match.start())} の {match.group(0).strip()}"))


# --- entry point ----------------------------------------------------------------------------------------------------


def check_file(root: Path, rel: str, interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    """Check one file. Returns {"file", "language", "checked", "problems"}; problems are reason lines."""
    rel = rel.replace("\\", "/").removeprefix("./")
    lang = language_of(rel)
    out: dict[str, Any] = {"file": rel, "language": lang, "checked": lang in CHECKED_LANGUAGES, "problems": []}
    if not out["checked"]:
        return out
    try:
        text = (Path(root) / rel).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        out["problems"] = [f"{rel}: {RULES['syntax']}（{exc}）"]
        return out
    ifaces = Interfaces(interfaces)
    found = _PythonCheck(Path(root), rel, ifaces).run(text) if lang == PYTHON else _JsCheck(Path(root), rel, ifaces).run(text)
    seen: set[str] = set()
    for line, rule, detail in found:
        where = f"{rel}:{line}" if line else rel
        reason = f"{where}: {RULES[rule]}（{detail}）"
        if reason not in seen:
            seen.add(reason)
            out["problems"].append(reason)
    return out
