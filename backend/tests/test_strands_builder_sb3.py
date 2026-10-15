"""strands-builder SB-3: custom Python @tool functions (static check, admin approval
by code hash, packaged into the zip, optionally run in the AgentCore Code
Interpreter) and the high-risk built-in tools (python_repl / shell / file_write /
use_aws, opened by the workspace's sandbox setting, run only in the sandbox) —
schema, code check, ledger gate, routes, generated code, and byte-identical output
for every spec that sets none of them. No AWS: the sandbox is a fake that runs the
code in this process."""

import asyncio
import contextlib
import importlib.util
import io
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from strands.models.model import Model

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.core.route_policy import ADMIN, ADMIN_DATA, PROJECT_EXEMPT, ROUTE_POLICY, admin_kind
from app.deployer import zip_runtime
from app.deployer.zip_runtime import _method_requirements, write_custom_tool_files
from app.models.ledger import Agent, Workspace
from app.schemas.agent import AgentSpec
from app.schemas.custom_tool_code import code_hash, risk_findings, tool_signature
from app.schemas.strands_builder import uses_code_interpreter
from app.services import custom_tools as svc
from app.templates.strands_a2a_agent import render_a2a_main_py
from app.templates.strands_agent import base_requirements, render_main_py
from app.templates.strands_agent.builder import custom_tool_files, journal_root
from tests import strands_builder_sb2_snapshots as sb2_snap
from tests import strands_builder_sb3_snapshots as sb3_snap
from tests.strands_builder_snapshots import OLD_SPECS, dump, render
from tests.test_project_scope import _login, _member_of, _project, _user, app  # noqa: F401

CLAUDE = "global.anthropic.claude-sonnet-4-5-v1:0"
BASE = {"name": "sb3-agent", "method": "zip_runtime", "system_prompt": "Be brief.",
        "model_id": CLAUDE}
REGION = "us-east-1"
SB3_NAMES = ("custom_tools", "sandbox_tools", "code_interpreter_id")
DEFAULT = {"X-Project": "default"}

CLEAN = '''from strands import tool


@tool
def word_count(text: str, unique: bool = False) -> int:
    """Count the words in a text.

    Args:
        text: The text to count.
        unique: Count distinct words only.
    """
    words = text.split()
    return len(set(words)) if unique else len(words)
'''

RISKY = '''import subprocess


@tool
def disk_usage(path: str) -> str:
    """Report disk usage."""
    return subprocess.run(["du", "-sh", path], capture_output=True, text=True).stdout
'''


def _tool(code=CLEAN, name="word_count", **over) -> dict:
    return {"name": name, "description": "Count words in a text.", "code": code, **over}


def _spec(**over) -> AgentSpec:
    return AgentSpec(**{**BASE, **over})


# ── older specs: byte-identical ──────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(sb3_snap.SB2_SPECS))
def test_sb2_spec_renders_byte_identical_code(key):
    spec = sb3_snap.SB2_SPECS[key]
    assert render(spec) == sb3_snap.fixture_path(key).read_text(encoding="utf-8")
    assert dump(spec) == sb3_snap.dump_path(key).read_text(encoding="utf-8")


@pytest.mark.parametrize("specs", [OLD_SPECS, sb2_snap.SB1_SPECS, sb3_snap.SB2_SPECS])
def test_older_specs_store_no_sb3_field(specs):
    for spec in specs.values():
        stored = spec.model_dump()
        assert not any(name in stored for name in SB3_NAMES)
        code = render(spec)
        assert "CUSTOM_TOOL_SPECS" not in code and "launchpad_sandbox_call" not in code
        assert custom_tool_files(spec) == {}
        assert not uses_code_interpreter(spec)


def test_older_spec_requirements_are_unchanged():
    assert _method_requirements(OLD_SPECS["default"]) == base_requirements()


# ── spec validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool,message", [
    (_tool(name="Word"), "lowercase letter"),
    (_tool(code="def word_count(:\n"), "syntax error"),
    (_tool(code="def word_count(text: str) -> int:\n    return 1\n"), "decorate"),
    (_tool(code="@tool\ndef other(text: str) -> int:\n    return 1\n"), "named 'word_count'"),
    (_tool(code="@tool\nasync def word_count(text: str) -> int:\n    return 1\n"), "async"),
    (_tool(code="@tool\ndef word_count(*args) -> int:\n    return 1\n"), "*args"),
    (_tool(code="@tool\ndef word_count(x: set[int]) -> int:\n    return 1\n"),
     "not supported"),
    (_tool(code="@tool\ndef word_count(x: int = len('a')) -> int:\n    return 1\n"),
     "literal"),
    (_tool(code=CLEAN + "\n\n@tool\ndef second(x: str) -> str:\n    return x\n"),
     "one @tool"),
    (_tool(requirements=["requests>=2"]), "name==version"),
    (_tool(requirements=["requests==2.32.3"], execution="code_interpreter"), "installs nothing"),
])
def test_bad_custom_tools_are_refused(tool, message):
    with pytest.raises(ValidationError, match=re.escape(message)):
        _spec(custom_tools=[tool])


@pytest.mark.parametrize("over,message", [
    ({"custom_tools": [_tool(), _tool()]}, "unique"),
    ({"custom_tools": [_tool(code=CLEAN.replace("word_count", "calculator"), name="calculator")]},
     "already a tool"),
    ({"strands_tools": ["think"], "custom_tools": [
        _tool(code=CLEAN.replace("word_count", "think"), name="think")]}, "already a tool"),
    ({"sandbox_tools": ["shell", "shell"]}, "unique"),
    ({"code_interpreter_id": "my-ci-abc"}, "code_interpreter_id applies"),
    ({"method": "harness", "custom_tools": [_tool()]}, "zip_runtime"),
    ({"code": "print(1)", "sandbox_tools": ["shell"]}, "code/code_bundle"),
])
def test_bad_sb3_specs_are_refused(over, message):
    with pytest.raises(ValidationError, match=re.escape(message)):
        _spec(**over)


def test_the_stored_spec_carries_what_was_set():
    spec = _spec(custom_tools=[_tool()], sandbox_tools=["python_repl"])
    stored = spec.model_dump()
    assert stored["custom_tools"][0]["execution"] == "runtime"
    assert stored["sandbox_tools"] == ["python_repl"]
    assert "code_interpreter_id" not in stored
    assert uses_code_interpreter(spec)
    assert not uses_code_interpreter(_spec(custom_tools=[_tool()]))


# ── the code check ───────────────────────────────────────────────────────────────


def test_the_signature_becomes_the_input_schema():
    sig = tool_signature(CLEAN, "word_count")
    assert sig.params == ["text", "unique"]
    assert sig.input_schema == {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to count."},
            "unique": {"type": "boolean", "default": False,
                       "description": "Count distinct words only."},
        },
        "required": ["text"],
    }
    code = (
        "from typing import Any, Literal\n\n@tool\ndef f(a: list[int], b: dict[str, Any] | None"
        " = None, *, c: Literal['x', 'y'] = 'x', d=3) -> str:\n    return ''\n"
    )
    props = tool_signature(code, "f").input_schema["properties"]
    assert props["a"] == {"type": "array", "items": {"type": "integer"}}
    assert props["b"] == {"type": "object", "additionalProperties": {}, "default": None}
    assert props["c"] == {"enum": ["x", "y"], "default": "x"}
    assert props["d"] == {"type": "string", "default": 3}


def test_clean_code_has_no_findings():
    assert risk_findings(CLEAN) == []
    allowed = (
        "import json, math, re, os\nfrom datetime import datetime\nimport urllib.parse\n\n"
        "@tool\ndef f(name: str) -> str:\n"
        "    with open(SESSION_DIR / 'notes.txt', 'a') as fh:\n        fh.write(name)\n"
        "    with open(session_path(name)) as fh:\n        fh.read()\n"
        "    open(f'{SESSION_DIR}/log.txt').close()\n"
        "    return os.environ.get('TEAM', '') + json.dumps(math.pi)\n"
    )
    assert risk_findings(allowed) == []


@pytest.mark.parametrize("snippet,kind,needle", [
    ("import subprocess", "dangerous_import", "subprocess"),
    ("import socket", "dangerous_import", "socket"),
    ("import ctypes", "dangerous_import", "ctypes"),
    ("import requests", "import", "outside the allow-list"),
    ("from urllib import request", "dangerous_import", "urllib"),
    ("import os\nos.system('ls')", "call", "os.system"),
    ("from os import popen", "call", "popen"),
    ("eval('1')", "call", "eval()"),
    ("exec('x = 1')", "call", "exec()"),
    ("__import__('os')", "call", "__import__()"),
    ("getattr(object, 'a' + 'b')", "call", "computed attribute"),
    ("open('/etc/passwd')", "open", "outside the session directory"),
    ("open(SESSION_DIR / '../x')", "open", "outside the session directory"),
    ("open(SESSION_DIR / '/etc/passwd')", "open", "outside the session directory"),
    ("open(SESSION_DIR / name)", "open", "outside the session directory"),
    ("().__class__.__bases__", "dunder", "__class__"),
    ("SESSION_DIR = '/'", "open", "redefines"),
])
def test_risky_code_is_found(snippet, kind, needle):
    body = "\n".join(f"    {line}" for line in snippet.splitlines())
    code = f"@tool\ndef f(name: str) -> str:\n{body}\n    return ''\n"
    findings = risk_findings(code)
    assert any(f.kind == kind and needle in f.message for f in findings), findings


def test_import_time_code_and_requirements_are_found():
    code = "import json\nDATA = json.loads('[]')\nprint('loading')\n\n" + CLEAN
    kinds = [(f.kind, f.line) for f in risk_findings(code, ["requests==2.32.3"])]
    assert ("requirement", 0) in kinds
    assert ("import_time", 2) in kinds and ("import_time", 3) in kinds
    # a literal constant is not import-time work
    assert risk_findings("LIMIT = 10\n\n" + CLEAN) == []


def test_any_change_is_a_new_hash():
    base = code_hash("t", CLEAN, [], "runtime")
    assert base == code_hash("t", CLEAN, [], "runtime")
    assert len({
        base,
        code_hash("t", CLEAN + "\n", [], "runtime"),
        code_hash("t", CLEAN, ["requests==2.32.3"], "runtime"),
        code_hash("t", CLEAN, [], "code_interpreter"),
        code_hash("u", CLEAN, [], "runtime"),
    }) == 5


# ── the ledger gate ──────────────────────────────────────────────────────────────


def _gate(spec, *, project="default", allowed=(), interpreter=None):
    db = SessionLocal()
    try:
        with project_context(project):
            ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
            if allowed or interpreter:
                svc.set_sandbox_setting(ws, allowed_tools=list(allowed),
                                        code_interpreter_id=interpreter, actor="admin")
                db.commit()
            return svc.assert_deployable(db, spec, workspace=ws, project_id=project)
    finally:
        db.close()


def _approve(spec_tool, project="default"):
    db = SessionLocal()
    try:
        with project_context(project):
            record = svc.approve(db, project, spec_tool, actor="admin", actor_user_id=None,
                                 note="reviewed")
            db.commit()
            return record
    finally:
        db.close()


def test_clean_tools_deploy_without_approval():
    spec = _spec(custom_tools=[_tool()])
    assert _gate(spec) is spec


def test_risky_tools_need_an_approval_of_their_version():
    spec = _spec(custom_tools=[_tool(RISKY, "disk_usage")])
    with pytest.raises(AppError) as err:
        _gate(spec)
    assert err.value.code == "custom_tool.approval_required"
    assert err.value.detail["tools"] == ["disk_usage"]
    record = _approve(spec.custom_tools[0])
    assert record["code_hash"] == spec.custom_tools[0].hash()
    assert record["findings"][0]["kind"] == "dangerous_import"
    assert _gate(spec).custom_tools == spec.custom_tools
    # one changed character: the approval no longer covers it
    edited = _spec(custom_tools=[_tool(RISKY.replace("-sh", "-h"), "disk_usage")])
    with pytest.raises(AppError, match="approval"):
        _gate(edited)
    # the approval is the project's own
    _project("alpha")
    with pytest.raises(AppError, match="approval"):
        _gate(spec, project="alpha")


def test_revoking_answers_404_outside_its_project():
    tool = _spec(custom_tools=[_tool(RISKY, "disk_usage")]).custom_tools[0]
    _approve(tool)
    _project("alpha")
    db = SessionLocal()
    try:
        with pytest.raises(NotFoundError):
            svc.revoke(db, "alpha", tool.hash())
        svc.revoke(db, "default", tool.hash())
        db.commit()
        assert svc.approvals(db, "default") == {}
    finally:
        db.close()


def test_sandbox_tools_need_the_workspace_setting_and_get_its_interpreter():
    spec = _spec(sandbox_tools=["python_repl", "shell"], code_interpreter_id="forged-ci")
    with pytest.raises(AppError) as err:
        _gate(spec)
    assert err.value.code == "custom_tool.sandbox_tool_not_allowed"
    with pytest.raises(AppError, match="shell is not allowed"):
        _gate(spec, allowed=["python_repl"])
    gated = _gate(spec, allowed=["python_repl", "shell"])
    assert gated.code_interpreter_id is None  # the forged id never survives
    gated = _gate(spec, allowed=["python_repl", "shell"], interpreter="team-ci-AbC123")
    assert gated.code_interpreter_id == "team-ci-AbC123"
    assert "'team-ci-AbC123'" in render_main_py(gated)


def _agent_row(spec: AgentSpec, project="default") -> str:
    db = SessionLocal()
    try:
        with project_context(project):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=spec.name,
                        method="zip_runtime", status="draft", spec=spec.model_dump())
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def test_the_generate_stage_gates_and_stamps():
    risky = _spec(custom_tools=[_tool(RISKY, "disk_usage", execution="code_interpreter")])
    agent_id = _agent_row(risky)
    db = SessionLocal()
    try:
        with project_context("default"):
            row = db.get(Agent, agent_id)
            with pytest.raises(AppError, match="approval"):
                zip_runtime._gate_custom_tools(row, AgentSpec(**row.spec))
            _approve(risky.custom_tools[0])
            ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
            svc.set_sandbox_setting(ws, allowed_tools=[], code_interpreter_id="team-ci-1",
                                    actor="admin")
            db.commit()
            db.expire_all()
            row = db.get(Agent, agent_id)
            spec = zip_runtime._gate_custom_tools(row, AgentSpec(**row.spec))
            assert spec.code_interpreter_id == "team-ci-1"
            assert row.spec["code_interpreter_id"] == "team-ci-1"
    finally:
        db.close()


def test_runtime_tools_add_their_pins_and_files(tmp_path):
    spec = _spec(requirements=["httpx==0.28.1"], custom_tools=[
        _tool(requirements=["requests==2.32.3", "httpx==0.28.1"]),
        _tool(CLEAN.replace("word_count", "word_count_b"), "word_count_b",
              execution="code_interpreter"),
    ])
    reqs = _method_requirements(spec)
    assert reqs[-2:] == ["httpx==0.28.1", "requests==2.32.3"]
    assert write_custom_tool_files(spec, tmp_path) == 2
    assert (tmp_path / "custom_tools" / "word_count.py").read_text() == CLEAN


# ── routes ───────────────────────────────────────────────────────────────────────


def test_routes_are_classified():
    assert ROUTE_POLICY[("POST", "/api/custom-tools/check")] == "perm:project.use"
    assert ROUTE_POLICY[("POST", "/api/custom-tools/approvals")] == ADMIN
    assert admin_kind("POST", "/api/custom-tools/approvals") == ADMIN_DATA
    assert admin_kind("PUT", "/api/custom-tools/sandbox") == ADMIN_DATA
    assert ("GET", "/api/custom-tools/sandbox") in PROJECT_EXEMPT
    assert ("POST", "/api/custom-tools/check") not in PROJECT_EXEMPT


def test_check_approve_and_revoke(client):
    body = {"name": "disk_usage", "code": RISKY}
    res = client.post("/api/custom-tools/check", json=body, headers=DEFAULT)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["ok"] and out["needs_approval"] and out["approval"] is None
    assert out["input_schema"]["required"] == ["path"]
    broken = client.post("/api/custom-tools/check", json={"name": "x", "code": "def x(:"},
                         headers=DEFAULT).json()
    assert not broken["ok"] and broken["error"]["line"] == 1
    res = client.post("/api/custom-tools/approvals", json={**body, "note": "ok"},
                      headers=DEFAULT)
    assert res.status_code == 201, res.text
    digest = res.json()["code_hash"]
    assert digest == out["code_hash"]
    again = client.post("/api/custom-tools/check", json=body, headers=DEFAULT).json()
    assert again["approval"]["approved_by"] and again["approval"]["note"] == "ok"
    listed = client.get("/api/custom-tools/approvals", headers=DEFAULT).json()["approvals"]
    assert [a["code_hash"] for a in listed] == [digest]
    bad = client.post("/api/custom-tools/approvals", json={"name": "x", "code": "x = 1"},
                      headers=DEFAULT)
    assert bad.status_code == 422 and bad.json()["code"] == "custom_tool.invalid"
    _project("alpha")
    alpha = {"X-Project": "alpha"}
    assert client.get("/api/custom-tools/approvals", headers=alpha).json()["approvals"] == []
    assert client.delete(f"/api/custom-tools/approvals/{digest}",
                         headers=alpha).status_code == 404
    assert client.delete(f"/api/custom-tools/approvals/{digest}",
                         headers=DEFAULT).status_code == 200


def test_the_sandbox_setting_route(client):
    res = client.get("/api/custom-tools/sandbox")
    assert res.status_code == 200 and res.json()["allowed_tools"] == []
    assert res.json()["effective_code_interpreter_id"] == "aws.codeinterpreter.v1"
    res = client.put("/api/custom-tools/sandbox",
                     json={"allowed_tools": ["shell", "python_repl"],
                           "code_interpreter_id": "team-ci-1"})
    assert res.status_code == 200, res.text
    assert res.json()["allowed_tools"] == ["python_repl", "shell"]
    assert client.get("/api/custom-tools/sandbox").json()["code_interpreter_id"] == "team-ci-1"
    assert client.put("/api/custom-tools/sandbox",
                      json={"allowed_tools": ["rm_rf"]}).status_code == 422


def test_a_member_cannot_approve_or_open_the_sandbox(app):  # noqa: F811
    _member_of("default", _user("dev", grant=False), "developer")
    dev = _login(app, "dev")
    body = {"name": "disk_usage", "code": RISKY}
    assert dev.post("/api/custom-tools/check", json=body, headers=DEFAULT).status_code == 200
    assert dev.get("/api/custom-tools/sandbox").status_code == 200
    assert dev.post("/api/custom-tools/approvals", json=body,
                    headers=DEFAULT).status_code == 403
    assert dev.put("/api/custom-tools/sandbox", json={"allowed_tools": []}).status_code == 403


# ── generated code ───────────────────────────────────────────────────────────────


class FakeSandbox:
    """A Code Interpreter that runs executeCode in-process, executeCommand as an
    echo and writeFiles into a dict."""

    instances: list["FakeSandbox"] = []

    def __init__(self, region, session=None):
        self.region, self.calls, self.files, self.started = region, [], {}, None
        self.ns: dict = {}
        FakeSandbox.instances.append(self)

    def start(self, identifier=None, session_timeout_seconds=None):
        self.started = identifier
        return "sess"

    def stop(self):
        return True

    def invoke(self, method, params=None):
        self.calls.append((method, params))
        if method == "executeCode":
            buf = io.StringIO()
            error = False
            with contextlib.redirect_stdout(buf):
                try:
                    exec(params["code"], self.ns)  # noqa: S102 — the fake sandbox
                except Exception as exc:  # noqa: BLE001
                    print(f"Traceback: {type(exc).__name__}: {exc}")
                    error = True
            text = buf.getvalue()
        elif method == "executeCommand":
            text, error = f"ran: {params['command']}", False
        else:
            for item in params["content"]:
                self.files[item["path"]] = item["text"]
            text, error = "ok", False
        return {"stream": [{"result": {"content": [{"type": "text", "text": text}],
                                       "isError": error}}]}


def _load(spec: AgentSpec, tmp_path: Path, monkeypatch, stem: str):
    monkeypatch.setenv("AWS_REGION", REGION)
    source = render_main_py(spec)
    compile(source, f"{stem}.py", "exec")
    pkg = tmp_path / stem
    pkg.mkdir()
    (pkg / "main.py").write_text(source, encoding="utf-8")
    write_custom_tool_files(spec, pkg)
    FakeSandbox.instances.clear()
    import bedrock_agentcore.tools.code_interpreter_client as ci

    monkeypatch.setattr(ci, "CodeInterpreter", FakeSandbox)
    module_spec = importlib.util.spec_from_file_location(stem, pkg / "main.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.BedrockAgentCoreContext, "get_config_bundle", staticmethod(lambda: {})
    )
    return module


class ToolModel(Model):
    def __init__(self, tool: str, tool_input: dict):
        self.config: dict = {}
        self.tool, self.tool_input = tool, tool_input
        self.seen: list[dict] = []

    def update_config(self, **kw):
        self.config.update(kw)

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):  # pragma: no cover - unused path
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        names = [t["name"] for t in tool_specs or []]
        results = [b["toolResult"] for m in messages if m["role"] == "user"
                   for b in m["content"] if "toolResult" in b]
        self.seen.append({"tools": names, "specs": tool_specs, "results": results})
        yield {"messageStart": {"role": "assistant"}}
        if self.tool in names and not results:
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1",
                                                               "name": self.tool}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps(self.tool_input)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": "Done."}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _drain(agen):
    async def collect():
        return [event async for event in agen]

    return asyncio.run(collect())


SANDBOXED = CLEAN.replace("word_count", "shout").replace(
    "words = text.split()\n    return len(set(words)) if unique else len(words)",
    "return {'loud': text.upper(), 'unique': unique}",
).replace("-> int", "-> dict")


def _full_spec(**over) -> AgentSpec:
    return _spec(
        custom_tools=[_tool(), _tool(SANDBOXED, "shout", description="Shout a text.",
                                     execution="code_interpreter")],
        sandbox_tools=["python_repl", "shell", "file_write", "use_aws"],
        **over,
    )


def test_the_tools_replace_the_defaults_and_run(tmp_path, monkeypatch):
    module = _load(_full_spec(), tmp_path, monkeypatch, "sb3_tools")
    agent = module.build_agent("u", "s")
    assert sorted(agent.tool_registry.registry) == [
        "file_write", "python_repl", "shell", "shout", "use_aws", "word_count"]
    registry = agent.tool_registry.registry
    assert registry["word_count"].tool_spec["description"] == "Count words in a text."
    assert module.DEFAULT_TOOL_DESCRIPTIONS["shout"] == "Shout a text."
    assert registry["shout"].tool_spec["inputSchema"]["json"]["required"] == ["text"]
    assert module.DEFAULT_TOOL_DESCRIPTIONS["python_repl"].startswith("Run Python code")
    # the runtime tool is the member's own function, imported from the zip
    assert module.CUSTOM_TOOLS[0]._tool_func("a b a", unique=True) == 2
    # sandbox tools never run here: each is a Code Interpreter call
    assert module.launchpad_sandbox_python("print(6 * 7)").strip() == "42"
    box = FakeSandbox.instances[0]
    assert box.started == "aws.codeinterpreter.v1" and box.region == REGION
    assert module.launchpad_sandbox_python("x = 5") == "(no output)"
    assert module.launchpad_sandbox_python("print(x)").strip() == "5"  # state carries
    assert module.launchpad_sandbox_shell("ls") == "ran: ls"
    assert module.launchpad_sandbox_file_write("out/a.txt", "hi") == "wrote out/a.txt"
    assert box.files == {"out/a.txt": "hi"}
    assert module.launchpad_sandbox_file_write("../a", "x").startswith("refused")
    assert module.launchpad_sandbox_use_aws("s3", "ListBuckets").startswith("refused")
    module.launchpad_sandbox_use_aws("s3", "list_buckets", {"MaxBuckets": 1})
    sent = box.calls[-1][1]["code"]
    assert "boto3.client('s3'" in sent and "'list_buckets'" in sent
    # the sandboxed custom tool: its source runs in the sandbox, in a fresh namespace
    out = module.launchpad_sandbox_custom("shout", SANDBOXED, {"text": "hey"})
    assert out == {"text": json.dumps({"loud": "HEY", "unique": False}), "is_error": False}
    assert "shout" not in box.ns and "x" in box.ns
    failed = module.launchpad_sandbox_custom("shout", SANDBOXED, {"wrong": 1})
    assert failed["is_error"] and "TypeError" in failed["text"]


def test_one_turn_through_a_sandboxed_custom_tool(tmp_path, monkeypatch):
    module = _load(_full_spec(), tmp_path, monkeypatch, "sb3_turn")
    fake_model = ToolModel("shout", {"text": "quiet"})
    monkeypatch.setattr(module, "build_model", lambda: fake_model)
    out = _drain(module.invoke({"prompt": "shout quiet", "actor_id": "bob"}))
    assert {"event": "tool", "name": "shout", "id": "t1"} in out
    assert out[-1] == {"event": "complete", "result": "Done.\n"}
    result = fake_model.seen[1]["results"][0]
    assert result["status"] == "success"
    assert json.loads(result["content"][0]["text"]) == {"loud": "QUIET", "unique": False}


def test_a_broken_custom_tool_loads_as_one_that_says_so(tmp_path, monkeypatch):
    broken = CLEAN.replace("from strands import tool", "import json\nraise_at_import = 1 / 0")
    spec = _spec(custom_tools=[_tool(broken)])
    module = _load(spec, tmp_path, monkeypatch, "sb3_broken")
    stub = module.CUSTOM_TOOLS[0]
    assert stub.tool_name == "word_count"
    result = stub._tool_func({"toolUseId": "t", "input": {"text": "a"}})
    assert result["status"] == "error" and "ZeroDivisionError" in result["content"][0]["text"]


def test_session_path_stays_inside(tmp_path, monkeypatch):
    spec = _spec(custom_tools=[_tool()])
    module = _load(spec, tmp_path, monkeypatch, "sb3_session")
    assert module.SESSION_DIR == Path(journal_root(spec))
    assert str(module.session_path("a", "b.txt")).endswith("/a/b.txt")
    with pytest.raises(ValueError, match="outside"):
        module.session_path("..", "etc", "passwd")


def test_a2a_agents_get_the_tools_too():
    # the a2a server package is not a backend dependency: render + compile only
    source = render_a2a_main_py(_full_spec(protocol="a2a"))
    compile(source, "sb3_a2a.py", "exec")
    assert '"tools": list(STRANDS_TOOLS) or [calculator, current_utc_time],' in source
    assert source.index("STRANDS_TOOLS = _launchpad_strands_tools()") < source.index(
        "STRANDS_TOOLS = [*STRANDS_TOOLS, *SANDBOX_TOOLS, *CUSTOM_TOOLS]")
    assert "'execution': 'code_interpreter'" in source
