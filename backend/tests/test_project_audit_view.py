"""A project's audit view and export (projects design §11.6, G11-audit): the view holds
the project's own rows (every workspace) and the structure rows naming it, nothing of
another project; `pperm:project.audit` (non-member 404, member without the key 403);
filters, keyset paging, CSV / JSONL export equal to the query; `outcome` and the
refusals journaled by the route policy; the additive migration."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, insert, inspect, select, text

from app.core import actor as actor_mod
from app.core import db as db_mod
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.models.ledger import AuditEvent
from app.services import audit as audit_svc
from tests.test_project_scope import (  # noqa: F401
    ADMIN,
    PEER,
    _agent,
    _login,
    _member_of,
    _project,
    _user,
    app,
)

WS = DEFAULT_WORKSPACE_ID
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_current_actor():
    actor_mod.set_current_actor(None, None)
    yield
    actor_mod.set_current_actor(None, None)


def _admin(app):  # noqa: F811
    from fastapi.testclient import TestClient

    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


def _row(project_id, action, *, actor="li.na", target="", at=T0, outcome="success",
         workspace_id=WS) -> None:
    db = SessionLocal()
    try:
        db.execute(insert(AuditEvent).values(
            workspace_id=workspace_id, project_id=project_id, actor=actor, action=action,
            target=target, created_at=at, outcome=outcome,
        ))
        db.commit()
    finally:
        db.close()


def _rows(**where) -> list[AuditEvent]:
    db = SessionLocal()
    try:
        with project_context(None):
            query = select(AuditEvent).order_by(AuditEvent.created_at)
            for column, value in where.items():
                query = query.where(getattr(AuditEvent, column) == value)
            return list(db.scalars(query))
    finally:
        db.close()


@pytest.fixture
def world(app):  # noqa: F811
    """alpha (owner olga, developer dev), beta (owner bob), alpha-b (a prefix twin)."""
    for pid in ("alpha", "beta", "alpha-b"):
        _project(pid)
    olga, dev, bob = (_user(n, grant=False) for n in ("olga", "dev", "bob"))
    _member_of("alpha", olga, "project_owner")
    _member_of("alpha", dev, "developer")
    _member_of("beta", bob, "project_owner")
    _row("alpha", "release.sign", target="agent risk-bot v12", at=T0)
    _row("alpha", "promotion.request", actor="zhang.san", at=T0 + timedelta(hours=1))
    _row("alpha", "golden.seed", actor="zhang.san", at=T0 + timedelta(hours=2),
         outcome="failed", workspace_id="other-ws")  # alpha is the project, any workspace
    _row(None, "project.members.update", actor="admin", target="project:alpha user:zhao",
         at=T0 + timedelta(hours=3))
    _row("beta", "project.transfer", actor="admin",
         target="agent:a1 project:alpha->project:beta", at=T0 + timedelta(hours=4))
    # not alpha's: another project, a prefix twin, a non-structure row naming it
    _row("beta", "release.sign", actor="bob", at=T0 + timedelta(hours=5))
    _row(None, "project.create", actor="admin", target="project:alpha-b",
         at=T0 + timedelta(hours=6))
    _row(None, "user.update", actor="admin", target="project:alpha", at=T0 + timedelta(hours=7))
    return app


def _events(client, pid="alpha", **params):
    res = client.get(f"/api/projects/{pid}/audit", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def test_the_view_holds_the_project_and_the_structure_rows_naming_it(world):
    olga = _login(world, "olga")
    body = _events(olga)
    actions = [e["action"] for e in body["events"]]
    assert actions == ["project.transfer", "project.members.update", "golden.seed",
                       "promotion.request", "release.sign"]  # newest first
    assert body["next_cursor"] is None
    assert body["events"][2]["outcome"] == "failed"
    assert body["events"][2]["workspace_id"] == "other-ws"
    assert set(body["facets"]["actors"]) == {"admin", "li.na", "zhang.san"}
    assert "release.sign" in body["facets"]["actions"]
    # the transfer is both projects' history
    beta = [e["action"] for e in _events(_login(world, "bob"), "beta")["events"]]
    assert beta == ["release.sign", "project.transfer"]


def test_who_may_read_it(world):
    olga, dev, bob = (_login(world, n) for n in ("olga", "dev", "bob"))
    res = olga.get("/api/projects/beta/audit")  # not hers: 404, never 403
    assert res.status_code == 404 and res.json()["code"] == "project.not_found"
    res = dev.get("/api/projects/alpha/audit")  # member without project.audit
    assert res.status_code == 403 and res.json()["code"] == "auth.permission_required"
    assert bob.get("/api/projects/alpha/audit/export").status_code == 404
    admin = _admin(world)
    events = _events(admin)["events"]  # the five, and the two refusals above
    assert len(events) == 7 and [e["outcome"] for e in events[:2]] == ["refused"] * 2
    assert admin.get("/api/projects/nowhere/audit").status_code == 404


def test_refusals_are_journaled_in_the_refused_project(world):
    _login(world, "dev").get("/api/projects/alpha/audit")
    _login(world, "bob").get("/api/projects/alpha/audit")
    refused = _rows(outcome="refused")
    assert [(r.actor, r.project_id) for r in refused] == [("dev", "alpha"), ("bob", "alpha")]
    assert refused[0].action == "GET /api/projects/{project_id}/audit"
    assert refused[0].target == "/api/projects/alpha/audit [auth.permission_required]"
    assert refused[1].target.endswith("[project.not_found]")
    assert refused[0].actor_user_id  # the account's id
    # a missing project names nothing: no row
    _login(world, "bob").get("/api/projects/nowhere/audit")
    assert len(_rows(outcome="refused")) == 2
    olga = _login(world, "olga")
    shown = _events(olga, outcome="refused")["events"]
    assert {e["actor"] for e in shown} == {"dev", "bob"}


def test_a_project_permission_refusal_inside_a_workspace_is_journaled(world):
    agent = _agent("risk-bot", "alpha")
    viewer = _user("vera", grant=False)
    _member_of("alpha", viewer, "sme")  # project.use only: no agents.delete
    res = _login(world, "vera").delete(f"/api/agents/{agent}", headers={"X-Project": "alpha"})
    assert res.status_code == 403
    (row,) = _rows(outcome="refused")
    assert (row.project_id, row.workspace_id, row.actor) == ("alpha", WS, "vera")
    assert row.action == "DELETE /api/agents/{agent_id}"


def test_filters(world):
    olga = _login(world, "olga")

    def actions(**params):
        return [e["action"] for e in _events(olga, **params)["events"]]

    assert actions(action="release.") == ["release.sign"]
    assert actions(actor="zhang.san") == ["golden.seed", "promotion.request"]
    assert actions(outcome="failed") == ["golden.seed"]
    since, until = (T0 + timedelta(hours=1)).isoformat(), (T0 + timedelta(hours=3)).isoformat()
    assert actions(**{"from": since, "to": until}) == ["golden.seed", "promotion.request"]
    # an offset is read as the instant it names
    local = (T0 + timedelta(hours=2)).astimezone(timezone(timedelta(hours=8))).isoformat()
    assert actions(**{"from": local}) == ["project.transfer", "project.members.update",
                                          "golden.seed"]
    res = olga.get("/api/projects/alpha/audit", params={"outcome": "maybe"})
    assert res.status_code == 400 and res.json()["code"] == "audit.invalid_outcome"
    res = olga.get("/api/projects/alpha/audit", params={"cursor": "garbage|x"})
    assert res.status_code == 400 and res.json()["code"] == "audit.invalid_cursor"


def test_keyset_paging_walks_every_row_once(world):
    for i in range(7):  # same instant: the id breaks the tie
        _row("alpha", f"kb.create.{i}", at=T0 + timedelta(days=1))
    olga = _login(world, "olga")
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = _events(olga, **params)
        assert ("facets" in body) == (cursor is None)
        seen += [e["id"] for e in body["events"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 4 and len(seen) == len(set(seen)) == 12
    assert seen == [e["id"] for e in _events(olga, limit=500)["events"]]


def test_export_matches_the_query_and_is_journaled(world):
    olga = _login(world, "olga")
    res = olga.get("/api/projects/alpha/audit/export", params={"actor": "zhang.san"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert 'filename="audit-alpha-' in res.headers["content-disposition"]
    table = list(csv.DictReader(io.StringIO(res.text)))
    queried = _events(olga, actor="zhang.san")["events"]
    assert [r["id"] for r in table] == [e["id"] for e in queried]
    assert table[0]["outcome"] == "failed" and list(table[0]) == list(audit_svc.EVENT_FIELDS)
    # the download is in the journal, and so in the next export
    (journaled,) = _rows(action="audit.export")
    assert (journaled.project_id, journaled.actor) == ("alpha", "olga")
    res = olga.get("/api/projects/alpha/audit/export", params={"format": "jsonl"})
    assert res.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in res.text.splitlines()]
    assert lines == _events(olga, limit=500)["events"]
    assert [e["action"] for e in lines[:2]] == ["audit.export", "audit.export"]
    assert olga.get("/api/projects/alpha/audit/export",
                    params={"format": "xml"}).status_code == 422


def test_record_defaults_to_success_and_rejects_unknown_outcomes():
    audit_svc.record_audit_event(workspace_id=WS, actor="x", action="kb.create",
                                 project_id=None)
    assert _rows(action="kb.create")[0].outcome == "success"
    with pytest.raises(ValueError):
        audit_svc.record_audit_event(workspace_id=WS, actor="x", action="kb.create",
                                     outcome="maybe")


def test_the_migration_adds_outcome_to_an_old_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE audit_events (id VARCHAR(32) PRIMARY KEY, "
                          "actor VARCHAR(64), action VARCHAR(160))"))
        conn.execute(text("INSERT INTO audit_events VALUES ('a', 'x', 'y')"))
    db_mod._migrate_audit_outcome(engine)
    db_mod._migrate_audit_outcome(engine)  # idempotent
    assert "outcome" in {c["name"] for c in inspect(engine).get_columns("audit_events")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT outcome FROM audit_events")).scalar() == "success"
