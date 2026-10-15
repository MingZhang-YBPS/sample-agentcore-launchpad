"""WIRE-A: the deploy paths read an isolated project's own resources (projects design
§10.4 memory, §10.6 gateway credentials, §10.10 prefixes) and stay unchanged for every
shared project; the KB migration routes; Lambda target ownership; the assistant's
storage catalog (storage-mounts §8.3) and the canary storage confirmation (§8.2).

AWS is stubbed throughout.
"""

import io
import json
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.assistant import proposal as proposal_contract
from app.assistant import service as assistant
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.deployer import harness as harness_deployer
from app.deployer.environment import runtime_environment
from app.models.project_isolation import (
    KIND_CEDAR_POLICY,
    KIND_M2M_CLIENT,
    KIND_M2M_PROVIDER,
    KIND_MEMORY,
    ProjectResource,
)
from app.models.projects import Project
from app.optimization import canary_infra
from app.schemas.agent import AgentSpec, KnowledgeBaseRef, ToolRef
from app.services import agent_iam, byoc_uploads, gateway_tools, project_gateway, registry_console
from app.services import project_isolation as svc
from app.services.agentcore import harness as hc
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext
from tests.test_project_scope import _project, app  # noqa: F401

WS = DEFAULT_WORKSPACE_ID
SHARED_PROVIDER = (
    "arn:aws:bedrock-agentcore:us-west-2:111122223333:token-vault/default/"
    "oauth2credentialprovider/launchpad-gw-m2m"
)
RESOURCES = {
    "memory_id": "launchpad_memory-shared",
    "memory_arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:memory/launchpad_memory-shared",
    "gateway_id": "gw-1",
    "gateway_arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/gw-1",
    "gateway_url": "https://gw-1.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
    "kb_gateway_id": "kbgw-1",
    "kb_gateway_arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/kbgw-1",
    "oauth_provider_arn": SHARED_PROVIDER,
    "execution_role_arn": "arn:aws:iam::111122223333:role/launchpad-agent-execution-role",
    "artifacts_bucket": "bkt",
}
PROJECT_SCOPES = ["launchpad-gw/invoke", "launchpad-gw/project.alpha"]


def _ws() -> WorkspaceContext:
    return WorkspaceContext(account_id="111122223333", region="us-west-2",
                            resources=dict(RESOURCES), id=WS)


def _isolate(pid: str) -> None:
    _project(pid)
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(Project, pid)
            row.settings = {**(row.settings or {}), "isolation": {"mode": svc.MODE_ISOLATED}}
            db.commit()
    finally:
        db.close()


def _provision(pid: str) -> str:
    """The project's ready principal and memory; returns its provider ARN."""
    names = svc.resource_names(pid)
    provider = f"arn:aws:bedrock-agentcore:us-west-2:111122223333:token-vault/default/" \
               f"oauth2credentialprovider/{names.m2m_provider}"
    db = SessionLocal()
    try:
        for kind, rid, arn in (
            (KIND_M2M_CLIENT, f"client-{pid}", None),
            (KIND_M2M_PROVIDER, names.m2m_provider, provider),
            (KIND_CEDAR_POLICY, "eng-1", "arn:engine"),
            (KIND_MEMORY, f"launchpad_mem_{pid}-0001",
             f"arn:aws:bedrock-agentcore:us-west-2:111122223333:memory/launchpad_mem_{pid}-0001"),
        ):
            db.merge(ProjectResource(project_id=pid, workspace_id=WS, kind=kind,
                                     resource_id=rid, arn=arn, status="ready",
                                     detail={"policies": []}))
        db.commit()
    finally:
        db.close()
    return provider


@pytest.fixture
def alpha(app):  # noqa: F811
    _isolate("alpha")
    return _provision("alpha")


GATEWAY_SPEC = AgentSpec(name="iso-agent", method="zip_runtime", system_prompt="hi",
                         tools=[ToolRef(type="gateway", name="launchpad-gw")])
KB_SPEC = AgentSpec(name="iso-kb", method="harness", system_prompt="hi",
                    knowledge_bases=[KnowledgeBaseRef(kb_id="KB1", name="docs")])


# ── code runtimes: LAUNCHPAD_GATEWAY_* and LAUNCHPAD_MEMORY_ID ───────────────


def test_a_shared_project_keeps_the_workspace_gateway_and_memory(app):  # noqa: F811
    _project("beta")
    with project_context("beta"):
        env = runtime_environment(GATEWAY_SPEC, RESOURCES, workspace_id=WS)
    assert env["LAUNCHPAD_GATEWAY_PROVIDER"] == "launchpad-gw-m2m"
    assert env["LAUNCHPAD_GATEWAY_SCOPE"] == "launchpad-gw/invoke"
    assert env["LAUNCHPAD_MEMORY_ID"] == "launchpad_memory-shared"


def test_an_isolated_agent_gets_its_projects_provider_scopes_and_memory(alpha):
    with project_context("alpha"):
        env = runtime_environment(GATEWAY_SPEC, RESOURCES, workspace_id=WS)
    assert env["LAUNCHPAD_GATEWAY_PROVIDER"] == svc.resource_names("alpha").m2m_provider
    assert env["LAUNCHPAD_GATEWAY_SCOPE"] == ",".join(PROJECT_SCOPES)
    assert env["LAUNCHPAD_MEMORY_ID"] == "launchpad_mem_alpha-0001"
    # a spec-pinned memory still wins
    pinned = GATEWAY_SPEC.model_copy(update={"memory": GATEWAY_SPEC.memory.model_copy(
        update={"memory_id": "team_notes-XYZ"})})
    with project_context("alpha"):
        assert runtime_environment(pinned, RESOURCES, workspace_id=WS)[
            "LAUNCHPAD_MEMORY_ID"] == "team_notes-XYZ"


# ── harnesses: outboundAuth and the memory ARN ───────────────────────────────


def _kb_auth(params: dict[str, Any]) -> dict[str, Any]:
    tool = next(t for t in params["tools"] if t["name"] == "launchpad_kb_gw")
    return tool["config"]["agentCoreGateway"]["outboundAuth"]["oauth"]


def test_a_harness_of_an_isolated_project_uses_its_provider_and_memory(alpha, monkeypatch):
    monkeypatch.setattr(registry_console, "resolve_gateway_attachments", lambda *_a, **_k: [])
    with project_context("alpha"):
        params = harness_deployer._build_live_params(KB_SPEC, _ws())
    assert _kb_auth(params) == {"providerArn": alpha, "grantType": "CLIENT_CREDENTIALS",
                                "scopes": PROJECT_SCOPES}
    assert params["memory"]["agentCoreMemoryConfiguration"]["arn"].endswith(
        "memory/launchpad_mem_alpha-0001")


def test_a_harness_of_a_shared_project_is_unchanged(app, monkeypatch):  # noqa: F811
    _project("beta")
    monkeypatch.setattr(registry_console, "resolve_gateway_attachments", lambda *_a, **_k: [])
    with project_context("beta"):
        params = harness_deployer._build_live_params(KB_SPEC, _ws())
    assert _kb_auth(params) == {"providerArn": SHARED_PROVIDER,
                                "grantType": "CLIENT_CREDENTIALS",
                                "scopes": ["launchpad-gw/invoke"]}
    assert params["memory"]["agentCoreMemoryConfiguration"]["arn"] == RESOURCES["memory_arn"]


def test_pinned_kb_bindings_keep_their_scopes():
    pin = {"bindings": {"resources": {"kb_gateway": {
        "gateway_arn": RESOURCES["kb_gateway_arn"], "oauth_provider_arn": "arn:p",
        "scopes": PROJECT_SCOPES}}}}
    params = harness_deployer._pinned_params(KB_SPEC, _ws(), pin)
    assert _kb_auth(params)["scopes"] == PROJECT_SCOPES
    # an approval pinned before the scopes were recorded keeps the workspace scope
    del pin["bindings"]["resources"]["kb_gateway"]["scopes"]
    assert _kb_auth(harness_deployer._pinned_params(KB_SPEC, _ws(), pin))["scopes"] == [
        "launchpad-gw/invoke"]


def test_the_legacy_launchpad_gw_attachment_takes_the_project_credentials(alpha):
    tools = [ToolRef(type="gateway", name="launchpad-gw")]
    with project_context("alpha"):
        (attachment,) = registry_console.resolve_gateway_attachments(tools, _ws())
    assert attachment["outbound_auth"]["oauth"]["providerArn"] == alpha
    assert attachment["outbound_auth"]["oauth"]["scopes"] == PROJECT_SCOPES


def test_the_logged_in_chat_path_mounts_the_kb_gateway_with_the_project_provider(alpha):
    spec = {"knowledge_bases": [{"kb_id": "KB1"}], "tools": []}
    with project_context("alpha"):
        (kb,) = hc.user_authenticated_tools(spec, RESOURCES, "tok", workspace_id=WS)
        (legacy,) = hc.user_authenticated_tools(spec, RESOURCES, "tok")
    oauth = kb["config"]["agentCoreGateway"]["outboundAuth"]["oauth"]
    assert oauth["providerArn"] == alpha and oauth["scopes"] == PROJECT_SCOPES
    # without the workspace id: the workspace provider, as before
    assert legacy["config"]["agentCoreGateway"]["outboundAuth"]["oauth"][
        "providerArn"] == SHARED_PROVIDER


# ── the per-agent role ───────────────────────────────────────────────────────


def test_the_role_context_names_the_projects_memory_and_provider(alpha):
    ctx = agent_iam.role_context(_ws(), "alpha")
    assert ctx.memory_id == "launchpad_mem_alpha-0001"
    assert ctx.oauth_provider_arn == alpha
    _project("beta")
    shared = agent_iam.role_context(_ws(), "beta")
    assert shared.memory_id == "launchpad_memory-shared"
    assert shared.oauth_provider_arn == SHARED_PROVIDER


# ── §10.10 prefixes: canary zips and BYOC uploads ────────────────────────────


def test_canary_zips_of_an_isolated_project_sit_under_its_prefix(alpha):
    assert canary_infra.candidate_s3_key("a", "c1", "candidate", "alpha") == \
        "projects/alpha/agents/a/canary/c1-candidate.zip"
    _project("beta")
    assert canary_infra.candidate_s3_key("a", "c1", "candidate", "beta") == \
        "agents/a/canary/c1-candidate.zip"
    assert canary_infra.candidate_s3_key("a", "c1", "candidate") == \
        "agents/a/canary/c1-candidate.zip"


class _S3:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.deleted: list[str] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def download_file(self, bucket: str, key: str, dest: str) -> None:
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        with open(dest, "wb") as out:
            out.write(self.objects[key])

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        self.deleted.append(Key)


def test_byoc_uploads_of_an_isolated_project_use_its_prefix_and_find_older_ones(
        alpha, tmp_path):
    assert byoc_uploads.source_key(WS, "up1", "alpha") == f"projects/alpha/byoc/{WS}/up1/source.zip"
    assert byoc_uploads.manifest_key(WS, "up1") == f"byoc/{WS}/up1/manifest.json"
    legacy = _S3({byoc_uploads.manifest_key(WS, "old"): json.dumps({"upload_id": "old"}).encode(),
                  byoc_uploads.source_key(WS, "old"): b"zip"})
    current = _S3({byoc_uploads.manifest_key(WS, "new", "alpha"): b'{"upload_id": "new"}',
                   byoc_uploads.source_key(WS, "new", "alpha"): b"zip2"})
    with project_context("alpha"):
        # staged before the project was isolated: the workspace key
        assert byoc_uploads.get_manifest(_ws(), "old", s3_client=legacy)["upload_id"] == "old"
        byoc_uploads.download_upload(_ws(), WS, "old", tmp_path / "a.zip", s3_client=legacy)
        assert byoc_uploads.get_manifest(_ws(), "new", s3_client=current)["upload_id"] == "new"
        byoc_uploads.download_upload(_ws(), WS, "new", tmp_path / "b.zip", s3_client=current)
        with pytest.raises(Exception, match="upload not found"):
            byoc_uploads.get_manifest(_ws(), "gone", s3_client=current)
        byoc_uploads.delete_upload_objects(_ws(), WS, "new", s3_client=current)
    assert (tmp_path / "b.zip").read_bytes() == b"zip2"
    assert set(current.deleted) == {
        byoc_uploads.source_key(WS, "new", "alpha"), byoc_uploads.source_key(WS, "new"),
        byoc_uploads.manifest_key(WS, "new", "alpha"), byoc_uploads.manifest_key(WS, "new")}


# ── Lambda targets: ownership and the project's Cedar ────────────────────────


def test_a_published_lambda_target_is_owned_and_a_deleted_one_forgotten(alpha, monkeypatch):
    scheduled: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(project_gateway, "schedule_reconcile",
                        lambda pid, ws, wait_target=None: scheduled.append((pid, ws, wait_target)))
    creator = gateway_tools.creators.Creator(user_id=None, name="dana")
    with project_context("alpha"):
        gateway_tools._owned(WS, "tgt-1", creator)
    db = SessionLocal()
    try:
        assert project_gateway.target_owner(db, "tgt-1") == (WS, "alpha")
    finally:
        db.close()
    assert scheduled == [("alpha", WS, "tgt-1")]
    gateway_tools._disowned("tgt-1")
    db = SessionLocal()
    try:
        assert project_gateway.target_owner(db, "tgt-1") is None
    finally:
        db.close()
    assert scheduled[-1] == ("alpha", WS, None)


# ── the KB migration routes ──────────────────────────────────────────────────


def test_kb_migration_routes_call_the_project_kb_service(monkeypatch):
    from app.routers import knowledge as router

    calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(router, "_require_visible", lambda db, ws, viewer, kb_id, own=False: None)
    monkeypatch.setattr(router.project_kb, "migrate_kb",
                        lambda ctx, kb_id, pid: calls.append(("migrate", kb_id, pid)) or {"ok": 1})
    monkeypatch.setattr(router.project_kb, "finish_kb_migration",
                        lambda ctx, kb_id, pid: calls.append(("finish", kb_id, pid)) or {"ok": 2})
    ws = SimpleNamespace(id=WS, context=object())
    with project_context("alpha"):
        assert router.migrate_knowledge_base("KB1", db=None, ws=ws, viewer=Viewer.ALL) == {"ok": 1}
        assert router.finish_knowledge_base_migration(
            "KB1", db=None, ws=ws, viewer=Viewer.ALL) == {"ok": 2}
    assert calls == [("migrate", "KB1", "alpha"), ("finish", "KB1", "alpha")]


def test_another_projects_kb_is_refused_before_the_migration(monkeypatch):
    from app.core.errors import NotFoundError
    from app.routers import knowledge as router

    monkeypatch.setattr(router.creators, "outside_current_project", lambda *_a: {"KB1"})
    monkeypatch.setattr(router.visibility, "aws_id_visible", lambda *_a: True)
    monkeypatch.setattr(router.project_kb, "migrate_kb",
                        lambda *_a: pytest.fail("must not reach AWS"))
    with pytest.raises(NotFoundError):
        router.migrate_knowledge_base("KB1", db=None, ws=SimpleNamespace(id=WS, context=None),
                                      viewer=Viewer.ALL)


# ── the assistant: storage in the catalog, the contract and approval ────────


STORAGE = [{"storage_id": "st-1", "name": "shared-notes", "kind": "s3files", "max_mode": "rw",
            "network_profile_id": "np-1", "status": "active", "access_point_arn": "arn:ap"}]


def test_the_restricted_catalog_carries_the_projects_storage(monkeypatch):
    monkeypatch.setattr(proposal_contract, "storage_catalog", lambda db, ws, pid: list(STORAGE))
    catalog = assistant.restrict_catalog(None, WS, Viewer.ALL, {"knowledge_bases": []})
    assert catalog["storage"] == STORAGE
    # no storage entries: the snapshot itself, as before
    monkeypatch.setattr(proposal_contract, "storage_catalog", lambda db, ws, pid: [])
    snapshot = {"knowledge_bases": []}
    assert assistant.restrict_catalog(None, WS, Viewer.ALL, snapshot) is snapshot


def test_owner_catalog_reads_storage_even_without_knowledge_bases(monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(assistant, "owner_viewer", lambda *_a: Viewer.ALL)
    monkeypatch.setattr(assistant, "restrict_catalog",
                        lambda db, ws, viewer, catalog: seen.append(catalog) or catalog)
    conversation = SimpleNamespace(catalog={"tools": []}, workspace_id=WS, owner_principal="p")
    assistant.owner_catalog(object(), conversation)
    assert seen == [{"tools": []}]
    assert assistant.owner_catalog(None, conversation) == {"tools": []}


def test_the_catalog_section_lists_storage_ids_but_never_arns():
    text = assistant.catalog_section({"storage": STORAGE + [
        {"storage_id": "st-2", "name": "blocked", "status": "blocked"}]})
    assert "`st-1` — shared-notes · s3files · max mode `rw` · network `np-1`" in text
    assert "st-2" not in text and "arn:ap" not in text
    assert "Storage" not in assistant.catalog_section({})


def test_the_proposal_contract_documents_storage():
    text = assistant.PROTOCOL_PREAMBLE
    assert '`storage`: optional list of `{"storage_id": "<catalog storage id>"' in text
    assert "never put ARNs, paths or mount points here" in text


# ── canaries: a read-write shared mount needs the starter's confirmation ────


def test_the_canary_request_carries_confirm_storage():
    from app.optimization.canary_routers import RuntimeCanaryCreate

    assert RuntimeCanaryCreate(agent_id="a").confirm_storage is False
    assert RuntimeCanaryCreate(agent_id="a", confirm_storage=True).confirm_storage is True
