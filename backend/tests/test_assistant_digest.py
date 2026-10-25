# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""assistant-context C2: the confirmed-items digest of the turns that fell out of the
replay window — when it is generated, what the model is given, where the preamble
shows it, the conditional write, and the owner-only regenerate route."""

import pytest

from app.assistant import digest, service
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import NotFoundError
from app.core.project_scope import current_project, project_context
from app.models.assistant import AssistantConversation, AssistantMessage
from app.models.projects import Project, ProjectWorkspace
from app.services import access

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    BASE,
    _latest,
    _open,
    _propose,
    _turn,
    gated,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)
from .test_assistant_context import _seed_turns

REPLY = ("## Confirmed items (turns 1–2)\n"
         "- Constraint: data stays in the region (turn 2, member stated)\n"
         "## Open questions\n- peak concurrency (turn 1, assistant asked, member did not answer)")


class FakeBedrock:
    def __init__(self, reply: str = REPLY):
        self.reply = reply
        self.fail: Exception | None = None
        self.during = None  # runs inside the call (a concurrent writer)
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append({**kwargs, "project": current_project()})
        if self.during is not None:
            self.during()
        if self.fail is not None:
            raise self.fail
        return {"output": {"message": {"content": [{"text": self.reply}]}}}

    def input_text(self, index: int = -1) -> str:
        return self.calls[index]["messages"][0]["content"][0]["text"]


@pytest.fixture
def bedrock(monkeypatch):
    fake = FakeBedrock()
    monkeypatch.setattr(digest, "bedrock_client", lambda workspace: fake)
    # the background refresh runs inline so a test sees its result deterministically
    monkeypatch.setattr(digest, "_start", lambda target, name: target())
    digest._RUNNING.clear()
    yield fake
    digest._RUNNING.clear()


def _row(cid: str) -> AssistantConversation:
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        db.expunge(row)
        return row
    finally:
        db.close()


def _store(cid: str, text: str | None, through: int | None) -> None:
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        row.context_digest, row.context_digest_through_turn = text, through
        db.commit()
    finally:
        db.close()


def _refresh(cid: str, fake: FakeBedrock, *, rebuild: bool = False) -> bool:
    db = SessionLocal()
    try:
        return digest.refresh(db, cid, fake, rebuild=rebuild)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# generation after a turn
# ---------------------------------------------------------------------------


def test_a_turn_that_pushes_turns_out_of_the_window_summarizes_them(client, ready, harness,
                                                                      bedrock):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 1)
    harness.reply("ok")
    assert _turn(client, cid, "next")[-1][0] == "done"
    # the NEXT turn omits turns 1 and 2: one cheap call covers exactly those
    assert len(bedrock.calls) == 1
    call = bedrock.calls[0]
    assert call["modelId"] == get_settings().agent_default_model_id  # D-2
    assert call["system"][0]["text"] == digest.SYSTEM_PROMPT
    sent = bedrock.input_text()
    assert "### Turn 1 — member" in sent and "### Turn 2 — assistant" in sent
    assert "### Turn 3" not in sent
    assert "## Previous digest\n\n(none)" in sent
    view = _latest(client, cid)["context_digest"]
    assert view["text"] == REPLY and view["through_turn"] == 2 and view["updated_at"]
    assert view["generating"] is False

    # the next request shows it in the preamble, before the member message (D-1)
    harness.reply("ok")
    _turn(client, cid, "after")
    first = harness.calls[-1]["messages"][0]["content"][0]["text"]
    preamble, _, _member = first.partition("## Member message")
    assert "## Summary of earlier turns (turns 1–2)" in preamble
    assert REPLY in preamble and "may be incomplete" in preamble
    assert "Turns up to 2 are summarized" in preamble


def test_no_call_while_every_turn_is_still_replayed(client, ready, harness, bedrock):
    cid = _open(client)
    _seed_turns(cid, 3)
    harness.reply("ok")
    _turn(client, cid, "next")
    assert bedrock.calls == []
    view = _latest(client, cid)["context_digest"]
    assert view == {"text": None, "through_turn": None, "updated_at": None, "generating": False}
    first = harness.calls[-1]["messages"][0]["content"][0]["text"]
    assert "Summary of earlier turns" not in first


def test_the_digest_sits_before_the_current_proposal(client, ready, harness, bedrock):
    cid = _open(client)
    _propose(client, harness, cid)
    _store(cid, REPLY, 1)
    db = SessionLocal()
    try:  # ``_preamble`` reads the stored proposal through the row's own session
        preamble = service._preamble(db.get(AssistantConversation, cid))
    finally:
        db.close()
    assert preamble.index("## Summary of earlier turns") < preamble.index(
        "## Current stored proposal")


def test_a_refresh_rolls_the_previous_digest_and_only_the_new_turns(client, ready, bedrock):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 2)
    assert _refresh(cid, bedrock) is True
    assert _row(cid).context_digest_through_turn == 2
    _seed_turns(cid, 1, start=service.MAX_REPLAY_TURNS + 3)
    bedrock.reply = "## Confirmed items (turns 1–3)\n- Requirement: x (turn 3, member stated)"
    assert _refresh(cid, bedrock) is True
    sent = bedrock.input_text()
    assert "## Previous digest (turns 1–2)" in sent and REPLY in sent
    assert "### Turn 3 — member" in sent and "### Turn 1 " not in sent
    row = _row(cid)
    assert row.context_digest_through_turn == 3 and row.context_digest.endswith("member stated)")
    # nothing new: no call at all
    assert _refresh(cid, bedrock) is False and len(bedrock.calls) == 2


def test_a_failed_call_keeps_the_previous_digest(client, ready, harness, bedrock):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 2)
    _store(cid, "- old (turn 1, member stated)", 1)
    bedrock.fail = RuntimeError("throttled")
    harness.reply("ok")
    assert _turn(client, cid, "next")[-1][0] == "done"  # the conversation is unaffected
    assert len(bedrock.calls) == 1
    row = _row(cid)
    assert (row.context_digest, row.context_digest_through_turn) == (
        "- old (turn 1, member stated)", 1)
    assert not digest.generating(cid)


def test_two_concurrent_refreshes_store_one_digest(client, ready, bedrock):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 2)

    def other_writer():  # finishes first, from the same starting point
        db = SessionLocal()
        try:
            assert digest._store(db, cid, "winner", 2, expected_through=None, expected_text=None)
        finally:
            db.close()

    bedrock.during = other_writer
    assert _refresh(cid, bedrock) is False
    assert _row(cid).context_digest == "winner"


def test_a_refresh_already_running_is_not_started_twice(client, ready, bedrock):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 2)
    digest._RUNNING.add(cid)
    db = SessionLocal()
    try:
        assert digest.after_turn(db, cid, None) is False
    finally:
        db.close()
    assert bedrock.calls == []
    assert _latest(client, cid)["context_digest"]["generating"] is True


def test_the_output_is_unfenced_and_bounded():
    assert digest.clean_output("```markdown\n- a (turn 1, member stated)\n```") == (
        "- a (turn 1, member stated)")
    long = "\n".join(f"- item {i} (turn 1, member stated)" for i in range(2000))
    out = digest.clean_output(long)
    assert len(out) <= digest.DIGEST_MAX_CHARS + 4 and out.endswith("\n- …")


def test_long_turns_are_cut_to_their_head_and_tail():
    text = "H" * digest.TURN_SIDE_HEAD_CHARS + "M" * 50_000 + "T" * digest.TURN_SIDE_TAIL_CHARS
    sent = digest.build_input(None, None, [(4, text, "ok")])
    assert "M" * 100 not in sent and "H" * 100 in sent and "T" * 100 in sent
    assert "## Turns to add (turns 4–4)" in sent


def test_the_digest_never_enters_a_proposal_or_its_hash(client, ready, harness, bedrock):
    cid = _open(client)
    proposal = _propose(client, harness, cid)
    _store(cid, REPLY, 1)
    after = _latest(client, cid)["proposals"][-1]
    assert after["content_hash"] == proposal["content_hash"]
    assert "Confirmed items" not in str(after["content"])


def test_the_protocol_asks_for_confirmed_constraints_in_the_proposal():
    rule = service.PROTOCOL_PREAMBLE.split("5. Older turns drop out", 1)[1]
    assert "`requirements_baseline`" in rule and "never lost" in rule


# ---------------------------------------------------------------------------
# regenerate (D-4: the owner only)
# ---------------------------------------------------------------------------


def _regenerate(session, cid: str):
    return session.post(f"{BASE}/conversations/{cid}/digest/regenerate")


def test_the_owner_rebuilds_the_digest_from_every_omitted_turn(client, ready, bedrock):
    cid = _open(client)
    _seed_turns(cid, service.MAX_REPLAY_TURNS + 2)
    _store(cid, "- wrong (turn 1, assistant suggested)", 2)
    bedrock.reply = "- rebuilt (turn 2, member stated)"
    res = _regenerate(client, cid)
    assert res.status_code == 200, res.text
    assert res.json()["context_digest"]["text"] == "- rebuilt (turn 2, member stated)"
    sent = bedrock.input_text()
    assert "wrong" not in sent and "(none)" in sent and "### Turn 1 — member" in sent
    assert _row(cid).context_digest_through_turn == 2


def test_regenerate_refuses_without_omitted_turns_while_busy_and_on_failure(client, ready,
                                                                          bedrock):
    cid = _open(client)
    _seed_turns(cid, 2)
    res = _regenerate(client, cid)
    assert res.status_code == 409 and res.json()["code"] == "assistant.digest_nothing_to_summarize"
    _seed_turns(cid, service.MAX_REPLAY_TURNS, start=3)
    _store(cid, "- kept", 1)
    digest._RUNNING.add(cid)
    res = _regenerate(client, cid)
    assert res.status_code == 409 and res.json()["code"] == "assistant.digest_busy"
    digest._RUNNING.clear()
    bedrock.fail = RuntimeError("model down")
    res = _regenerate(client, cid)
    assert res.status_code == 502 and res.json()["code"] == "assistant.digest_failed"
    assert _row(cid).context_digest == "- kept" and not digest.generating(cid)


def test_only_the_owner_regenerates_a_shared_conversation(gated, bedrock):
    admin, member, other, _ids, _preset = gated
    shared_cid = _open(admin)
    private_cid = _open(admin)
    assert admin.put(f"{BASE}/conversations/{shared_cid}/sharing",
                     json={"shared": True}).status_code == 200
    res = _regenerate(member, shared_cid)
    assert res.status_code == 403 and res.json()["code"] == "assistant.digest_owner_only"
    # a collaborator still reads it
    assert "context_digest" in _latest(member, shared_cid)
    assert _regenerate(other, private_cid).status_code == 404
    assert bedrock.calls == []


# ---------------------------------------------------------------------------
# the project boundary
# ---------------------------------------------------------------------------


def _alpha_conversation(turns: int) -> str:
    db = SessionLocal()
    try:
        with project_context(None):
            if db.get(Project, "alpha") is None:
                db.add(Project(id="alpha", name="ALPHA", status="active"))
                db.add(ProjectWorkspace(project_id="alpha", workspace_id=DEFAULT_WORKSPACE_ID,
                                        is_workspace_default=False))
                access.bump_epoch(db)
        with project_context("alpha"):
            conv = AssistantConversation(workspace_id=DEFAULT_WORKSPACE_ID, owner="river",
                                         owner_principal="user:ann", title="alpha")
            db.add(conv)
            db.flush()
            for turn in range(1, turns + 1):
                for role in ("user", "assistant"):
                    db.add(AssistantMessage(workspace_id=DEFAULT_WORKSPACE_ID,
                                            conversation_id=conv.id, turn=turn, role=role,
                                            text=f"{role[0]}{turn}"))
            conv.turns = turns
            db.commit()
            return conv.id
    finally:
        db.close()


def test_the_background_refresh_runs_as_the_rows_project(bedrock):
    cid = _alpha_conversation(service.MAX_REPLAY_TURNS + 2)
    db = SessionLocal()
    try:
        with project_context("alpha"):
            assert digest.after_turn(db, cid, None) is True
    finally:
        db.close()
    assert [c["project"] for c in bedrock.calls] == ["alpha"]
    db = SessionLocal()
    try:
        with project_context("alpha"):
            assert db.get(AssistantConversation, cid).context_digest_through_turn == 2
    finally:
        db.close()


def test_another_projects_conversation_is_not_found(bedrock):
    cid = _alpha_conversation(service.MAX_REPLAY_TURNS + 2)
    db = SessionLocal()
    try:
        with project_context("default"), pytest.raises(NotFoundError):
            service.accessible_conversation(db, DEFAULT_WORKSPACE_ID, "user:ann", cid)
    finally:
        db.close()
