"""P1D leftovers (node LEFT): studio conversations scoped by workspace + creator as
well as project, online-eval config names keyed by (workspace, project), and the
online-eval agent check answering 404 for a missing or another project's agent."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.evaluation import online
from app.evaluation.models import OnlineEvalConfig
from app.evaluation.online_routers import _agent_in
from app.models.conversation import CreateConversationRequest
from app.models.ledger import Agent
from app.models.projects import Project, ProjectWorkspace
from app.services import access
from app.services.conversation_service import ConversationService, SessionCaller
from tests.conftest import TEST_PROJECT

WS = SimpleNamespace(id=DEFAULT_WORKSPACE_ID)
ADA = SessionCaller(DEFAULT_WORKSPACE_ID, "ada")
BOB = SessionCaller(DEFAULT_WORKSPACE_ID, "bob")
ADMIN = SessionCaller(DEFAULT_WORKSPACE_ID, "root", is_admin=True)
OTHER_WS_ADMIN = SessionCaller("ws-other", "root", is_admin=True)


def _project(pid: str) -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            if db.get(Project, pid) is None:
                db.add(Project(id=pid, name=pid.upper(), status="active"))
            if db.get(ProjectWorkspace, (pid, DEFAULT_WORKSPACE_ID)) is None:
                db.add(ProjectWorkspace(project_id=pid, workspace_id=DEFAULT_WORKSPACE_ID,
                                        is_workspace_default=False))
            access.bump_epoch(db)
            db.commit()
    finally:
        db.close()


def _add(pid: str | None, row):
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _agent(name: str, pid: str, status: str = "active") -> str:
    return _add(pid, Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime",
                           status=status, spec={"name": name}))


# ── /api/conversations: workspace + creator + project ────────────────────────


@pytest.fixture
def stub_interpreter(monkeypatch):
    monkeypatch.setattr(get_settings(), "studio_exec_python", sys.executable)


def _create(svc: ConversationService, caller: SessionCaller, pid: str = TEST_PROJECT) -> str:
    with project_context(pid):
        session = asyncio.run(svc.create_session(
            CreateConversationRequest(generated_code="print('x')"), caller))
    return session.session_id


def _listed(svc: ConversationService, caller: SessionCaller, pid: str = TEST_PROJECT) -> set[str]:
    with project_context(pid):
        return {s.session_id for s in asyncio.run(svc.get_sessions(caller)).sessions}


def _refused(svc: ConversationService, sid: str, caller: SessionCaller, pid: str = TEST_PROJECT):
    with project_context(pid):
        for call in (
            svc.get_session_history(sid, caller),
            svc.get_session_messages(sid, caller),
            svc.update_session_code(sid, "print(2)", caller),
            svc.send_message(sid, "hi", caller),
            svc.delete_session(sid, caller),
        ):
            with pytest.raises(ValueError, match=f"Session {sid} not found"):
                asyncio.run(call)


def test_a_member_sees_only_their_own_sessions(stub_interpreter):
    svc = ConversationService()
    ada = _create(svc, ADA)
    bob = _create(svc, BOB)
    assert _listed(svc, ADA) == {ada}
    assert _listed(svc, BOB) == {bob}
    _refused(svc, ada, BOB)
    with project_context(TEST_PROJECT):
        history = asyncio.run(svc.get_session_history(ada, ADA))
    assert history.session.session_id == ada
    assert ada in svc.sessions  # the refused delete removed nothing


def test_an_admin_sees_every_session_of_the_project_only(stub_interpreter):
    svc = ConversationService()
    ada = _create(svc, ADA)
    bob = _create(svc, BOB)
    alpha = _create(svc, ADA, pid="alpha")
    assert _listed(svc, ADMIN) == {ada, bob}
    assert _listed(svc, ADMIN, pid="alpha") == {alpha}
    _refused(svc, alpha, ADMIN)  # another project's: missing even to an admin
    _refused(svc, ada, ADA, pid="alpha")
    with project_context(TEST_PROJECT):
        assert asyncio.run(svc.delete_session(bob, ADMIN))["message"]
    assert bob not in svc.sessions


def test_another_workspaces_session_reads_as_missing(stub_interpreter):
    svc = ConversationService()
    ada = _create(svc, ADA)
    assert _listed(svc, OTHER_WS_ADMIN) == set()
    _refused(svc, ada, OTHER_WS_ADMIN)


def test_cleanup_still_reaches_every_session(stub_interpreter):
    svc = ConversationService()
    _create(svc, ADA)
    _create(svc, BOB, pid="alpha")
    for s in svc.sessions.values():
        s.updated_at = s.updated_at.replace(year=2000)
    assert asyncio.run(svc.cleanup_expired_sessions()) == 2
    assert svc.sessions == {} and svc.owners == {}


# ── online evaluation: config names per (workspace, project) ─────────────────


def _online_row(pid: str, agent_id: str, config_id: str) -> None:
    _add(pid, OnlineEvalConfig(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                               agent_name="a", config_id=config_id, config_arn=f"arn:{config_id}",
                               name=f"oe_{config_id}", service_name="s", log_group="g"))


@pytest.fixture
def summaries(monkeypatch):
    online.reset_names_cache()
    calls: list[int] = []
    rows = [
        {"onlineEvaluationConfigId": "cfg-default", "onlineEvaluationConfigName": "oe_d"},
        {"onlineEvaluationConfigId": "cfg-alpha", "onlineEvaluationConfigName": "oe_a"},
        {"onlineEvaluationConfigId": "ext-1", "onlineEvaluationConfigName": "console_made"},
    ]

    def _list(_control):
        calls.append(1)
        return rows

    monkeypatch.setattr(online.ac, "list_online_eval_configs", _list)
    yield calls
    online.reset_names_cache()


def test_config_names_never_cross_projects(summaries):
    _project("alpha")
    _online_row(TEST_PROJECT, _agent("d", TEST_PROJECT), "cfg-default")
    _online_row("alpha", _agent("a", "alpha"), "cfg-alpha")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            alpha = online.config_names(WS, object(), db)
        with project_context(TEST_PROJECT):
            default = online.config_names(WS, object(), db)
            assert online.config_names(WS, object(), db) is default  # cached per project
    finally:
        db.close()
    assert alpha == {"cfg-alpha": "oe_a", "ext-1": "console_made"}
    assert default == {"cfg-default": "oe_d", "ext-1": "console_made"}
    assert len(summaries) == 2  # one List per (workspace, project)


def test_config_names_unfenced_in_a_single_project_workspace(summaries):
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            names = online.config_names(WS, object(), db)
    finally:
        db.close()
    assert names == {"cfg-default": "oe_d", "cfg-alpha": "oe_a", "ext-1": "console_made"}


# ── online evaluation: missing / another project's agent → 404 ───────────────


def test_online_agent_check_answers_404_for_missing_or_foreign_agents():
    _project("alpha")
    alpha_agent = _agent("a", "alpha")
    stopped = _agent("s", TEST_PROJECT, status="failed")
    live = _agent("d", TEST_PROJECT)
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            for agent_id in ("nope", alpha_agent):
                with pytest.raises(NotFoundError) as info:
                    _agent_in(db, WS, agent_id)
                assert info.value.code == "agent.not_found"
            with pytest.raises(AppError) as info:
                _agent_in(db, WS, stopped)
            assert info.value.code == "agent.not_active" and info.value.status_code == 400
            assert _agent_in(db, WS, live).id == live
    finally:
        db.close()
