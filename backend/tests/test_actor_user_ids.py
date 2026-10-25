"""The immutable `users.id` beside every recorded actor (design §3.6, §5.8, §5.10, §7.4).

Session epoch (forced logout) in the cookie stamp, the request caller's id stamped by
audit / share-link / SoD writers, and the SoD checks comparing ids when both sides
carry one — so a re-registered username is a different person, and a missing id
falls back to the name exactly as before.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import actor as actor_mod
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.dlc import criteria as criteria_svc
from app.dlc import releases as release_svc
from app.models.dlc import CriteriaSet, ReleaseRecord, Waiver
from app.models.ledger import AuditEvent, Promotion, User
from app.routers import auth
from app.services import promotion as promotion_service
from app.services import share_links
from app.services import users as users_service
from app.services.audit import record_audit_event
from tests import test_dlc_api as dlc_api

WS = DEFAULT_WORKSPACE_ID
ENG = dlc_api.ENG
_agent, _create_set = dlc_api._agent, dlc_api._create_set
# the DLC console harness: an auth-enabled app and a registered, logged-in engineer
app = dlc_api.app
engineer = dlc_api.engineer


@pytest.fixture(autouse=True)
def _no_current_actor():
    actor_mod.set_current_actor(None, None)
    yield
    actor_mod.set_current_actor(None, None)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        for model in (Waiver, ReleaseRecord, CriteriaSet):
            session.query(model).delete()
        session.commit()
        yield session
    finally:
        session.rollback()
        session.close()


# ── the helpers ─────────────────────────────────────────────────────────────────


def test_same_actor_prefers_ids_and_falls_back_to_names():
    # both ids known: the ids decide, whatever the names say
    assert actor_mod.same_actor("bob", "u1", "bob", "u1")
    assert not actor_mod.same_actor("bob", "u1", "bob", "u2")  # re-registered "bob"
    # either id missing: the name, as before the ids existed
    assert actor_mod.same_actor("bob", None, "bob", "u2")
    assert actor_mod.same_actor("bob", "u1", "bob", None)
    assert not actor_mod.same_actor("bob", None, "carol", None)
    # an empty name matches nothing
    assert not actor_mod.same_actor("", None, "", None)
    assert not actor_mod.same_actor(None, None, None, None)


def test_user_id_for_answers_only_for_the_request_caller():
    assert actor_mod.user_id_for("alice") is None  # no request
    actor_mod.set_current_actor("alice", "u-alice")
    assert actor_mod.user_id_for("alice") == "u-alice"
    assert actor_mod.user_id_for("bob") is None  # someone else's name passed through
    assert actor_mod.user_id_for("") is None
    assert actor_mod.user_id_for("bob", "u-bob") == "u-bob"  # explicit wins
    actor_mod.set_current_actor("admin", None)  # the built-in admin: no row, no id
    assert actor_mod.user_id_for("admin") is None


# ── session epoch ───────────────────────────────────────────────────────────────


def test_epoch_zero_stamps_exactly_as_before():
    user = SimpleNamespace(password_hash="pbkdf2$1$abc$def", session_epoch=0)
    assert auth.account_stamp(user) == users_service.credential_stamp(user.password_hash)
    user.session_epoch = None  # a row read before the column had a value
    assert auth.account_stamp(user) == users_service.credential_stamp(user.password_hash)
    user.session_epoch = 1
    bumped = auth.account_stamp(user)
    assert bumped != users_service.credential_stamp(user.password_hash)
    user.session_epoch = 2
    assert auth.account_stamp(user) != bumped


def test_a_session_epoch_bump_ends_the_session(engineer):
    assert engineer.get("/api/auth/status").json()["authenticated"] is True
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, ENG["username"])
        assert user.session_epoch == 0
        user.session_epoch = 1
        db.commit()
    finally:
        db.close()
    assert engineer.get("/api/auth/status").json()["authenticated"] is False
    assert engineer.get("/api/criteria-sets").status_code == 401
    # a fresh login is stamped with the new epoch and works again
    assert engineer.post("/api/auth/login", json={
        "username": ENG["username"], "password": ENG["password"]}).status_code == 200
    assert engineer.get("/api/criteria-sets").status_code == 200


# ── writers stamp the caller's id ───────────────────────────────────────────────


def _user_id(username: str) -> str:
    db = SessionLocal()
    try:
        return users_service.find_by_username(db, username).id
    finally:
        db.close()


def test_a_request_stamps_the_callers_id_on_criteria_and_audit(engineer):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    uid = _user_id(ENG["username"])
    db = SessionLocal()
    try:
        row = db.query(CriteriaSet).filter(CriteriaSet.lineage_id == lineage).one()
        assert (row.created_by, row.created_by_user_id) == (ENG["username"], uid)
        assert row.updated_by_user_id == uid
    finally:
        db.close()
    res = engineer.post("/api/auth/password", json={
        "current_password": ENG["password"], "new_password": "Eng-pw-456!"})
    assert res.status_code == 200, res.text
    db = SessionLocal()
    try:
        event = db.query(AuditEvent).filter(
            AuditEvent.action == "auth.password_change").order_by(
            AuditEvent.created_at.desc()).first()
        assert (event.actor, event.actor_user_id) == (ENG["username"], uid)
    finally:
        db.close()


def test_the_builtin_admin_and_system_actors_record_no_id():
    record_audit_event(workspace_id=WS, actor="system", action="test.no_id")
    actor_mod.set_current_actor("someone", "u-someone")
    record_audit_event(workspace_id=WS, actor="system", action="test.no_id")
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.action == "test.no_id").all()
        assert [r.actor_user_id for r in rows] == [None, None]
    finally:
        db.close()


def test_a_share_link_records_its_creators_id(engineer):
    agent_id = _agent(name="api-share")
    actor_mod.set_current_actor("engineer", "u-eng")
    db = SessionLocal()
    try:
        link, _ = share_links.create_link(
            db, workspace_id=WS, agent=None, target_id=agent_id, label="x",
            created_by="engineer", expires_in_days=1)
        assert link.created_by_user_id == "u-eng"
        other, _ = share_links.create_link(
            db, workspace_id=WS, agent=None, target_id=agent_id, label="y",
            created_by="engineer", expires_in_days=1, created_by_user_id="u-explicit")
        assert other.created_by_user_id == "u-explicit"
        db.delete(link)
        db.delete(other)
        db.commit()
    finally:
        db.close()


# ── SoD by id ───────────────────────────────────────────────────────────────────


def _published_set(db, *, actor: str, actor_user_id: str | None) -> CriteriaSet:
    row = criteria_svc.create_set(db, WS, kind="template", name="t", actor=actor,
                                  actor_user_id=actor_user_id)
    row.status = "published"
    db.flush()
    return row


def test_criteria_sign_compares_ids_when_both_sides_carry_one(db):
    row = _published_set(db, actor="alice", actor_user_id="u-old-alice")
    with pytest.raises(AppError) as exc:
        criteria_svc.sign(db, row, actor="alice", note="", is_admin=False,
                          actor_user_id="u-old-alice")
    assert exc.value.code == "criteria.self_sign"
    with pytest.raises(AppError):  # no id on the signer: the name decides
        criteria_svc.sign(db, row, actor="alice", note="", is_admin=False)
    # a different account that re-registered the name is a different person
    criteria_svc.sign(db, row, actor="alice", note="", is_admin=False,
                      actor_user_id="u-new-alice")
    assert (row.signed_by, row.signed_by_user_id) == ("alice", "u-new-alice")


def test_criteria_sign_without_ids_behaves_as_before(db):
    row = _published_set(db, actor="alice", actor_user_id=None)
    with pytest.raises(AppError):
        criteria_svc.sign(db, row, actor="alice", note="", is_admin=False,
                          actor_user_id="u-alice")
    criteria_svc.sign(db, row, actor="alice", note="", is_admin=True)  # admin exempt


def test_release_sign_compares_ids(db, monkeypatch):
    monkeypatch.setattr(release_svc, "point_endpoint", lambda *a, **k: {})
    # no mounts on either version: no storage sign-off needed (storage-mounts §8.1)
    monkeypatch.setattr(release_svc, "storage_evidence",
                        lambda *a, **k: {"changes": [], "requires_ack": False})
    agent = SimpleNamespace(id="a1")
    record = ReleaseRecord(workspace_id=WS, agent_id="a1", candidate_version="2",
                           decision="pending", requested_by="dev", requested_by_user_id="u1",
                           gate_report={"verdict": "PASS"})
    db.add(record)
    db.flush()
    with pytest.raises(AppError) as exc:
        release_svc.sign(db, record, agent, object(), actor="dev", actor_user_id="u1")
    assert exc.value.code == "release.self_sign"
    release_svc.sign(db, record, agent, object(), actor="dev", actor_user_id="u2")
    assert (record.decided_by, record.decided_by_user_id) == ("dev", "u2")


def test_waiver_approval_compares_ids(db):
    waiver = Waiver(workspace_id=WS, agent_id="a1", criterion_key="G1", reason="r",
                    risk_owner="o", expires_on=datetime.now(UTC) + timedelta(days=3),
                    requested_by="dev", requested_by_user_id="u1")
    db.add(waiver)
    db.flush()
    with pytest.raises(AppError) as exc:
        release_svc.decide_waiver(db, waiver, actor="dev", approve=True, actor_user_id="u1")
    assert exc.value.code == "waiver.self_approve"
    actor_mod.set_current_actor("dev", "u2")  # the request caller's id is picked up
    release_svc.decide_waiver(db, waiver, actor="dev", approve=True)
    assert (waiver.approved_by, waiver.approved_by_user_id) == ("dev", "u2")


def test_promotion_review_compares_ids(monkeypatch):
    monkeypatch.setattr(promotion_service, "evaluate_gates", lambda *a, **k: {})
    db = SimpleNamespace(flush=lambda: None)
    pending = Promotion(status="pending", requested_by="dev", requested_by_user_id="u1")
    with pytest.raises(AppError) as exc:
        promotion_service.review_promotion(
            db, pending, approve=True, reviewer="dev", note=None, target=None, bundle=None,
            reviewer_user_id="u1")
    assert exc.value.code == "promotion.self_approval"
    with pytest.raises(AppError):  # no reviewer id: the name decides
        promotion_service.review_promotion(
            db, pending, approve=True, reviewer="dev", note=None, target=None, bundle=None)
    promotion_service.review_promotion(
        db, pending, approve=True, reviewer="dev", note=None, target=None, bundle=None,
        reviewer_user_id="u2")
    assert (pending.reviewed_by, pending.reviewed_by_user_id) == ("dev", "u2")
    assert pending.status == "approved"


def test_a_new_users_epoch_is_zero():
    db = SessionLocal()
    try:
        user, _ = users_service.create_user(
            db, username="epoch-user", email="epoch@acme-corp.com", password="sufficient-pass",
            created_by="test")
        assert db.get(User, user.id).session_epoch == 0
    finally:
        db.close()
