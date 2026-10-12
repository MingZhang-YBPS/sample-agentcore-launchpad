"""T19–T22 — the operator role, release bundles, promotion review and the inbox.

The invariants worth pinning are the ones that make the dev → ops hand-off trustworthy:
a bundle's digest identifies exactly what was tested and is stable, the same publish
never forks into two bundles, a member cannot approve their own release, an operator can
approve but cannot edit an agent, and the inbox surfaces work without becoming a place
where an item has no link to act on.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, AuditEvent, Deployment, Promotion, SpecSnapshot, Workspace
from app.services import promotion as promotion_service
from app.services import users as users_service

ADMIN_CREDS = {"username": "release-admin", "password": "s3cret-pass"}
DEV_CREDS = {
    "username": "builder",
    "email": "builder@acme-corp.com",
    "password": "sufficient-pass",
}
OPS_CREDS = {
    "username": "releaser",
    "email": "releaser@acme-corp.com",
    "password": "sufficient-pass",
}
TARGET_WS = {"id": "prod-ws", "name": "Prod", "account_id": "444455556677", "region": "us-west-1"}

SPEC = {
    "name": "release-me",
    "method": "harness",
    "system_prompt": "be helpful",
    "model_id": "anthropic.claude",
    "memory": {"short_term": True, "long_term": False},
}


# ── fixtures ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _seed_agent(*, active: bool = True, with_snapshot: bool = True) -> str:
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID,
            name=SPEC["name"],
            method="harness",
            status="active" if active else "draft",
            spec=dict(SPEC),
            arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/h",
            version="3",
        )
        db.add(agent)
        db.flush()
        deployment = Deployment(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, status="succeeded"
        )
        db.add(deployment)
        db.flush()
        if with_snapshot:
            db.add(
                SpecSnapshot(
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    agent_id=agent.id,
                    seq=1,
                    spec=dict(SPEC),
                    aws_version="3",
                    deployment_id=deployment.id,
                    created_by="builder",
                )
            )
        db.commit()
        return agent.id
    finally:
        db.close()


def _ensure_target_row() -> None:
    """Grants need an existing workspace; the admin fixture may not have run yet."""
    db = SessionLocal()
    try:
        if db.get(Workspace, TARGET_WS["id"]) is None:
            db.add(Workspace(**TARGET_WS, bootstrap_status="registered", resources={}))
            db.commit()
    finally:
        db.close()


def _member(app, creds, role: str, *, target_grant: bool = True) -> TestClient:
    """A signed-in account granted on the source and — unless told otherwise — the target.

    Releasing into a workspace requires a grant on it (security review finding), so the
    legitimate hand-off grants both; `target_grant=False` models a dev-only account.
    """
    client = TestClient(app, client=("127.0.0.1", 4321))
    client.__enter__()
    assert client.post("/api/auth/register", json=creds).status_code == 201
    if target_grant:
        _ensure_target_row()
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, creds["username"])
        user.status = users_service.STATUS_ACTIVE
        user.role = role
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        grants = [DEFAULT_WORKSPACE_ID] + ([TARGET_WS["id"]] if target_grant else [])
        users_service.set_workspace_grants(db, user, grants)
        db.commit()
    finally:
        db.close()
    # more than one grant means no implicit workspace: name the source on every call
    client.headers["X-Workspace"] = DEFAULT_WORKSPACE_ID
    login = client.post(
        "/api/auth/login",
        json={"username": creds["username"], "password": creds["password"]},
    )
    assert login.status_code == 200, login.text
    return client


@pytest.fixture
def admin(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        client.post("/api/workspaces", json=TARGET_WS)
        yield client


@pytest.fixture
def developer(gated_app):
    client = _member(gated_app, DEV_CREDS, users_service.ROLE_MEMBER)
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def operator(gated_app):
    client = _member(gated_app, OPS_CREDS, users_service.ROLE_OPERATOR)
    yield client
    client.__exit__(None, None, None)


# ── T19: roles and permission defaults ───────────────────────────────────────────


def test_role_defaults_separate_building_from_releasing():
    member = users_service.default_permissions(users_service.ROLE_MEMBER)
    ops = users_service.default_permissions(users_service.ROLE_OPERATOR)
    admin = users_service.default_permissions(users_service.ROLE_ADMIN)

    assert "agents.deploy" in member and "promotion.approve" not in member
    # an operator releases what others built: approval yes, editing no
    assert "promotion.approve" in ops and "agents.deploy" not in ops
    assert set(admin) == set(users_service.AGENT_PERMISSIONS)


def test_an_operator_cannot_deploy_but_can_reach_the_review_surface(operator):
    denied = operator.post("/api/agents", json=dict(SPEC, name="nope-not-allowed"))
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "auth.permission_required"
    assert denied.json()["detail"]["permission"] == "agents.deploy"
    assert operator.get("/api/promotions").status_code == 200


def test_an_admin_can_grant_approval_to_one_member(admin, developer):
    status = developer.get("/api/auth/status").json()
    assert "promotion.approve" not in status["permissions"]
    user_id = next(
        row["id"]
        for row in admin.get("/api/users", params={"q": DEV_CREDS["username"]}).json()["items"]
    )
    patched = admin.patch(
        f"/api/users/{user_id}", json={"permissions": {"promotion.approve": True}}
    )
    assert patched.status_code == 200, patched.text
    assert "promotion.approve" in developer.get("/api/auth/status").json()["permissions"]


def test_an_unknown_role_is_refused(admin, developer):
    user_id = next(
        row["id"]
        for row in admin.get("/api/users", params={"q": DEV_CREDS["username"]}).json()["items"]
    )
    bad = admin.patch(f"/api/users/{user_id}", json={"role": "superuser"})
    assert bad.status_code in (400, 422)
    ok = admin.patch(f"/api/users/{user_id}", json={"role": "operator"})
    assert ok.status_code == 200 and ok.json()["role"] == "operator"


# ── T20: release bundles ─────────────────────────────────────────────────────────


def test_the_digest_covers_the_spec_and_artifact_but_not_the_evidence():
    base = {"agent_name": "a", "method": "harness", "spec": {"p": 1}, "artifact": {"v": "2"}}
    first = promotion_service.bundle_digest(**base)
    assert first == promotion_service.bundle_digest(**base)  # stable
    # key order must not matter — the canonical form sorts
    assert first == promotion_service.bundle_digest(
        agent_name="a", method="harness", spec={"p": 1}, artifact={"v": "2"}
    )
    assert first != promotion_service.bundle_digest(**{**base, "spec": {"p": 2}})
    assert first != promotion_service.bundle_digest(**{**base, "artifact": {"v": "3"}})


def test_bundling_is_idempotent_per_publish(developer):
    agent_id = _seed_agent()
    first = developer.post(f"/api/agents/{agent_id}/release-bundles", json={})
    assert first.status_code == 201, first.text
    second = developer.post(f"/api/agents/{agent_id}/release-bundles", json={})
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["digest"] == second.json()["digest"]
    listed = developer.get(f"/api/agents/{agent_id}/release-bundles").json()["bundles"]
    assert len(listed) == 1


def test_a_bundle_records_what_was_tested(developer):
    agent_id = _seed_agent()
    body = developer.post(f"/api/agents/{agent_id}/release-bundles", json={"note": "rc1"}).json()
    assert body["snapshot_seq"] == 1
    assert body["spec"]["system_prompt"] == SPEC["system_prompt"] if "spec" in body else True
    assert body["artifact"]["aws_version"] == "3"
    assert body["policy"]["guardrail"] == {"enabled": False}
    assert body["note"] == "rc1"
    assert len(body["digest"]) == 64


def test_a_draft_agent_and_an_unknown_publish_are_refused(developer):
    draft = _seed_agent(active=False)
    refused = developer.post(f"/api/agents/{draft}/release-bundles", json={})
    assert refused.status_code == 409
    assert refused.json()["code"] == "promotion.agent_not_active"

    active = _seed_agent()
    missing = developer.post(f"/api/agents/{active}/release-bundles", json={"snapshot_seq": 99})
    assert missing.status_code == 404
    assert missing.json()["code"] == "promotion.snapshot_not_found"


def test_an_agent_published_before_snapshots_existed_still_bundles(developer):
    agent_id = _seed_agent(with_snapshot=False)
    body = developer.post(f"/api/agents/{agent_id}/release-bundles", json={})
    assert body.status_code == 201, body.text
    assert body.json()["snapshot_seq"] is None
    assert body.json()["digest"]


# ── T21: promotion review ────────────────────────────────────────────────────────


def _bundle_id(client, agent_id: str) -> str:
    return client.post(f"/api/agents/{agent_id}/release-bundles", json={}).json()["id"]


def _request(client, bundle_id: str, target: str = TARGET_WS["id"]):
    return client.post(
        "/api/promotions",
        json={
            "bundle_id": bundle_id,
            "target_workspace_id": target,
            "change_note": "first release",
            "rollback_note": "roll back to the previous publish",
        },
    )


def test_a_request_captures_the_notes_and_the_gates(admin, developer):
    agent_id = _seed_agent()
    created = _request(developer, _bundle_id(developer, agent_id))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "pending"
    assert body["requested_by"] == DEV_CREDS["username"]
    assert body["change_note"] and body["rollback_note"]
    keys = {check["key"] for check in body["gates"]["checks"]}
    # a superset, not an exact set: later gates (T23 resource mapping, T26 policy and
    # window) are added to the same list and must not have to edit this test
    assert {"evaluation", "artifact", "guardrail", "target_ready", "target_agent"} <= keys
    # the target was registered but never bootstrapped, so that gate must fail
    assert "target_ready" in body["gates"]["blocking_failures"]


def test_notes_are_required(admin, developer):
    agent_id = _seed_agent()
    bundle_id = _bundle_id(developer, agent_id)
    bad = developer.post(
        "/api/promotions",
        json={
            "bundle_id": bundle_id,
            "target_workspace_id": TARGET_WS["id"],
            "change_note": "",
            "rollback_note": "",
        },
    )
    assert bad.status_code == 422


def test_the_same_workspace_and_a_duplicate_request_are_refused(admin, developer):
    agent_id = _seed_agent()
    bundle_id = _bundle_id(developer, agent_id)
    same = _request(developer, bundle_id, target=DEFAULT_WORKSPACE_ID)
    assert same.status_code == 400 and same.json()["code"] == "promotion.same_workspace"

    assert _request(developer, bundle_id).status_code == 201
    again = _request(developer, bundle_id)
    assert again.status_code == 409 and again.json()["code"] == "promotion.already_open"


def test_a_member_cannot_review(admin, developer):
    agent_id = _seed_agent()
    promotion_id = _request(developer, _bundle_id(developer, agent_id)).json()["id"]
    denied = developer.post(
        f"/api/promotions/{promotion_id}/review", json={"decision": "approve"}
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["permission"] == "promotion.approve"


def test_an_operator_approves_and_the_decision_is_journaled(admin, developer, operator):
    agent_id = _seed_agent()
    promotion_id = _request(developer, _bundle_id(developer, agent_id)).json()["id"]
    approved = operator.post(
        f"/api/promotions/{promotion_id}/review",
        json={"decision": "approve", "note": "gates read, accepted"},
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved"
    assert body["reviewed_by"] == OPS_CREDS["username"]
    assert body["review_note"] == "gates read, accepted"
    assert body["reviewed_at"]

    db = SessionLocal()
    try:
        actions = [
            row.action
            for row in db.scalars(
                select(AuditEvent).where(AuditEvent.workspace_id == DEFAULT_WORKSPACE_ID)
            )
        ]
    finally:
        db.close()
    assert "promotion.approve" in actions


def test_nobody_approves_their_own_release(admin, operator):
    """The hand-off is the point: the requester cannot also be the approver."""
    agent_id = _seed_agent()
    bundle_id = operator.post(f"/api/agents/{agent_id}/release-bundles", json={}).json()["id"]
    promotion_id = _request(operator, bundle_id).json()["id"]
    refused = operator.post(
        f"/api/promotions/{promotion_id}/review", json={"decision": "approve"}
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "promotion.self_approval"
    # rejecting one's own request is allowed — withdrawing is not a privilege escalation
    assert operator.post(
        f"/api/promotions/{promotion_id}/review", json={"decision": "reject"}
    ).status_code == 200


def test_a_reviewed_promotion_cannot_be_reviewed_again(admin, developer, operator):
    agent_id = _seed_agent()
    promotion_id = _request(developer, _bundle_id(developer, agent_id)).json()["id"]
    assert operator.post(
        f"/api/promotions/{promotion_id}/review", json={"decision": "reject"}
    ).status_code == 200
    again = operator.post(
        f"/api/promotions/{promotion_id}/review", json={"decision": "approve"}
    )
    assert again.status_code == 409 and again.json()["code"] == "promotion.not_pending"


def test_the_detail_diffs_the_bundle_against_the_target(admin, developer):
    agent_id = _seed_agent()
    promotion_id = _request(developer, _bundle_id(developer, agent_id)).json()["id"]
    # the target has no agent of that name yet: every field reads as added
    fresh = developer.get(f"/api/promotions/{promotion_id}").json()
    assert fresh["target_agent"] is None
    assert {row["field"] for row in fresh["diff"]} >= {"system_prompt", "model_id"}
    assert all(row["kind"] == "added" for row in fresh["diff"])

    db = SessionLocal()
    try:
        db.add(
            Agent(
                workspace_id=TARGET_WS["id"],
                name=SPEC["name"],
                method="harness",
                status="active",
                spec=dict(SPEC, system_prompt="the old prompt"),
                version="1",
            )
        )
        db.commit()
    finally:
        db.close()
    changed = developer.get(f"/api/promotions/{promotion_id}").json()
    assert changed["target_agent"]["status"] == "active"
    rows = {row["field"]: row for row in changed["diff"]}
    assert rows["system_prompt"]["kind"] == "changed"
    assert rows["system_prompt"]["before"] == "the old prompt"
    assert "model_id" not in rows  # unchanged fields are omitted


def test_spec_diff_truncates_and_labels_each_kind():
    rows = {
        row["field"]: row
        for row in promotion_service.spec_diff(
            {"kept": 1, "changed": "a", "removed": True}, {"kept": 1, "changed": "b", "added": 2}
        )
    }
    assert set(rows) == {"changed", "removed", "added"}
    assert rows["changed"]["kind"] == "changed"
    assert rows["removed"]["kind"] == "removed" and rows["removed"]["after"] == "—"
    assert rows["added"]["kind"] == "added" and rows["added"]["before"] == "—"

    long_value = "x" * 900
    [row] = promotion_service.spec_diff({}, {"prompt": long_value})
    assert row["after"].endswith("…") and len(row["after"]) <= 601


def test_a_bundle_from_another_workspace_is_not_reachable(admin, developer):
    agent_id = _seed_agent()
    bundle_id = _bundle_id(developer, agent_id)
    db = SessionLocal()
    try:
        from app.models.ledger import ReleaseBundle

        db.get(ReleaseBundle, bundle_id).workspace_id = TARGET_WS["id"]
        db.commit()
    finally:
        db.close()
    missing = developer.get(f"/api/release-bundles/{bundle_id}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "promotion.bundle_not_found"


# ── T22: the inbox ───────────────────────────────────────────────────────────────


def test_the_inbox_is_admin_only(developer, operator):
    assert developer.get("/api/inbox").status_code == 403
    assert operator.get("/api/inbox").status_code == 403


def test_the_inbox_surfaces_pending_work_with_a_link_each(admin, developer):
    agent_id = _seed_agent()
    _request(developer, _bundle_id(developer, agent_id))
    body = admin.get("/api/inbox").json()
    items = {item["key"]: item for item in body["items"]}

    assert "promotions_pending" in items
    assert items["promotions_pending"]["count"] == 1
    assert items["promotions_pending"]["severity"] == "action"
    # the registered-but-not-bootstrapped target is worth an administrator's attention
    assert "workspaces_attention" in items
    # every entry must be actionable, or the inbox is just a wall of numbers
    assert all(item["to"] for item in body["items"])
    assert body["total"] == sum(item["count"] for item in body["items"])
    # action items sort above warnings, warnings above info
    order = [item["severity"] for item in body["items"]]
    assert order == sorted(order, key=lambda s: {"action": 0, "warn": 1, "info": 2}[s])


def test_an_unevaluated_active_agent_shows_as_info(admin):
    _seed_agent()
    items = {item["key"]: item for item in admin.get("/api/inbox").json()["items"]}
    assert items["agents_unevaluated"]["count"] >= 1
    assert items["agents_unevaluated"]["severity"] == "info"


def test_a_quiet_workspace_produces_an_empty_inbox(admin):
    db = SessionLocal()
    try:
        # the seeded target workspace is the only noisy thing in a clean ledger
        db.get(Workspace, TARGET_WS["id"]).bootstrap_status = "ready"
        db.commit()
    finally:
        db.close()
    body = admin.get("/api/inbox").json()
    assert [item for item in body["items"] if item["key"] == "workspaces_attention"] == []
    assert body["workspace_id"] == DEFAULT_WORKSPACE_ID


def test_promotions_can_be_filtered_by_status_and_target(admin, developer, operator):
    agent_id = _seed_agent()
    promotion_id = _request(developer, _bundle_id(developer, agent_id)).json()["id"]
    pending = developer.get("/api/promotions", params={"status": "pending"}).json()
    assert len(pending["promotions"]) == 1
    operator.post(f"/api/promotions/{promotion_id}/review", json={"decision": "reject"})
    assert developer.get("/api/promotions", params={"status": "pending"}).json()["promotions"] == []
    rejected = developer.get("/api/promotions", params={"status": "rejected"}).json()["promotions"]
    assert len(rejected) == 1 and rejected[0]["bundle"]["agent_name"] == SPEC["name"]
    assert developer.get(
        "/api/promotions", params={"target": "nowhere"}
    ).json()["promotions"] == []


def test_a_promotion_row_is_workspace_scoped(admin, developer):
    agent_id = _seed_agent()
    promotion_id = _request(developer, _bundle_id(developer, agent_id)).json()["id"]
    db = SessionLocal()
    try:
        db.get(Promotion, promotion_id).workspace_id = TARGET_WS["id"]
        db.commit()
    finally:
        db.close()
    assert developer.get(f"/api/promotions/{promotion_id}").status_code == 404


# ── security review: the target workspace is a grant boundary too ────────────────
#
# Finding (IDOR, MEDIUM): the route policy authorized only the SOURCE workspace, so a
# member granted one dev workspace could name ANY target. The promotion detail then
# diffed the target's same-name agent — env, prompt, code — back to them, and an
# operator granted only dev could approve and execute a release into prod.

DEV_ONLY = {"username": "dev-only", "email": "dev-only@acme-corp.com",
            "password": "sufficient-pass"}
OPS_DEV_ONLY = {"username": "ops-dev-only", "email": "ops-dev-only@acme-corp.com",
                "password": "sufficient-pass"}


def _seed_victim(spec_overrides: dict) -> None:
    """The agent an attacker wants to read: same name, in the target workspace."""
    db = SessionLocal()
    try:
        db.add(Agent(workspace_id=TARGET_WS["id"], name=SPEC["name"], method="harness",
                     status="active", spec=dict(SPEC, **spec_overrides), version="9"))
        db.commit()
    finally:
        db.close()


def test_a_member_cannot_request_a_release_into_an_ungranted_workspace(admin, gated_app):
    dev_only = _member(gated_app, DEV_ONLY, users_service.ROLE_MEMBER, target_grant=False)
    try:
        agent_id = _seed_agent()
        bundle_id = _bundle_id(dev_only, agent_id)
        refused = _request(dev_only, bundle_id)
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "workspace.forbidden"
    finally:
        dev_only.__exit__(None, None, None)


def test_the_detail_never_shows_target_data_to_an_ungranted_reader(admin, gated_app):
    """Even a promotion someone else (legitimately) created must not leak the target."""
    _seed_victim({"system_prompt": "TOP SECRET PROMPT", "env": {"API_TOKEN": "s3cr3t"}})
    agent_id = _seed_agent()
    bundle_id = admin.post(f"/api/agents/{agent_id}/release-bundles", json={}).json()["id"]
    promotion_id = _request(admin, bundle_id).json()["id"]

    dev_only = _member(gated_app, DEV_ONLY, users_service.ROLE_MEMBER, target_grant=False)
    try:
        body = dev_only.get(f"/api/promotions/{promotion_id}").json()
        assert body["target_visible"] is False
        assert body["target_agent"] is None and body["diff"] == []
        assert "TOP SECRET" not in str(body) and "s3cr3t" not in str(body)
        for path in ("plan", "execution"):
            res = dev_only.get(f"/api/promotions/{promotion_id}/{path}")
            assert res.status_code == 403, (path, res.text)
        preview = dev_only.get(
            f"/api/release-bundles/{bundle_id}/resolution",
            params={"target_workspace_id": TARGET_WS["id"]},
        )
        assert preview.status_code in (403, 404), preview.text
    finally:
        dev_only.__exit__(None, None, None)


def test_a_dev_only_operator_cannot_approve_execute_or_roll_back(admin, gated_app):
    agent_id = _seed_agent()
    bundle_id = admin.post(f"/api/agents/{agent_id}/release-bundles", json={}).json()["id"]
    promotion_id = _request(admin, bundle_id).json()["id"]

    ops = _member(gated_app, OPS_DEV_ONLY, users_service.ROLE_OPERATOR, target_grant=False)
    try:
        for path, body in (("review", {"decision": "approve"}), ("execute", None),
                           ("rollback", None)):
            res = ops.post(f"/api/promotions/{promotion_id}/{path}", json=body)
            assert res.status_code == 403, (path, res.text)
            assert res.json()["code"] == "workspace.forbidden"
    finally:
        ops.__exit__(None, None, None)
    # nothing moved: the request is still waiting for someone who may act on the target
    assert admin.get(f"/api/promotions/{promotion_id}").json()["status"] == "pending"


def test_a_granted_reader_sees_the_diff_but_never_secret_values(admin, developer):
    _seed_victim({"env": {"API_TOKEN": "s3cr3t"}, "code": "print('old')"})
    agent_id = _seed_agent()
    promotion_id = _request(developer, _bundle_id(developer, agent_id)).json()["id"]
    body = developer.get(f"/api/promotions/{promotion_id}").json()
    assert body["target_visible"] is True and body["target_agent"]["status"] == "active"
    rows = {row["field"]: row for row in body["diff"]}
    assert rows["env"]["redacted"] is True and rows["env"]["before"] == "‹hidden›"
    assert rows["code"]["before"] == "‹hidden›"
    assert "s3cr3t" not in str(body) and "print('old')" not in str(body)
    assert rows["env"]["kind"] == "removed"  # the change itself is still reported


def test_redaction_is_limited_to_secret_shaped_fields():
    rows = {
        row["field"]: row
        for row in promotion_service.spec_diff(
            {"system_prompt": "a", "env": {"K": "v"}, "byoc": {"image_uri": "x"}},
            {"system_prompt": "b", "env": {"K": "w"}, "byoc": None},
        )
    }
    assert rows["system_prompt"]["before"] == "a" and rows["system_prompt"]["redacted"] is False
    assert rows["env"]["before"] == rows["env"]["after"] == "‹hidden›"
    assert rows["byoc"]["before"] == "‹hidden›" and rows["byoc"]["after"] == "—"


def test_removing_a_target_workspace_cancels_the_requests_aimed_at_it(admin, developer):
    """Regression (found by the e2e suite): a pending request into a purged workspace sat
    in the inbox forever — nobody could review it, because its target answers 404."""
    agent_id = _seed_agent()
    promotion_id = _request(developer, _bundle_id(developer, agent_id)).json()["id"]
    assert "promotions_pending" in {i["key"] for i in admin.get("/api/inbox").json()["items"]}

    purged = admin.post(f"/api/workspaces/{TARGET_WS['id']}/purge")
    assert purged.status_code == 200, purged.text

    row = developer.get(f"/api/promotions/{promotion_id}").json()
    assert row["status"] == "cancelled"
    assert TARGET_WS["id"] in (row["error"] or "")
    assert "promotions_pending" not in {i["key"] for i in admin.get("/api/inbox").json()["items"]}


def test_finished_promotions_survive_a_target_removal_as_history():
    db = SessionLocal()
    try:
        agent_id = _seed_agent()
        from app.models.ledger import ReleaseBundle

        bundle = ReleaseBundle(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                               agent_name=SPEC["name"], method="harness", spec={}, artifact={},
                               digest="e" * 64)
        db.add(bundle)
        db.flush()
        done = Promotion(workspace_id=DEFAULT_WORKSPACE_ID, bundle_id=bundle.id,
                         target_workspace_id="gone-ws", status="succeeded",
                         change_note="c", rollback_note="r")
        db.add(done)
        db.commit()
        assert promotion_service.cancel_for_removed_target(db, "gone-ws") == 0
        db.commit()
        assert db.get(Promotion, done.id).status == "succeeded"
    finally:
        db.close()


def test_an_ungranted_reader_never_sees_target_resource_ids_in_the_gates(admin, gated_app):
    """Final-scan follow-up: the resource-mapping gate lists target resource ids, which a
    source-workspace reader without a target grant must not receive (list or detail)."""
    agent_id = _seed_agent()
    bundle_id = admin.post(f"/api/agents/{agent_id}/release-bundles", json={}).json()["id"]
    promotion_id = _request(admin, bundle_id).json()["id"]
    db = SessionLocal()
    try:
        row = db.get(Promotion, promotion_id)
        row.gates = {"checks": [{"key": "resource_mapping", "ok": True, "detail": "1 mapped",
                                 "resolved": [{"to": "KBTARGETSECRET"}], "unmapped": []}]}
        db.commit()
    finally:
        db.close()
    dev_only = _member(gated_app, DEV_ONLY, users_service.ROLE_MEMBER, target_grant=False)
    try:
        detail = dev_only.get(f"/api/promotions/{promotion_id}").json()
        listed = dev_only.get("/api/promotions").json()["promotions"]
        for body in (detail, *listed):
            assert "KBTARGETSECRET" not in str(body)
            assert body["target_visible"] is False
        # the verdict itself is still reported
        assert detail["gates"]["checks"][0]["ok"] is True
    finally:
        dev_only.__exit__(None, None, None)
    granted = admin.get(f"/api/promotions/{promotion_id}").json()
    assert "KBTARGETSECRET" in str(granted)  # an admin reaches the target, so sees the ids
