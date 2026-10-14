"""Projects phase 1 (design §5.8) for share / review / annotate / channel links: a link
carries its target's project, the public `/share/*` routes run as that project (so a
visitor's rows are stamped with it), a link whose agent or task is in another project —
or whose project is archived — is the same opaque 404, and archiving can revoke every
link of a project at once."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.share as share_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import NotFoundError
from app.core.project_scope import current_project, project_context
from app.dlc import calibration as cal
from app.main import create_app
from app.models.dlc import AnnotationTask
from app.models.ledger import Agent, ChatFeedback, ChatMessage, ChatSession, ShareLink
from app.models.projects import Project
from app.services import annotation_links, channels, review_links, share_links
from tests.test_project_scope import _project

WS = DEFAULT_WORKSPACE_ID


@pytest.fixture(autouse=True)
def fresh_limiters():
    share_links.limiter.reset()
    review_links.review_limiter.reset()
    annotation_links.annotate_limiter.reset()
    yield
    share_links.limiter.reset()
    review_links.review_limiter.reset()
    annotation_links.annotate_limiter.reset()


@pytest.fixture
def two_projects():
    """The default workspace bound to alpha and beta too: no row can be stamped from the
    workspace alone, so whatever a public route writes must come from the link."""
    _project("alpha")
    _project("beta")


def _agent(name: str, pid: str) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=WS, name=name, method="zip_runtime", status="active",
                        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
                        spec={"name": name, "display_name": "Desk"})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _link(agent_id: str, kind: str = share_links.KIND_CHAT, **fields) -> tuple[str, str]:
    """(link id, raw token), minted unscoped: the project must come from the agent."""
    db = SessionLocal()
    try:
        with project_context(None):
            agent = db.get(Agent, agent_id)
            link, raw = share_links.create_link(
                db, workspace_id=WS, agent=agent, label="l", created_by="river",
                expires_in_days=None, kind=kind, **fields,
            )
            return link.id, raw
    finally:
        db.close()


def _row(model, ident):
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(model, ident)
            db.expunge(row)
            return row
    finally:
        db.close()


def _set(model, ident, **fields) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(model, ident)
            for key, value in fields.items():
                setattr(row, key, value)
            db.commit()
    finally:
        db.close()


@pytest.fixture
def stream(monkeypatch):
    seen: list[str | None] = []

    def fake(agent, prompt, session_id=None, **kwargs):
        seen.append(current_project())  # what the generator thread sees
        yield {"event": "meta", "data": {"session_id": session_id, "agent": agent.name,
                                         "mode": "stream"}}
        yield {"event": "delta", "data": {"text": "hello"}}
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(share_router, "chat_stream", fake)
    return seen


def _session_id(res) -> str:
    for frame in res.text.split("\n\n"):
        if "event: meta" in frame:
            data = frame.split("data: ", 1)[1]
            return json.loads(data)["session_id"]
    raise AssertionError(res.text)


# ── the link carries its project ──────────────────────────────────────────────


def test_a_link_is_stamped_with_its_agents_project(two_projects):
    link_id, _raw = _link(_agent("stamp", "beta"))
    assert _row(ShareLink, link_id).project_id == "beta"


def test_the_public_chat_runs_as_the_links_project(two_projects, stream):
    agent_id = _agent("pub", "alpha")
    link_id, raw = _link(agent_id)
    client = TestClient(create_app())
    assert client.get(f"/share/{raw}").status_code == 200
    res = client.post(f"/share/{raw}/chat", json={"prompt": "hi"})
    assert res.status_code == 200, res.text
    assert stream == ["alpha"]
    session_id = _session_id(res)
    db = SessionLocal()
    try:
        with project_context(None):
            assert set(db.scalars(select(ChatSession.project_id))) == {"alpha"}
            assert set(db.scalars(select(ChatMessage.project_id))) == {"alpha"}
            answer = db.scalars(select(ChatMessage.id).where(ChatMessage.role == "agent")).first()
    finally:
        db.close()
    # the conversation continues, and its feedback is filed in the same project
    again = client.post(f"/share/{raw}/chat", json={"prompt": "more", "session_id": session_id})
    assert again.status_code == 200, again.text
    rated = client.post(f"/share/{raw}/feedback",
                        json={"session_id": session_id, "message_id": answer, "verdict": "up"})
    assert rated.status_code == 200, rated.text
    db = SessionLocal()
    try:
        with project_context(None):
            assert set(db.scalars(select(ChatFeedback.project_id))) == {"alpha"}
    finally:
        db.close()
    assert _row(ShareLink, link_id).use_count == 2


def test_an_unknown_token_leaves_no_project_current(two_projects):
    client = TestClient(create_app())
    res = client.get("/share/shr_nope")
    assert res.status_code == 404 and res.json()["code"] == "share.not_found"
    assert share_links.link_project("shr_nope") is None
    assert share_links.link_project("not-a-token") is None


# ── the opaque 404 ────────────────────────────────────────────────────────────


def test_an_agent_moved_to_another_project_closes_the_link(two_projects):
    agent_id = _agent("moved", "alpha")
    _link_id, raw = _link(agent_id)
    client = TestClient(create_app())
    assert client.get(f"/share/{raw}").status_code == 200
    _set(Agent, agent_id, project_id="beta")
    res = client.get(f"/share/{raw}")
    assert res.status_code == 404 and res.json()["code"] == "share.not_found"


def test_an_archived_project_closes_its_links(two_projects):
    agent_id = _agent("arch", "alpha")
    _link_id, raw = _link(agent_id)
    review_id, review_raw = _link(agent_id, kind=review_links.KIND_REVIEW)
    client = TestClient(create_app())
    assert client.get(f"/share/{raw}").status_code == 200
    _set(Project, "alpha", status="archived")
    try:
        for path in (f"/share/{raw}", f"/share/review/{review_raw}"):
            res = client.get(path)
            assert res.status_code == 404 and res.json()["code"] == "share.not_found", path
    finally:
        _set(Project, "alpha", status="active")
    assert client.get(f"/share/review/{review_raw}").status_code == 200
    assert _row(ShareLink, review_id).project_id == "alpha"


def test_a_link_without_a_project_is_unreachable(two_projects):
    link_id, raw = _link(_agent("orphan", "alpha"))
    _set(ShareLink, link_id, project_id=None)
    db = SessionLocal()
    try:
        with pytest.raises(NotFoundError) as err:
            share_links.resolve_link(db, raw)
        assert err.value.code == "share.not_found"
    finally:
        db.close()


def test_resolve_ignores_the_current_project(two_projects):
    """The token lookup is unscoped: a link resolves whatever happens to be current."""
    _link_id, raw = _link(_agent("unscoped", "alpha"))
    db = SessionLocal()
    try:
        with project_context("beta"):
            assert share_links.resolve_link(db, raw).project_id == "alpha"
    finally:
        db.close()


# ── review / annotate / channels ──────────────────────────────────────────────


def test_the_review_queue_runs_as_the_links_project(two_projects, monkeypatch):
    agent_id = _agent("rev", "alpha")
    _link_id, raw = _link(agent_id, kind=review_links.KIND_REVIEW)
    seen: list[str | None] = []

    def fake_queue(db, link, agent):
        seen.append(current_project())
        return []

    monkeypatch.setattr(review_links, "queue", fake_queue)
    res = TestClient(create_app()).get(f"/share/review/{raw}")
    assert res.status_code == 200, res.text
    assert seen == ["alpha"]


def _task(pid: str) -> str:
    agent_id = _agent(f"ann-{pid}", pid)
    db = SessionLocal()
    try:
        with project_context(pid):
            task = cal.create_task(
                db, workspace_id=WS, agent_id=agent_id, criteria_set_id=None,
                criterion_key="J1", purpose="judge_calibration",
                items=[{"ref": "i0", "input": "q", "answer": "a", "judge_label": "pass"}],
                annotators=["alice", "bob"], adjudicator="alice", actor="root",
                run_id=None, dataset_id=None,
            )
            db.commit()
            return task.id
    finally:
        db.close()


def _annotation_link(task_id: str) -> tuple[str, str]:
    db = SessionLocal()
    try:
        with project_context(None):
            task = db.get(AnnotationTask, task_id)
            link, raw = annotation_links.create(
                db, task=task, workspace=None, label="Wang", created_by="root",
                expires_in_days=None,
            )
            db.commit()
            return link.id, raw
    finally:
        db.close()


def test_an_annotation_link_follows_its_tasks_project(two_projects):
    task_id = _task("beta")
    link_id, raw = _annotation_link(task_id)
    assert _row(ShareLink, link_id).project_id == "beta"
    client = TestClient(create_app())
    res = client.post(f"/share/annotate/{raw}/label", json={"item_ref": "i0", "label": "pass"})
    assert res.status_code == 200, res.text
    assert res.json()["labelled"] == 1
    # the task moved under the link: same opaque 404 as a revoked link
    _set(AnnotationTask, task_id, project_id="alpha")
    res = client.get(f"/share/annotate/{raw}")
    assert res.status_code == 404 and res.json()["code"] == "share.not_found"


def test_a_channel_webhook_runs_as_the_links_project(two_projects, monkeypatch):
    _link_id, raw = _link(_agent("chan", "alpha"), kind="slack",
                          channel_config={}, channel_secrets={})
    seen: dict[str, str | None] = {}

    class Adapter:
        @staticmethod
        def verify(link, headers, body):
            seen["verify"] = current_project()

        @staticmethod
        def parse(link, payload):
            from types import SimpleNamespace

            return SimpleNamespace(kind="handshake", response={"challenge": "c"})

    monkeypatch.setattr(channels, "adapter_for", lambda platform: Adapter)
    res = TestClient(create_app()).post(f"/share/channels/slack/{raw}", json={"type": "x"})
    assert res.status_code == 200, res.text
    assert res.json() == {"challenge": "c"}
    assert seen == {"verify": "alpha"}


# ── archive: revoke every link of a project ───────────────────────────────────


def test_revoke_project_links_revokes_only_that_projects_live_links(two_projects):
    alpha = _agent("rv-a", "alpha")
    beta = _agent("rv-b", "beta")
    chat_id, raw = _link(alpha)
    review_id, _ = _link(alpha, kind=review_links.KIND_REVIEW)
    gone_id, _ = _link(alpha)
    other_id, other_raw = _link(beta)
    db = SessionLocal()
    try:
        with project_context(None):
            share_links.revoke_project_links(db, "nobody")  # nothing to do
            db.get(ShareLink, gone_id).revoked_at = datetime(2020, 1, 1, tzinfo=UTC)
            db.commit()
        with project_context("beta"):  # whatever is current, the helper sees every row
            assert share_links.revoke_project_links(db, "alpha") == 2
        db.commit()
    finally:
        db.close()
    for ident in (chat_id, review_id):
        row = _row(ShareLink, ident)
        assert row.revoked_at is not None and not row.enabled
    assert share_links._aware(_row(ShareLink, gone_id).revoked_at).year == 2020
    assert _row(ShareLink, other_id).revoked_at is None
    client = TestClient(create_app())
    assert client.get(f"/share/{raw}").status_code == 404
    assert client.get(f"/share/{other_raw}").status_code == 200


# ── a single-project workspace ────────────────────────────────────────────────


def test_a_single_project_workspace_shares_as_before(stream):
    """No extra projects: the link and the visitor's rows land in the default project."""
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=WS, name="solo", method="zip_runtime", status="active",
                      arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x", spec={"name": "solo"})
        db.add(agent)
        db.commit()
        agent_id, pid = agent.id, agent.project_id
    finally:
        db.close()
    link_id, raw = _link(agent_id)
    assert _row(ShareLink, link_id).project_id == pid
    res = TestClient(create_app()).post(f"/share/{raw}/chat", json={"prompt": "hi"})
    assert res.status_code == 200, res.text
    assert stream == [pid]
