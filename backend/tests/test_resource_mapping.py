"""T23 — logical resource mapping: CRUD, spec resolution, and the promotion gate."""

import copy

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ReleaseBundle, Workspace
from app.services import promotion as promotion_service
from app.services import resource_mapping as rm

SPEC = {
    "name": "hr-bot",
    "method": "harness",
    "system_prompt": "help",
    "knowledge_bases": [{"kb_id": "DEVKB12345", "name": "HR Policy"}],
    "memory": {"short_term": True, "memory_id": "hr_mem-dev1"},
    "tools": [
        {"type": "builtin", "name": "code-interpreter"},
        {
            "type": "gateway",
            "name": "Jira",
            "config": {"gateway_id": "gw-dev-1", "record_id": "rec-dev-1"},
        },
    ],
    "skills": ["s3://launchpad-artifacts-dev/agent-skills/ab12/triage/", "/local/skill"],
}


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        session.add(
            Workspace(
                id="prod-ws", name="Prod", account_id="444455556677", region="us-west-2",
                tier="prod", bootstrap_status="ready",
                resources={"gateway_id": "gw-prod-default", "memory_id": "shared-prod"},
            )
        )
        session.commit()
        yield session
    finally:
        session.close()


def _map(db, kind, name, rid, ws="prod-ws"):
    rm.upsert_mapping(
        db, workspace_id=ws, kind=kind, name=name, resource_id=rid, note=None, updated_by="t"
    )
    db.commit()


def test_extract_finds_only_environment_specific_ids():
    refs = rm.extract_references(SPEC)
    assert [(r.kind, r.source_id, r.path_text) for r in refs] == [
        ("kb", "DEVKB12345", "knowledge_bases[0].kb_id"),
        ("memory", "hr_mem-dev1", "memory.memory_id"),
        ("gateway", "gw-dev-1", "tools[1].config.gateway_id"),
        ("mcp_record", "rec-dev-1", "tools[1].config.record_id"),
        ("skill", "s3://launchpad-artifacts-dev/agent-skills/ab12/triage/", "skills[0]"),
    ]


def test_resolution_rewrites_a_copy_and_reports_what_is_missing(db):
    _map(db, "kb", "hr-policy", "PRODKB9999")
    _map(db, "memory", "hr_mem-dev1", "hr_mem-prod")
    before = copy.deepcopy(SPEC)
    result = rm.resolve_spec(
        db, SPEC, source_workspace_id=DEFAULT_WORKSPACE_ID, target_workspace_id="prod-ws"
    )
    assert SPEC == before  # input never mutated
    assert result.spec["knowledge_bases"][0]["kb_id"] == "PRODKB9999"
    assert result.spec["memory"]["memory_id"] == "hr_mem-prod"
    assert not result.complete
    missing = {u["logical"]: u for u in result.unmapped}
    assert set(missing) == {"gateway:jira", "mcp_record:jira", "skill:triage"}
    assert missing["gateway:jira"]["source_id"] == "gw-dev-1"
    assert missing["gateway:jira"]["path"] == "tools[1].config.gateway_id"
    assert "prod-ws" in missing["gateway:jira"]["reason"]
    # an unmapped reference keeps its source value, flagged rather than silently swapped
    assert result.spec["tools"][1]["config"]["gateway_id"] == "gw-dev-1"


def test_a_source_side_mapping_names_the_reference(db):
    # the dev team named its KB `handbook` on purpose; the target is asked for that name
    _map(db, "kb", "handbook", "DEVKB12345", ws=DEFAULT_WORKSPACE_ID)
    _map(db, "kb", "handbook", "PRODKB1", ws="prod-ws")
    result = rm.resolve_spec(
        db, SPEC, source_workspace_id=DEFAULT_WORKSPACE_ID, target_workspace_id="prod-ws"
    )
    assert result.spec["knowledge_bases"][0]["kb_id"] == "PRODKB1"
    assert next(r for r in result.resolved if r["kind"] == "kb")["logical"] == "kb:handbook"


def test_a_workspaces_own_shared_gateway_needs_no_mapping(db):
    spec = {"tools": [{"type": "gateway", "name": "gw", "config": {"gateway_id": "gw-dev-1"}}]}
    result = rm.resolve_spec(
        db, spec, source_workspace_id=DEFAULT_WORKSPACE_ID, target_workspace_id="prod-ws",
        source_resources={"gateway_id": "gw-dev-1"},
        target_resources={"gateway_id": "gw-prod-default"},
    )
    assert result.complete
    assert result.spec["tools"][0]["config"]["gateway_id"] == "gw-prod-default"
    assert result.resolved[0]["via"] == "workspace-default"


def test_a_spec_without_references_is_trivially_complete(db):
    result = rm.resolve_spec(
        db, {"name": "x"}, source_workspace_id=None, target_workspace_id="prod-ws"
    )
    assert result.complete and result.resolved == []


@pytest.mark.parametrize(
    ("kind", "name", "rid"),
    [
        ("kb", "Bad Name", "ABC"),
        ("kb", "ok", "has space"),
        ("skill", "ok", "/not/s3"),
        ("nope", "ok", "x"),
    ],
)
def test_invalid_mappings_are_refused(kind, name, rid):
    with pytest.raises(Exception) as err:
        rm.validate_mapping(kind, name, rid)
    assert getattr(err.value, "status_code", None) == 422


def test_the_gate_lists_unmapped_references_for_the_approver(db):
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="hr-bot", method="harness",
                  status="active", spec=dict(SPEC))
    db.add(agent)
    db.flush()
    bundle = ReleaseBundle(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name="hr-bot",
        method="harness", spec=dict(SPEC), artifact={}, digest="d" * 64,
    )
    db.add(bundle)
    db.commit()
    target = db.get(Workspace, "prod-ws")

    gates = promotion_service.evaluate_gates(db, bundle, target)
    gate = next(c for c in gates["checks"] if c["key"] == "resource_mapping")
    assert gate["ok"] is False and "resource_mapping" in gates["blocking_failures"]
    assert len(gate["unmapped"]) == 5
    assert "unmapped in prod-ws" in gate["detail"]

    for kind, name, rid in [
        ("kb", "hr-policy", "PRODKB1"),
        ("memory", "hr_mem-dev1", "m_prod"),
        ("gateway", "jira", "gw-p"),
        ("mcp_record", "jira", "rec-p"),
        ("skill", "triage", "s3://launchpad-artifacts-prod/agent-skills/x/triage/"),
    ]:
        _map(db, kind, name, rid)
    gates = promotion_service.evaluate_gates(db, bundle, target)
    gate = next(c for c in gates["checks"] if c["key"] == "resource_mapping")
    assert gate["ok"] is True and gate["unmapped"] == []


# ── routes ───────────────────────────────────────────────────────────────────────


def test_mapping_crud_is_scoped_to_the_selected_workspace(client, db):
    headers = {"X-Workspace": "prod-ws"}
    put = client.put(
        "/api/resource-mappings/kb/hr-policy",
        json={"resource_id": "PRODKB1", "note": "prod handbook"},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    assert put.json()["key"] == "kb:hr-policy"
    again = client.put(
        "/api/resource-mappings/kb/hr-policy", json={"resource_id": "PRODKB2"}, headers=headers
    )
    assert again.json()["resource_id"] == "PRODKB2"  # upsert, not a second row

    listed = client.get("/api/resource-mappings", headers=headers).json()
    assert [m["resource_id"] for m in listed["mappings"]] == ["PRODKB2"]
    assert client.get("/api/resource-mappings").json()["mappings"] == []  # default ws

    bad = client.put(
        "/api/resource-mappings/kb/hr-policy", json={"resource_id": "no spaces!"}, headers=headers
    )
    assert bad.status_code == 422
    assert client.delete("/api/resource-mappings/kb/hr-policy", headers=headers).json() == {
        "deleted": True, "key": "kb:hr-policy",
    }
    assert client.delete("/api/resource-mappings/kb/hr-policy", headers=headers).status_code == 404


def test_the_resolution_route_previews_a_bundle(client, db):
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="hr-bot", method="harness",
                  status="active", spec=dict(SPEC))
    db.add(agent)
    db.flush()
    bundle = ReleaseBundle(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name="hr-bot",
        method="harness", spec=dict(SPEC), artifact={}, digest="e" * 64,
    )
    db.add(bundle)
    db.commit()
    _map(db, "kb", "hr-policy", "PRODKB1")

    body = client.get(
        f"/api/release-bundles/{bundle.id}/resolution", params={"target_workspace_id": "prod-ws"}
    ).json()
    assert body["complete"] is False
    assert body["spec"]["knowledge_bases"][0]["kb_id"] == "PRODKB1"
    assert len(body["unmapped"]) == 4
    missing = client.get(
        "/api/release-bundles/nope/resolution", params={"target_workspace_id": "prod-ws"}
    )
    assert missing.status_code == 404
