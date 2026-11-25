"""The signatures of the public interfaces and the compared keys of the files workers also touch, for the lock.

Signatures are taken per language: Python with ast, TypeScript (and JavaScript) from tsc's declaration output, Go
with go doc. tsc and go are run with subprocess; nothing else is installed. An interface in another language, or
one whose tool is not there, is not locked: it is recorded as an interface whose signature cannot be taken.

The files workers also touch (package.json, pyproject.toml, the pytest and behave ini files) are not locked whole.
Only their compared keys are: the runner's settings and the lines of the commands that run the tests. A key's value
is read from the file (JSON, TOML or ini) and compared as data, so a change of layout elsewhere in the file is not a
change. A changed key is put back to its locked value by rewriting the key alone.
"""

from __future__ import annotations

import ast
import configparser
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ImportError:  # Python before 3.11: the TOML keys are not taken.
    tomllib = None  # type: ignore[assignment]

PYTHON = "Python"
TYPESCRIPT = "TypeScript"
JAVASCRIPT = "JavaScript"
GO = "Go"
_TS_EXT = {"ts": TYPESCRIPT, "tsx": TYPESCRIPT, "mts": TYPESCRIPT, "cts": TYPESCRIPT,
           "js": JAVASCRIPT, "jsx": JAVASCRIPT, "mjs": JAVASCRIPT, "cjs": JAVASCRIPT}
_TS_TRY = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_OTHER_EXT = {
    "rs": "Rust", "rb": "Ruby", "java": "Java", "kt": "Kotlin", "kts": "Kotlin", "php": "PHP", "cs": "C#", "fs": "F#",
    "swift": "Swift", "scala": "Scala", "ex": "Elixir", "exs": "Elixir", "dart": "Dart", "vue": "Vue",
    "svelte": "Svelte", "c": "C", "cc": "C++", "cpp": "C++", "lua": "Lua", "clj": "Clojure",
}
TIMEOUT = 120

# Reasons an interface's signature is not taken.
UNSUPPORTED = "シグネチャを取り出せない言語"
NO_TOOL = "道具が無い"
NOT_FOUND = "見つからない"
FAILED = "取り出せなかった"

# The files workers also touch, and the keys of each that are locked (dotted paths; an ini key is a section).
KEY_FILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "package.json": ("json", ("scripts.test", "jest", "cucumber", "mocha", "ava")),
    "pyproject.toml": ("toml", ("tool.pytest.ini_options", "tool.behave")),
    "pytest.ini": ("ini", ("pytest",)),
    "tox.ini": ("ini", ("pytest",)),
    "setup.cfg": ("ini", ("tool:pytest",)),
    "behave.ini": ("ini", ("behave",)),
    ".behaverc": ("ini", ("behave",)),
}
_SCRIPT_RUN = re.compile(r"(?<![\w-])(npm|pnpm|yarn|bun)(?:\s+--?[\w-]+(?:=\S+)?)*\s+(?:run(?:-script)?\s+([\w:.@/-]+)|(test|t)(?![\w:.-]))")
_SCRIPT_BARE = re.compile(r"(?<![\w-])(?:pnpm|yarn)\s+([\w:.@/-]+)")


def _norm(path: str) -> str:
    return str(path).strip().replace("\\", "/").removeprefix("./").strip("/")


def interface_id(module: str, name: str) -> str:
    """The key of an interface in the lock list's signatures: <module>:<name>."""
    return f"{module}:{name}"


# ---------------------------------------------------------------- Python

_KEPT_DECORATORS = {"staticmethod", "classmethod", "property", "abstractmethod", "abc.abstractmethod", "overload",
                    "typing.overload", "functools.cached_property", "cached_property"}


def _py_decorators(node: ast.AST, keep_all: bool) -> list[str]:
    out = []
    for deco in getattr(node, "decorator_list", []):
        text = ast.unparse(deco)
        if keep_all or text in _KEPT_DECORATORS or text.rsplit(".", 1)[-1] in ("setter", "getter", "deleter"):
            out.append("@" + text)
    return out


def _py_function(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str = "") -> list[str]:
    head = f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}({ast.unparse(node.args)})"
    if node.returns is not None:
        head += f" -> {ast.unparse(node.returns)}"
    return [indent + line for line in [*_py_decorators(node, False), head]]


def _py_public(name: str) -> bool:
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def _py_class(node: ast.ClassDef) -> list[str]:
    bases = [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords]
    lines = [*_py_decorators(node, True), f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")]
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and _py_public(item.name):
            lines += _py_function(item, "    ")
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and _py_public(item.target.id):
            text = f"{item.target.id}: {ast.unparse(item.annotation)}"
            if item.value is not None:
                text += f" = {ast.unparse(item.value)}"
            lines.append("    " + text)
    return lines


def _py_find(body: list[ast.stmt], name: str) -> list[str]:
    head, _, rest = name.partition(".")
    lines: list[str] = []
    for node in body:
        if isinstance(node, ast.ClassDef) and node.name == head:
            lines += _py_find(node.body, rest) if rest else _py_class(node)
        elif rest:
            continue
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == head:
            lines += _py_function(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == head:
            lines.append(f"{head}: {ast.unparse(node.annotation)}")
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == head for t in node.targets):
            lines.append(head)
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name) == head for a in node.names):
            lines.append(ast.unparse(node))
        elif isinstance(node, ast.If):
            lines += _py_find(node.body + node.orelse, name)
        elif isinstance(node, ast.Try):
            lines += _py_find(node.body + node.orelse + node.finalbody + [s for h in node.handlers for s in h.body], name)
    return lines


def _py_candidates(module: str, explicit: bool) -> list[str]:
    path = _norm(module)
    if explicit:
        return [path]
    base = path if "/" in path else path.replace(".", "/")
    found = [f"{base}.py", f"{base}/__init__.py"]
    return found + [f"src/{p}" for p in found if not p.startswith("src/")]


def _python(root: Path, rel: str, name: str) -> dict[str, Any]:
    try:
        tree = ast.parse((root / rel).read_bytes(), filename=rel)
    except (OSError, SyntaxError, ValueError) as exc:
        return {"signature": None, "reason": f"{FAILED}: {type(exc).__name__}"}
    lines = _py_find(tree.body, name)
    return {"signature": "\n".join(lines)} if lines else {"signature": None, "reason": f"{NOT_FOUND}: {name}"}


# ---------------------------------------------------------------- TypeScript

def _tsc(root: Path) -> str | None:
    for local in ("tsc.cmd", "tsc"):
        path = root / "node_modules" / ".bin" / local
        if path.is_file():
            return str(path)
    return shutil.which("tsc")


def _ts_candidates(module: str, explicit: bool) -> list[str]:
    path = _norm(module)
    if explicit:
        return [path]
    return [path + ext for ext in _TS_TRY] + [f"{path}/index{ext}" for ext in _TS_TRY]


def _ts_statements(text: str) -> list[str]:
    """Split a declaration file into its top-level statements, without comments, whitespace collapsed."""
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            cur.append(" ")
            continue
        if ch in "\"'`":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            cur.append(text[i:j + 1])
            i = j + 1
            continue
        cur.append(ch)
        i += 1
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if depth:
            continue
        stmt = " ".join("".join(cur).split())
        block = re.match(r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:class|interface|namespace|module|enum|global)\b", stmt)
        if ch == ";" or (ch == "}" and block):
            if stmt.strip(";"):
                out.append(stmt)
            cur = []
    rest = " ".join("".join(cur).split())
    if rest:
        out.append(rest)
    return out


def _ts_declaration(text: str, name: str) -> list[str]:
    pattern = re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
        r"(?:function\s*\*?|class|interface|type|const|let|var|enum|namespace|module)\s+" + re.escape(name) + r"(?![\w$])"
    )
    return [s for s in _ts_statements(text) if pattern.match(s)]


def _typescript(root: Path, rel: str, name: str, tsc: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="atelier-tsc-") as tmp:
        args = [tsc, "--declaration", "--emitDeclarationOnly", "--skipLibCheck", "--allowJs", "--jsx", "preserve",
                "--rootDir", str(root), "--outDir", tmp, str(root / rel)]
        try:
            proc = subprocess.run(args, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"signature": None, "reason": f"{FAILED}: tsc ({type(exc).__name__})"}
        posix = PurePosixPath(rel)
        stem = posix.name.rsplit(".", 1)[0]
        outs = sorted(Path(tmp).rglob(f"{stem}.d.*ts"))
        same = [p for p in outs if p.parent.relative_to(tmp).as_posix() == posix.parent.as_posix()]
        chosen = (same or outs or [None])[0]
        if chosen is None:
            tail = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            return {"signature": None, "reason": f"{FAILED}: tsc ({tail[0][:200]})"}
        decls = _ts_declaration(chosen.read_text(encoding="utf-8", errors="replace"), name)
    return {"signature": "\n".join(decls)} if decls else {"signature": None, "reason": f"{NOT_FOUND}: {name}"}


# ---------------------------------------------------------------- Go

def _go_module_path(root: Path) -> str | None:
    try:
        text = (root / "go.mod").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*module\s+(\S+)", text, re.MULTILINE)
    return match.group(1).strip('"') if match else None


def _go_dir(root: Path, module: str, explicit: bool) -> str | None:
    path = _norm(module)
    if explicit:
        return PurePosixPath(path).parent.as_posix()
    mod = _go_module_path(root)
    if mod and (path == mod or path.startswith(mod + "/")):
        path = path[len(mod):].strip("/")
    target = root / path if path else root
    if target.is_dir() and any(p.suffix == ".go" for p in target.iterdir() if p.is_file()):
        return path or "."
    return None


def _go(root: Path, pkg_dir: str, name: str, go: str) -> dict[str, Any]:
    pkg = "." if pkg_dir in ("", ".") else "./" + pkg_dir
    try:
        proc = subprocess.run([go, "doc", pkg, name], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"signature": None, "reason": f"{FAILED}: go doc ({type(exc).__name__})"}
    if proc.returncode:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return {"signature": None, "reason": f"{NOT_FOUND}: go doc ({err[0][:200]})"}
    lines = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        # The package line and the doc comments (indented by four spaces) are not the signature.
        if not line.strip() or line.startswith("package ") or line.startswith("    "):
            continue
        lines.append(line.rstrip())
    return {"signature": "\n".join(lines)} if lines else {"signature": None, "reason": f"{NOT_FOUND}: {name}"}


# ---------------------------------------------------------------- one interface

def _ext(module: str) -> str:
    name = PurePosixPath(_norm(module)).name
    _, dot, ext = name.rpartition(".")
    return ext.lower() if dot else ""


def take(root: Path, module: str, name: str) -> dict[str, Any]:
    """One interface's signature: {"language", "file", "signature"} when taken, or with "signature": None and a
    "reason" when it is not; "unavailable": True when the tool to take it is not there or the language has none."""
    root = Path(root).resolve()
    ext = _ext(module)
    explicit_py = ext == "py"
    explicit_ts = ext in _TS_EXT
    explicit_go = ext == "go"
    known = explicit_py or explicit_ts or explicit_go
    if not known and ext in _OTHER_EXT:
        return {"language": _OTHER_EXT[ext], "signature": None, "reason": UNSUPPORTED, "unavailable": True}

    if explicit_py or not known:
        for rel in _py_candidates(module, explicit_py):
            if (root / rel).is_file():
                return {"language": PYTHON, "file": rel, **_python(root, rel, name)}
        if explicit_py:
            return {"language": PYTHON, "file": _norm(module), "signature": None, "reason": f"{NOT_FOUND}: {_norm(module)}"}
    if explicit_ts or not known:
        for rel in _ts_candidates(module, explicit_ts):
            if (root / rel).is_file():
                language = _TS_EXT[_ext(rel)]
                tsc = _tsc(root)
                if not tsc:
                    return {"language": language, "file": rel, "signature": None, "reason": f"{NO_TOOL}: tsc", "unavailable": True}
                return {"language": language, "file": rel, **_typescript(root, rel, name, tsc)}
        if explicit_ts:
            return {"language": _TS_EXT[ext], "file": _norm(module), "signature": None, "reason": f"{NOT_FOUND}: {_norm(module)}"}
    if explicit_go or not known:
        pkg_dir = _go_dir(root, module, explicit_go)
        if pkg_dir is not None and (not explicit_go or (root / _norm(module)).is_file()):
            file = _norm(module) if explicit_go else pkg_dir
            go = shutil.which("go")
            if not go:
                return {"language": GO, "file": file, "signature": None, "reason": f"{NO_TOOL}: go", "unavailable": True}
            return {"language": GO, "file": file, **_go(root, pkg_dir, name, go)}
        if explicit_go:
            return {"language": GO, "file": _norm(module), "signature": None, "reason": f"{NOT_FOUND}: {_norm(module)}"}
    return {"language": None, "signature": None, "reason": f"{NOT_FOUND}: {module}"}


def extract(root: Path, interfaces: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """The signatures to lock, and the interfaces whose signature cannot be taken (with the reason).

    signatures: <module>:<name> -> {"module", "name", "language", "file", "signature"}.
    """
    signatures: dict[str, dict[str, Any]] = {}
    unextractable: list[dict[str, Any]] = []
    for item in interfaces or []:
        if not isinstance(item, dict):
            continue
        module, name = str(item.get("module") or "").strip(), str(item.get("name") or "").strip()
        if not module or not name:
            continue
        got = take(root, module, name)
        if got.get("signature") is not None:
            signatures[interface_id(module, name)] = {
                "module": module, "name": name, "language": got["language"], "file": got["file"], "signature": got["signature"],
            }
        else:
            unextractable.append({"module": module, "name": name, "language": got.get("language"), "reason": got.get("reason")})
    return signatures, unextractable


def current(root: Path, locked: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The signatures of the locked interfaces now: id -> {"signature", "reason"}. An interface whose tool is not
    there now is left out (it cannot be compared)."""
    out: dict[str, dict[str, Any]] = {}
    for key, entry in (locked or {}).items():
        got = take(root, entry["module"], entry["name"])
        if got.get("unavailable"):
            continue
        out[key] = {"signature": got.get("signature"), "reason": got.get("reason")}
    return out


# ---------------------------------------------------------------- keys

def key_spec(path: str) -> tuple[str, tuple[str, ...]] | None:
    """(format, keys) for a file workers also touch, by its name; None for other files."""
    return KEY_FILES.get(PurePosixPath(_norm(path)).name)


def command_scripts(commands: list[str], scripts: dict[str, Any]) -> list[str]:
    """The package.json scripts the test commands run (npm test, npm run X, yarn X, pnpm X, bun run X)."""
    out: list[str] = []
    for command in commands or []:
        for match in _SCRIPT_RUN.finditer(str(command)):
            name = match.group(2) or "test"
            if name not in out:
                out.append(name)
        for match in _SCRIPT_BARE.finditer(str(command)):
            name = match.group(1)
            if name in scripts and name not in out:
                out.append(name)
    return out


def _walk(data: Any, key: str) -> tuple[bool, Any]:
    """Follow a dotted key; a part may itself hold dots (a script named test.unit)."""
    parts = key.split(".")
    node = data
    while parts:
        if not isinstance(node, dict):
            return False, None
        for cut in range(len(parts), 0, -1):
            head = ".".join(parts[:cut])
            if head in node:
                node, parts = node[head], parts[cut:]
                break
        else:
            return False, None
    return True, node


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _ini(text: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(text)
    return parser


def _parse(fmt: str, text: str) -> Any:
    if fmt == "json":
        return json.loads(text)
    if fmt == "toml":
        if tomllib is None:
            raise ValueError("tomllib is not available")
        return tomllib.loads(text)
    parser = _ini(text)
    return {section: dict(parser.items(section)) for section in parser.sections()}


def _value(fmt: str, data: Any, key: str) -> Any:
    if fmt == "ini":
        return _plain(data[key]) if key in data else None
    found, value = _walk(data, key)
    return _plain(value) if found else None


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def read_keys(root: Path, files: list[str], commands: list[str] | None = None) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """The keys to lock in the files workers also touch: file -> {key: value} (None when the key is absent), and
    the files that could not be read (with the reason)."""
    root = Path(root).resolve()
    keys: dict[str, dict[str, Any]] = {}
    unreadable: list[dict[str, Any]] = []
    for rel in files:
        spec = key_spec(rel)
        path = root / rel
        if spec is None or not path.is_file():
            continue
        fmt, names = spec
        try:
            data = _parse(fmt, _read_text(path))
        except (OSError, UnicodeError, ValueError, configparser.Error) as exc:
            unreadable.append({"file": rel, "reason": f"{FAILED}: {type(exc).__name__}"})
            continue
        wanted = list(names)
        if fmt == "json" and PurePosixPath(rel).name == "package.json":
            scripts = data.get("scripts") if isinstance(data, dict) and isinstance(data.get("scripts"), dict) else {}
            wanted += [f"scripts.{s}" for s in command_scripts(commands or [], scripts) if f"scripts.{s}" not in wanted]
        keys[rel] = {key: _value(fmt, data, key) for key in wanted}
    return keys, unreadable


def key_values(root: Path, locked: dict[str, dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """The locked keys' values now: file -> {key: {"value"} or {"error"}}."""
    root = Path(root).resolve()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for rel, keys in (locked or {}).items():
        spec = key_spec(rel)
        if spec is None:
            continue
        fmt = spec[0]
        path = root / rel
        try:
            data = _parse(fmt, _read_text(path)) if path.is_file() else None
        except (OSError, UnicodeError, ValueError, configparser.Error) as exc:
            out[rel] = {key: {"error": f"読めない（{type(exc).__name__}）"} for key in keys}
            continue
        if data is None:
            out[rel] = {key: {"error": "ファイルが無い"} for key in keys}
            continue
        out[rel] = {key: {"value": _value(fmt, data, key)} for key in keys}
    return out


def same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def brief(value: Any, limit: int = 160) -> str:
    text = "（無い）" if value is None else (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True))
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- putting a key back

def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _json_indent(text: str) -> int | str:
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) < len(line):
            lead = line[: len(line) - len(stripped)]
            return "\t" if lead.startswith("\t") else len(lead)
    return 2


def _set_json(text: str, key: str, value: Any) -> str:
    data = json.loads(text)
    parts = key.split(".")
    node = data
    while len(parts) > 1:
        found = next((cut for cut in range(len(parts) - 1, 0, -1) if ".".join(parts[:cut]) in node), None)
        if found is None:
            if value is None:
                return text
            node = node.setdefault(parts[0], {})
            parts = parts[1:]
        else:
            node, parts = node[".".join(parts[:found])], parts[found:]
        if not isinstance(node, dict):
            raise ValueError(f"{key} is not under an object")
    last = parts[0]
    if value is None:
        node.pop(last, None)
    else:
        node[last] = value
    nl = _newline(text)
    body = json.dumps(data, ensure_ascii=False, indent=_json_indent(text))
    return body.replace("\n", nl) + (nl if text.endswith(("\n", "\r")) else "")


_TOML_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _TOML_BARE.match(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise ValueError(f"cannot write {type(value).__name__} as TOML")


def _header_parts(header: str) -> tuple[str, ...]:
    return tuple(p.strip().strip("\"'") for p in header.split("."))


def _replace_sections(text: str, header_re: re.Pattern[str], matches: Any, block: list[str]) -> str:
    """Drop the sections whose header matches, and put block where the first one was (or at the end)."""
    lines = text.splitlines(keepends=True)
    nl = _newline(text)
    out: list[str] = []
    placed = False
    skipping = False
    for line in lines:
        head = header_re.match(line)
        if head:
            skipping = matches(head.group(1))
            if skipping and not placed:
                out.extend(b + nl for b in block)
                placed = True
        if not skipping:
            out.append(line)
    if not placed and block:
        if out and not out[-1].endswith(("\n", "\r")):
            out.append(nl)
        if out and out[-1].strip():
            out.append(nl)
        out.extend(b + nl for b in block)
    return "".join(out)


_TOML_ANY_HEADER = re.compile(r"^\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
_INI_HEADER = re.compile(r"^\[([^\]]+)\]")


def _set_toml(text: str, key: str, value: Any) -> str:
    parts = tuple(key.split("."))

    def matches(header: str) -> bool:
        got = _header_parts(header)
        return got[: len(parts)] == parts

    block: list[str] = []
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError(f"{key} is not a table")
        block = ["[" + ".".join(_toml_key(p) for p in parts) + "]"]
        block += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()]
    return _replace_sections(text, _TOML_ANY_HEADER, matches, block)


def _set_ini(text: str, key: str, value: Any) -> str:
    block: list[str] = []
    if value is not None:
        block = [f"[{key}]"]
        for k, v in value.items():
            lines = str(v).split("\n")
            block.append(f"{k} = {lines[0]}")
            block += [f"    {line}" for line in lines[1:]]
    return _replace_sections(text, _INI_HEADER, lambda header: header.strip() == key, block)


def restore_key(root: Path, rel: str, key: str, value: Any) -> bool:
    """Put one key of a file workers also touch back to its locked value; True when the file now holds it.

    Only the key is rewritten (a TOML table or an ini section is written anew). When the result does not read back
    as the locked value, the file is left as it was.
    """
    spec = key_spec(rel)
    path = Path(root).resolve() / rel
    if spec is None or not path.is_file():
        return False
    fmt = spec[0]
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        bom = raw.startswith(b"\xef\xbb\xbf")
        setter = {"json": _set_json, "toml": _set_toml, "ini": _set_ini}[fmt]
        new = setter(text, key, value)
        if not same(_value(fmt, _parse(fmt, new), key), value):
            return False
    except (UnicodeError, ValueError, KeyError, TypeError, configparser.Error):
        return False
    path.write_bytes(((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8")))
    return True
