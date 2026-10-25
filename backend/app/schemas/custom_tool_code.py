"""Custom Python ``@tool`` functions (strands-builder SB-3) — the pure code checks.

Three layers, all on the source text alone (nothing here imports, runs or
installs the member's code):

* **structure** (``tool_signature``): the source parses; it defines exactly one
  top-level function named after the tool, decorated ``@tool``; its parameters
  are plain keyword parameters with supported annotations and literal defaults.
  A spec that fails this is refused outright (``CustomTool`` validation).
* **input schema** (``ToolSignature.input_schema``): the parameters → the JSON
  Schema the model sees, computed here so a sandboxed tool is offered to the model
  without its code ever running in the runtime.
* **risk findings** (``risk_findings``): imports outside ``ALLOWED_IMPORTS``,
  dangerous calls (subprocess / ``os.system`` / ``eval`` / ``exec`` / sockets /
  ``ctypes`` / ``open`` outside the session directory), dunder escapes, code that
  runs at import, and every declared requirement. A tool with findings deploys
  only once an administrator approved that exact version (``code_hash``) —
  ``services.custom_tools``.

The checks are a gate in front of a person's review, not a sandbox: a tool that
must not touch the runtime runs in the AgentCore Code Interpreter instead
(``execution="code_interpreter"``).
"""

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

CUSTOM_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CUSTOM_TOOL_CODE_MAX_CHARS = 20000
CUSTOM_TOOL_MAX_PARAMS = 20
# ``name==version`` (optionally with extras): one immutable release, no URL / VCS
CUSTOM_TOOL_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9.+!_-]+$"
)

# The name the generated code binds to the session directory (session storage
# mount, else /tmp): ``open`` under it needs no approval.
SESSION_DIR_NAME = "SESSION_DIR"
# ``session_path(*parts)``: a path under SESSION_DIR, checked at run time to stay
# there — the way to open a file whose name the model chose
SESSION_PATH_NAME = "session_path"

# Standard-library modules a tool may import without review: computation, text,
# time, data shapes — no process, network, file-system or interpreter control.
# ``os`` is here for ``os.environ`` / ``os.path``; its dangerous attributes are
# findings of their own (``_OS_DANGEROUS``).
ALLOWED_IMPORTS = frozenset({
    "base64", "binascii", "bisect", "calendar", "cmath", "collections",
    "collections.abc", "copy", "csv", "dataclasses", "datetime", "decimal",
    "difflib", "enum", "fractions", "functools", "hashlib", "heapq", "hmac", "html",
    "ipaddress", "itertools", "json", "math", "numbers", "operator", "os", "os.path",
    "pprint", "random", "re", "secrets", "statistics", "string", "struct", "textwrap",
    "time", "typing", "typing_extensions", "unicodedata", "urllib.parse", "uuid",
    "zoneinfo",
    # the tool decorator and its models
    "pydantic", "strands",
})
# Named in the finding, so the reviewer sees why at a glance.
DANGEROUS_MODULES = frozenset({
    "subprocess", "socket", "ctypes", "cffi", "multiprocessing", "pty", "shutil",
    "importlib", "pickle", "marshal", "shelve", "builtins", "signal", "sys",
    "threading", "asyncio", "http", "urllib", "urllib.request", "ftplib", "smtplib",
    "telnetlib", "ssl", "select", "selectors", "mmap", "resource", "fcntl", "gc",
    "inspect", "code", "codeop", "runpy", "pathlib", "io", "tempfile", "glob",
})
_OS_DANGEROUS = re.compile(
    r"^(system|popen|exec\w*|spawn\w*|fork\w*|kill\w*|posix_spawn\w*|remove\w*|unlink|"
    r"rmdir|rename\w*|replace|chmod|chown|lchown|putenv|unsetenv|set\w*id|setgroups|"
    r"chroot|chdir|fchdir|truncate|link|symlink|mkfifo|mknod|open|fdopen|write|"
    r"startfile|_exit|abort|plock|nice|scandir|listdir|walk|makedirs|mkdir)$"
)
_DANGEROUS_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "breakpoint", "globals", "locals", "vars",
    "input", "help", "exit", "quit", "memoryview",
})
# dunder names that are plain data, not escape hatches
_SAFE_DUNDERS = frozenset({"__name__", "__doc__", "__init__", "__post_init__", "__all__"})
# requirement distribution → the module it installs, where the two differ
_IMPORT_NAMES = {
    "beautifulsoup4": "bs4", "pyyaml": "yaml", "python-dateutil": "dateutil",
    "scikit-learn": "sklearn", "pillow": "PIL", "opencv-python": "cv2",
    "python-dotenv": "dotenv", "protobuf": "google.protobuf",
}

_SCALAR_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


@dataclass(frozen=True)
class Finding:
    """One thing a reviewer must look at before the tool may deploy."""

    kind: str  # import | dangerous_import | call | open | dunder | import_time | requirement
    line: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "line": self.line, "message": self.message}


@dataclass
class ToolSignature:
    name: str
    params: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    docstring: str = ""


class CustomToolCodeError(ValueError):
    """The source cannot become a tool (syntax or structure) — refused, not reviewed."""

    def __init__(self, message: str, line: int = 0, col: int = 0) -> None:
        super().__init__(message)
        self.line, self.col = line, col

    def as_dict(self) -> dict[str, Any]:
        return {"message": str(self), "line": self.line, "col": self.col}


def code_hash(name: str, code: str, requirements: list[str], execution: str) -> str:
    """The version an approval is for: any change to the code, its requirements,
    its name or where it runs is a different hash — and so needs a new approval."""
    canonical = json.dumps(
        {"name": name, "code": code, "requirements": list(requirements),
         "execution": execution},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse(code: str) -> ast.Module:
    if len(code) > CUSTOM_TOOL_CODE_MAX_CHARS:
        raise CustomToolCodeError(
            f"the tool's code exceeds {CUSTOM_TOOL_CODE_MAX_CHARS} characters"
        )
    try:
        return ast.parse(code, filename="<custom tool>", type_comments=False)
    except SyntaxError as exc:
        raise CustomToolCodeError(
            f"syntax error: {exc.msg} (line {exc.lineno or 0})",
            exc.lineno or 0, exc.offset or 0,
        ) from None


def _is_tool_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id == "tool"
    return (
        isinstance(target, ast.Attribute) and target.attr == "tool"
        and isinstance(target.value, ast.Name) and target.value.id == "strands"
    )


def _tool_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    if not functions:
        raise CustomToolCodeError(
            f"define a top-level function named {name!r} — the tool's name — and "
            "decorate it with @tool"
        )
    if len(functions) > 1:
        raise CustomToolCodeError(f"{name!r} is defined more than once", functions[1].lineno)
    fn = functions[0]
    if isinstance(fn, ast.AsyncFunctionDef):
        raise CustomToolCodeError(f"{name!r} must be a plain def, not async def", fn.lineno)
    tools = [d for d in fn.decorator_list if _is_tool_decorator(d)]
    if len(tools) != 1:
        raise CustomToolCodeError(f"decorate {name!r} with @tool (once)", fn.lineno)
    if len(fn.decorator_list) != 1:
        raise CustomToolCodeError(f"{name!r} takes the @tool decorator only", fn.lineno)
    others = [
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn
        and any(_is_tool_decorator(d) for d in node.decorator_list)
    ]
    if others:
        raise CustomToolCodeError(
            f"one @tool per custom tool — {others[0]!r} is a second one; add it as its "
            "own tool"
        )
    return fn


def _annotation_schema(node: ast.expr | None, where: str) -> dict[str, Any]:
    """A parameter annotation → JSON Schema (the supported subset)."""
    if node is None:
        return {"type": "string"}
    if isinstance(node, ast.Constant) and node.value is None:
        return {"type": "null"}
    if isinstance(node, ast.Name):
        if node.id in _SCALAR_TYPES:
            return {"type": _SCALAR_TYPES[node.id]}
        if node.id == "list":
            return {"type": "array"}
        if node.id == "dict":
            return {"type": "object"}
        if node.id == "Any":
            return {}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _annotation_schema(node.left, where)
        right = _annotation_schema(node.right, where)
        if right == {"type": "null"}:
            return left
        if left == {"type": "null"}:
            return right
        return {"anyOf": [left, right]}
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        head, inner = node.value.id, node.slice
        if head == "list":
            return {"type": "array", "items": _annotation_schema(inner, where)}
        if head == "dict":
            if isinstance(inner, ast.Tuple) and len(inner.elts) == 2:
                return {"type": "object",
                        "additionalProperties": _annotation_schema(inner.elts[1], where)}
        if head == "Optional":
            return _annotation_schema(inner, where)
        if head == "Literal":
            values = inner.elts if isinstance(inner, ast.Tuple) else [inner]
            if values and all(
                isinstance(v, ast.Constant) and isinstance(v.value, (str, int, float, bool))
                for v in values
            ):
                return {"enum": [v.value for v in values]}  # type: ignore[attr-defined]
    raise CustomToolCodeError(
        f"{where}: annotation {ast.unparse(node)!r} is not supported — use str, int, "
        "float, bool, list[...], dict[str, ...], Literal[...], Any or X | None",
        node.lineno,
    )


def _docstring_args(doc: str) -> dict[str, str]:
    """``Args:`` section of a Google-style docstring → {param: description}."""
    out: dict[str, str] = {}
    in_args = False
    current = ""
    for raw in doc.splitlines():
        line = raw.strip()
        if line.lower() in ("args:", "arguments:", "parameters:"):
            in_args = True
            continue
        if not in_args:
            continue
        if not line:
            continue
        if line.endswith(":") and " " not in line:  # the next section (Returns:)
            break
        match = re.match(r"^(\w+)\s*(\([^)]*\))?\s*:\s*(.*)$", line)
        if match:
            current = match.group(1)
            out[current] = match.group(3).strip()
        elif current:
            out[current] = (out[current] + " " + line).strip()
    return out


def tool_signature(code: str, name: str) -> ToolSignature:
    """Structure check + the model-facing input schema. Raises CustomToolCodeError."""
    tree = parse(code)
    fn = _tool_function(tree, name)
    args = fn.args
    if args.vararg or args.kwarg:
        raise CustomToolCodeError(f"{name!r}: *args / **kwargs are not supported", fn.lineno)
    if args.posonlyargs:
        raise CustomToolCodeError(
            f"{name!r}: positional-only parameters are not supported", fn.lineno
        )
    params = [*args.args, *args.kwonlyargs]
    if len(params) > CUSTOM_TOOL_MAX_PARAMS:
        raise CustomToolCodeError(
            f"{name!r} takes more than {CUSTOM_TOOL_MAX_PARAMS} parameters", fn.lineno
        )
    defaults: dict[str, ast.expr] = {}
    for arg, default in zip(args.args[len(args.args) - len(args.defaults):], args.defaults,
                            strict=True):
        defaults[arg.arg] = default
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is not None:
            defaults[arg.arg] = default
    doc = ast.get_docstring(fn) or ""
    described = _docstring_args(doc)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg in params:
        if arg.arg in ("self", "agent", "tool_context"):
            raise CustomToolCodeError(
                f"{name!r}: parameter {arg.arg!r} is reserved by the Strands SDK", arg.lineno
            )
        schema = _annotation_schema(arg.annotation, f"parameter {arg.arg!r}")
        if arg.arg in defaults:
            try:
                value = ast.literal_eval(defaults[arg.arg])
            except ValueError:
                raise CustomToolCodeError(
                    f"parameter {arg.arg!r}: the default must be a literal value",
                    arg.lineno,
                ) from None
            schema = {**schema, "default": value}
        else:
            required.append(arg.arg)
        if described.get(arg.arg):
            schema = {**schema, "description": described[arg.arg]}
        properties[arg.arg] = schema
    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required
    return ToolSignature(
        name=name, params=[a.arg for a in params], input_schema=input_schema, docstring=doc
    )


def requirement_import_name(requirement: str) -> str:
    dist = re.split(r"[\[=<>!~ ]", requirement, maxsplit=1)[0].strip()
    return _IMPORT_NAMES.get(dist.lower(), dist.lower().replace("-", "_").replace(".", "_"))


def _module_allowed(module: str) -> bool:
    parts = module.split(".")
    return any(".".join(parts[:i]) in ALLOWED_IMPORTS for i in range(len(parts), 0, -1)) and (
        # a dangerous submodule of an allowed package (urllib.request) stays a finding
        module not in DANGEROUS_MODULES
    )


def _session_rooted(node: ast.expr) -> bool:
    """A path expression that provably starts at the session directory."""
    if isinstance(node, ast.Name):
        return node.id == SESSION_DIR_NAME
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == SESSION_PATH_NAME:
            return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        # Path / "/etc" restarts at the root, Path / ".." climbs out
        return _session_rooted(node.left) and not _has_parent_ref(node.right, absolute=True)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _session_rooted(node.left) and not _has_parent_ref(node.right)
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return (
            isinstance(first, ast.FormattedValue) and _session_rooted(first.value)
            and not any(_has_parent_ref(v) for v in node.values[1:])
        )
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        joined = (
            isinstance(func, ast.Attribute) and func.attr == "join"
            and isinstance(func.value, ast.Attribute) and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name) and func.value.value.id == "os"
        )
        return joined and _session_rooted(node.args[0]) and not any(
            _has_parent_ref(a, absolute=True) for a in node.args[1:]
        )
    return False


def _has_parent_ref(node: ast.AST, absolute: bool = False) -> bool:
    """A literal ``..`` anywhere in the expression — or, where a path is joined, a
    component that starts at the root. A computed component (a parameter) may hold
    either, so only literal-or-SESSION_DIR shapes pass."""
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if ".." in n.value or (absolute and n.value.startswith("/")):
                return True
        elif isinstance(n, (ast.Name, ast.Call, ast.Attribute, ast.Subscript)):
            return True
    return False


def _import_time_statement(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                         ast.ClassDef, ast.Pass)):
        return False
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        return False  # a docstring
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        value = node.value
        return value is not None and any(isinstance(n, ast.Call) for n in ast.walk(value))
    return True


def risk_findings(code: str, requirements: list[str] | None = None) -> list[Finding]:
    """What needs an administrator's approval, in source order (one per site)."""
    tree = parse(code)
    out: list[Finding] = []
    for requirement in requirements or []:
        out.append(Finding(
            "requirement", 0,
            f"installs {requirement} into the agent's runtime — third-party code is "
            "reviewed with the tool",
        ))
    os_aliases = {"os"}
    for node in tree.body:
        if _import_time_statement(node):
            out.append(Finding(
                "import_time", node.lineno,
                "runs code when the tool loads — keep the work inside the @tool function",
            ))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.extend(_import_finding(alias.name, node.lineno))
                if alias.name == "os" and alias.asname:
                    os_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.append(Finding("import", node.lineno, "relative imports are not allowed"))
                continue
            module = node.module or ""
            out.extend(_import_finding(module, node.lineno))
            if module == "os":
                for alias in node.names:
                    if alias.name == "*" or _OS_DANGEROUS.match(alias.name):
                        out.append(Finding(
                            "call", node.lineno,
                            f"from os import {alias.name} — process / file-system control",
                        ))
        elif isinstance(node, ast.Call):
            out.extend(_call_findings(node, os_aliases))
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr not in _SAFE_DUNDERS:
                out.append(Finding(
                    "dunder", node.lineno,
                    f"attribute {node.attr} reaches interpreter internals",
                ))
            elif (
                isinstance(node.value, ast.Name) and node.value.id in os_aliases
                and _OS_DANGEROUS.match(node.attr)
            ):
                out.append(Finding(
                    "call", node.lineno, f"os.{node.attr} — process / file-system control"
                ))
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__import__",
                                                         "__loader__", "__spec__"):
            out.append(Finding("dunder", node.lineno, f"{node.id} reaches interpreter internals"))
        if _rebinds_session_names(node):
            out.append(Finding(
                "open", getattr(node, "lineno", 0),
                f"redefines {SESSION_DIR_NAME} / {SESSION_PATH_NAME} — the platform "
                "provides them",
            ))
    # one finding per (kind, line, message), source order
    seen: set[tuple[str, int, str]] = set()
    unique: list[Finding] = []
    for finding in sorted(out, key=lambda f: (f.line, f.kind)):
        key = (finding.kind, finding.line, finding.message)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _rebinds_session_names(node: ast.AST) -> bool:
    names = {SESSION_DIR_NAME, SESSION_PATH_NAME}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name in names
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return node.id in names
    if isinstance(node, ast.arg):
        return node.arg in names
    if isinstance(node, ast.alias):
        return (node.asname or node.name) in names
    return False


def _import_finding(module: str, line: int) -> list[Finding]:
    if _module_allowed(module):
        return []
    root = module.split(".")[0]
    if module in DANGEROUS_MODULES or root in DANGEROUS_MODULES:
        return [Finding(
            "dangerous_import", line,
            f"import {module} — process, network, file-system or interpreter control",
        )]
    return [Finding("import", line, f"import {module} is outside the allow-list")]


def _call_findings(node: ast.Call, os_aliases: set[str]) -> list[Finding]:
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in _DANGEROUS_CALLS:
            return [Finding("call", node.lineno, f"{func.id}() runs or inspects arbitrary code")]
        if func.id in ("getattr", "setattr", "delattr", "hasattr") and (
            len(node.args) < 2
            or not (isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str))
        ):
            return [Finding(
                "call", node.lineno,
                f"{func.id}() with a computed attribute name can reach anything",
            )]
        if func.id == "open":
            if node.args and _session_rooted(node.args[0]):
                return []
            return [Finding(
                "open", node.lineno,
                f"open() outside the session directory — build the path from "
                f"{SESSION_DIR_NAME} (e.g. {SESSION_DIR_NAME} / \"notes.txt\")",
            )]
    return []
