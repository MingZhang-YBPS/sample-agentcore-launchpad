"""Every backend error code has console copy in both locales (storyline-ux §4, SG-5).

The console turns an envelope's ``code`` into ``apiErrors.<code>``
(``frontend/src/lib/api.ts`` ``localizedMessage``); a code without that key shows
the backend's operator-facing English log text to every user, in every locale.
This test reads ``backend/app`` statically and finds the codes the backend can
put in an envelope:

* a string literal passed as the code to ``AppError`` / ``NotFoundError`` / a
  subclass that forwards its ``code`` (``_ReviewRefused``), to a subclass's
  ``super().__init__(...)``, to ``envelope(...)``, or to any helper whose first
  parameter is ``code`` (``_error``, ``_forbidden``, ``_git_ok(code=...)``);
* a module constant or a local ``code`` variable that such a call names,
  resolved through plain assignments, ``a if … else b`` and tuple unpacking;
* ``{"code": "x.y"}`` dict literals and ``("x.y", message, status)`` tuples that
  an ``*_error`` function returns — codes carried as data to a raise elsewhere;
* the ``AWS_ERROR_MAP`` codes the ``ClientError`` handler emits;
* f-string codes (``f"skill_lab.{code}"``): each template must be listed in
  ``TEMPLATES`` with the values it expands to, so a new dynamic family fails
  here until someone lists — and writes copy for — its members.

The test fails with the missing ``code → file:line`` list. Add the copy to
``frontend/src/locales/{en,zh-CN}/common.json`` under ``apiErrors`` (flat
dotted keys; zh-CN with full-width punctuation).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"
LOCALES = BACKEND.parent / "frontend" / "src" / "locales"

CODE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# f-string codes the scan cannot expand itself (it follows plain parameters to
# their call sites) → the values they take. Keys are the template with every
# `{…}` replaced by `{}`; read the site to find what can fill it.
TEMPLATES: dict[str, tuple[str, ...]] = {
    # FastAPI/Starlette HTTPException → http.<status> (core/errors.py)
    "http.{}": ("http.400", "http.401", "http.403", "http.404", "http.405", "http.409",
                "http.413", "http.422", "http.429", "http.500", "http.502", "http.503"),
    # evaluation/recommendations.py — `_PROBLEM_TEXT` keys, `eligibility()` reason codes
    "recommendation.evaluator_{}": ("recommendation.evaluator_lower_is_better",
                                    "recommendation.evaluator_ground_truth",
                                    "recommendation.evaluator_categorical"),
    "recommendation.{}": ("recommendation.run_not_completed", "recommendation.run_no_batch"),
    # services/agent_tools.py — `_MESSAGES` keys
    "agent_tools.{}": ("agent_tools.not_runtime", "agent_tools.not_deployed",
                       "agent_tools.protocol", "agent_tools.jwt_inbound"),
    # services/attachments.py — `_error(kind, …)`; the PDF probe's kinds come from a dict
    "chat.attachment_{}": ("chat.attachment_too_large", "chat.attachment_invalid",
                           "chat.attachment_too_many", "chat.attachment_unsupported",
                           "chat.attachment_image_unsupported", "chat.attachment_pdf_unreadable",
                           "chat.attachment_pdf_text_unavailable", "chat.attachment_pdf_too_long",
                           "chat.attachment_text_too_large", "chat.attachment_canary_unsupported",
                           "chat.attachment_new_session_required"),
    # services/governance.py — `iam_preflight()["status"]` other than "pass"
    "governance.iam_preflight_{}": ("governance.iam_preflight_fail",
                                    "governance.iam_preflight_unknown"),
    # services/release_gates.py — blocking gate keys (ADVISORY_GATES never refuse)
    "promotion.gate_failed.{}": ("promotion.gate_failed.evaluation",
                                 "promotion.gate_failed.artifact",
                                 "promotion.gate_failed.target_ready",
                                 "promotion.gate_failed.eval_score",
                                 "promotion.gate_failed.policy_enforce",
                                 "promotion.gate_failed.deploy_window"),
}


def _func_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _template(node: ast.JoinedStr) -> str:
    return "".join(
        part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
    )


def _first_param(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    params = [*fn.args.posonlyargs, *fn.args.args]
    if params and params[0].arg in {"self", "cls"}:
        params = params[1:]
    return params[0].arg if params else None


class _Scan:
    def __init__(self) -> None:
        self.trees = {p: ast.parse(p.read_text("utf-8"), str(p)) for p in sorted(APP.rglob("*.py"))}
        self.codes: dict[str, str] = {}
        self.templates: dict[str, str] = {}
        self.consts: dict[str, set[str]] = {}
        self.tables: dict[str, set[str]] = {}
        self._parents: dict[ast.AST, ast.AST] = {}
        self._calls: dict[str, list[tuple[Path, ast.Call]]] = {}
        self._file: dict[ast.AST, Path] = {}
        self._fns: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        for path, tree in self.trees.items():
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    self._parents[child] = parent
                if isinstance(parent, ast.Call) and (name := _func_name(parent.func)):
                    self._calls.setdefault(name, []).append((path, parent))
                elif isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
                    self._fns.setdefault(parent.name, []).append(parent)
                    self._file[parent] = path
            for stmt in tree.body:
                target, value = (
                    (stmt.targets[0], stmt.value) if isinstance(stmt, ast.Assign)
                    else (stmt.target, stmt.value) if isinstance(stmt, ast.AnnAssign)
                    else (None, None)
                )
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    self.consts.setdefault(target.id, set()).add(value.value)
                elif isinstance(value, ast.Dict):
                    # a module lookup table: `_CODES[x]` / `_CODES.get(x, default)`
                    self.tables.setdefault(target.id, set()).update(
                        v.value for v in value.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str))
        self.callees = self._callees()

    def _callees(self) -> set[str]:
        """Callables whose first argument (or ``code=``) is an envelope code."""
        family = {"AppError", "NotFoundError"}
        classes: dict[str, ast.ClassDef] = {}
        for tree in self.trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    classes[node.name] = node
        grew = True
        while grew:
            grew = False
            for name, cls in classes.items():
                if name not in family and any(_func_name(b) in family for b in cls.bases):
                    family.add(name)
                    grew = True
        callees = {"envelope"}
        for name in family:
            init = next((s for s in classes[name].body if isinstance(s, ast.FunctionDef)
                         and s.name == "__init__"), None) if name in classes else None
            # a subclass that fixes its own code is read at its super().__init__ instead
            if init is None or _first_param(init) == "code":
                callees.add(name)
        for name, fns in self._fns.items():
            if any(_first_param(fn) == "code" or any(a.arg == "code" for a in fn.args.kwonlyargs)
                   for fn in fns):
                callees.add(name)
        return callees

    def _enclosing_fn(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        while node in self._parents:
            node = self._parents[node]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node
        return None

    # -- value resolution --------------------------------------------------
    def _values(self, node: ast.expr, fn: ast.AST | None, depth: int = 0) -> tuple[set[str], bool]:
        """The strings `node` can evaluate to, and whether that set is complete."""
        if depth > 4:
            return set(), False
        if isinstance(node, ast.Constant):
            return ({node.value}, True) if isinstance(node.value, str) else (set(), True)
        if isinstance(node, ast.IfExp):
            a, ca = self._values(node.body, fn, depth)
            b, cb = self._values(node.orelse, fn, depth)
            return a | b, ca and cb
        if isinstance(node, ast.JoinedStr):
            out, complete = {""}, True
            for part in node.values:
                if isinstance(part, ast.Constant):
                    out = {o + str(part.value) for o in out}
                    continue
                vals, ok = self._values(part.value, fn, depth)
                complete &= ok
                out = {o + v for o in out for v in vals}
            return out, complete
        if isinstance(node, ast.Subscript) and (table := _func_name(node.value)) in self.tables:
            return set(self.tables[table]), True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and (table := _func_name(node.func.value)) in self.tables):
            out = set(self.tables[table])
            if len(node.args) > 1:
                out |= self._values(node.args[1], fn, depth)[0]
            return out, True
        if isinstance(node, ast.Attribute):
            vals = self.consts.get(node.attr, set())
            return set(vals), bool(vals)
        if isinstance(node, ast.Name):
            if node.id in self.consts and node.id.isupper():
                return set(self.consts[node.id]), True
            if fn is not None:
                return self._local(fn, node.id, depth + 1)
        return set(), False

    def _local(self, fn: ast.AST, name: str, depth: int) -> tuple[set[str], bool]:
        """A local of `fn`: its assignments, or — for a parameter — its default and
        what every call site passes."""
        out: set[str] = set()
        found, complete = False, True
        for node in ast.walk(fn):
            pairs: list[tuple[ast.expr, ast.expr]] = []
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                        pairs += list(zip(target.elts, node.value.elts, strict=False))
                    else:
                        pairs.append((target, node.value))
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                pairs.append((node.target, node.value))
            elif (isinstance(node, ast.For | ast.comprehension)
                  and isinstance(node.target, ast.Name) and node.target.id == name):
                return out, False
            for target, value in pairs:
                if isinstance(target, ast.Name) and target.id == name:
                    vals, ok = self._values(value, fn, depth)
                    out |= vals
                    found, complete = True, complete and ok
        if found or not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            return out, found and complete
        return self._param(fn, name, depth)

    def _param(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, name: str,
               depth: int) -> tuple[set[str], bool]:
        args = fn.args
        positional = [*args.posonlyargs, *args.args]
        names = [a.arg for a in positional]
        kwonly = [a.arg for a in args.kwonlyargs]
        if name not in names and name not in kwonly:
            return set(), False
        out: set[str] = set()
        complete = True
        if name in names:
            idx = names.index(name) - (len(names) - len(args.defaults))
            if idx >= 0:
                out |= self._values(args.defaults[idx], fn, depth)[0]
        elif (default := args.kw_defaults[kwonly.index(name)]) is not None:
            out |= self._values(default, fn, depth)[0]
        bound = 1 if names and names[0] in {"self", "cls"} else 0
        # a private helper is called from its own module only; `_error` exists in many
        home = self._file.get(fn)
        calls = [call for path, call in self._calls.get(fn.name, [])
                 if not fn.name.startswith("_")
                 or (path == home and isinstance(call.func, ast.Name))]
        for call in calls:
            arg = next((k.value for k in call.keywords if k.arg == name), None)
            if arg is None and name in names:
                pos = names.index(name) - bound
                if any(isinstance(a, ast.Starred) for a in call.args[: pos + 1]):
                    complete = False
                    continue
                arg = call.args[pos] if 0 <= pos < len(call.args) else None
            if arg is None:
                continue
            vals, ok = self._values(arg, self._enclosing_fn(call), depth)
            out |= vals
            complete &= ok
        return out, complete and bool(calls)

    # -- the scan ----------------------------------------------------------
    def _add(self, path: Path, value: ast.expr, at: ast.AST) -> None:
        site = f"{path.relative_to(BACKEND)}:{getattr(at, 'lineno', 0)}"
        vals, complete = self._values(value, self._enclosing_fn(at))
        for code in vals:
            if CODE_RE.match(code):
                self.codes.setdefault(code, site)
        if isinstance(value, ast.JoinedStr) and not complete:
            self.templates.setdefault(_template(value), site)

    def run(self) -> _Scan:
        for path, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    self._call(path, node)
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values, strict=False):
                        if isinstance(key, ast.Constant) and key.value in {"code", "error_code"}:
                            self._add(path, value, node)
                elif isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
                    fn = self._enclosing_fn(node)
                    if fn is not None and fn.name.endswith("error") and node.value.elts:
                        self._add(path, node.value.elts[0], node)
        for _status, code in _aws_error_map().values():
            self.codes.setdefault(code, "app/core/errors.py:AWS_ERROR_MAP")
        self.codes.setdefault("workspace.assume_role_failed", "app/core/errors.py")
        return self

    def _call(self, path: Path, node: ast.Call) -> None:
        name = _func_name(node.func)
        for kw in node.keywords:
            # `code=` to an error constructor, or a code handed on (`categorical_code=`)
            if kw.arg and kw.arg.endswith("code") and isinstance(kw.value, ast.JoinedStr):
                self._add(path, kw.value, node)
        is_super_init = (
            name == "__init__"
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and _func_name(node.func.value.func) == "super"
        )
        if name not in self.callees and not is_super_init:
            return
        for kw in node.keywords:
            if kw.arg == "code":
                self._add(path, kw.value, node)
        if node.args and not isinstance(node.args[0], ast.Starred):
            self._add(path, node.args[0], node)


def _aws_error_map() -> dict[str, tuple[int, str]]:
    from app.core.errors import AWS_ERROR_MAP

    return AWS_ERROR_MAP


def _scan() -> _Scan:
    return _Scan().run()


def _copy(locale: str) -> dict[str, object]:
    data = json.loads((LOCALES / locale / "common.json").read_text("utf-8"))
    return data["apiErrors"]


def _has(tree: dict[str, object], code: str) -> bool:
    """i18next lookup of `apiErrors.<code>`: a flat dotted key or a nested path."""
    if isinstance(tree.get(code), str):
        return True
    head, _, rest = code.partition(".")
    sub = tree.get(head)
    return bool(rest) and isinstance(sub, dict) and _has(sub, rest)


def all_codes() -> dict[str, str]:
    """`code → first file:line` for every code the scan finds, templates expanded."""
    scan = _scan()
    codes = dict(scan.codes)
    for template, site in scan.templates.items():
        for code in TEMPLATES.get(template, ()):
            codes.setdefault(code, site)
    return codes


def test_scan_finds_the_known_shapes():
    codes = all_codes()
    # a literal, a subclass's super().__init__, a module constant, a local
    # tuple-unpacked code, an `*_error` tuple return, the AWS map
    for code in ("agent.not_found", "registry.skill_invalid", "kb.has_attached_agents",
                 "harness.context_window_exceeded", "studio.exec.disabled", "aws.throttled"):
        assert code in codes, code


def test_every_dynamic_code_family_is_listed():
    unlisted = {t: site for t, site in _scan().templates.items()
                if CODE_RE.match(t.replace("{}", "x")) and t not in TEMPLATES}
    assert not unlisted, (
        "f-string error codes with no TEMPLATES entry (list what each expands to): "
        + json.dumps(unlisted, indent=1)
    )


def test_every_backend_error_code_has_copy_in_both_locales():
    codes = all_codes()
    missing: dict[str, dict[str, str]] = {}
    for locale in ("en", "zh-CN"):
        tree = _copy(locale)
        gaps = {code: site for code, site in sorted(codes.items()) if not _has(tree, code)}
        if gaps:
            missing[locale] = gaps
    assert not missing, (
        "error codes without apiErrors.<code> copy:\n" + json.dumps(missing, indent=1)
    )
