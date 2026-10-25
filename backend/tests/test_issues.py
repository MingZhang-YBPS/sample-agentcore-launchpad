"""T36 -- the issue box: lifecycle, fix recording, and that it composes the existing
dataset / rule endpoints instead of reimplementing them."""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.db import SessionLocal
from app.evaluation.models import EvalDataset
from app.main import create_app
from app.models.ledger import ChatFeedback
from tests.selfservice_util import seed_turn
from tests.test_share_links import make_agent

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def client():
    return TestClient(create_app())


def down(client, agent_id, question="q?", answer="wrong"):
    sid, mid = seed_turn(agent_id, question, answer)
    res = client.post(f"/api/chat/{agent_id}/feedback", json={
        "session_id": sid, "message_id": mid, "verdict": "down", "comment": "bad"})
    assert res.status_code == 200, res.text
    return sid, mid


def only_issue(client, **params):
    items = client.get("/api/issues", params=params).json()["items"]
    assert len(items) == 1
    return items[0]


def test_a_thumbs_down_opens_one_issue_with_the_session_facts(client):
    agent_id = make_agent()
    sid, mid = down(client, agent_id, "How many days?", "12")
    issue = only_issue(client)
    assert (issue["kind"], issue["status"], issue["session_id"], issue["message_id"]) == (
        "thumbs_down", "open", sid, mid)
    assert (issue["question"], issue["answer"], issue["comment"]) == ("How many days?", "12", "bad")
    assert issue["history"][0]["status"] == "open" and issue["history"][0]["by"]
    # a second down-vote / a re-vote does not duplicate it
    client.post(f"/api/chat/{agent_id}/feedback", json={
        "session_id": sid, "message_id": mid, "verdict": "down", "comment": "still bad"})
    issue = only_issue(client)
    assert issue["comment"] == "still bad"
    detail = client.get(f"/api/issues/{issue['id']}").json()
    assert [m["role"] for m in detail["transcript"]] == ["user", "agent"]
    assert detail["transcript"][1]["flagged"] is True


def test_lifecycle_transitions_record_who_and_when_and_measure_close_time(client):
    agent_id = make_agent()
    down(client, agent_id)
    issue = only_issue(client)
    iid = issue["id"]

    # cannot reopen what is open, or fix twice
    assert client.post(f"/api/issues/{iid}/reopen", json={}).status_code == 409
    assert client.post(f"/api/issues/{iid}/resolve", json={"status": "open"}).status_code == 422

    done = client.post(f"/api/issues/{iid}/resolve",
                       json={"status": "fixed", "note": "added a curated answer"}).json()
    assert done["status"] == "fixed" and done["resolved_by"] and done["resolved_at"]
    assert done["hours_to_close"] is not None and done["hours_to_close"] >= 0
    assert client.post(f"/api/issues/{iid}/resolve",
                       json={"status": "wont_fix"}).status_code == 409  # reopen first

    reopened = client.post(f"/api/issues/{iid}/reopen", json={"note": "still wrong"}).json()
    assert reopened["status"] == "open" and reopened["resolved_at"] is None
    closed = client.post(f"/api/issues/{iid}/resolve", json={"status": "wont_fix"}).json()
    assert closed["status"] == "wont_fix"
    trail = [h["status"] for h in closed["history"]]
    assert trail == ["open", "fixed", "open", "wont_fix"]
    assert all(h["by"] and h["at"] for h in closed["history"])

    summary = client.get("/api/issues").json()["summary"]
    assert summary["wont_fix"] == 1 and summary["open"] == 0
    assert summary["median_hours_to_close"] is not None
    assert client.get("/api/issues", params={"status": "open"}).json()["items"] == []


def test_an_unanswered_question_can_be_opened_and_is_idempotent(client):
    agent_id = make_agent()
    sid, mid = seed_turn(agent_id, "When is payday?", "I couldn't find that.")
    body = {"agent_id": agent_id, "session_id": sid, "message_id": mid, "kind": "unanswered"}
    first = client.post("/api/issues", json=body)
    assert first.status_code == 201 and first.json()["kind"] == "unanswered"
    assert client.post("/api/issues", json=body).json()["id"] == first.json()["id"]
    assert client.post("/api/issues", json={**body, "message_id": 999999}).status_code == 404
    other = make_agent(name="other")
    assert client.post("/api/issues", json={**body, "agent_id": other}).status_code == 404


def test_sync_backfills_thumbs_down_votes_cast_before_the_box(client):
    agent_id = make_agent()
    sid, mid = seed_turn(agent_id, "q", "a")
    db = SessionLocal()
    try:
        db.add(ChatFeedback(workspace_id="default", agent_id=agent_id, session_id=sid,
                            message_id=mid, verdict="down", actor="old", source="console"))
        db.commit()
    finally:
        db.close()
    assert client.post("/api/issues/sync").json() == {"opened": 1}
    assert client.post("/api/issues/sync").json() == {"opened": 0}


def test_a_curated_answer_created_for_an_issue_is_linked_but_the_issue_stays_open(client):
    agent_id = make_agent()
    down(client, agent_id, "How many days?", "12")
    iid = only_issue(client)["id"]
    rule = client.post(f"/api/agents/{agent_id}/rules", json={
        "pattern": "How many days?", "answer": "25 days.", "issue_id": iid}).json()
    issue = client.get(f"/api/issues/{iid}").json()
    assert issue["status"] == "open"  # marking it resolved is the owner's explicit step
    assert [(f["action"], f["ref"]) for f in issue["fixes"]] == [("rule", rule["id"])]
    assert rule["source_issue_id"] == iid


def test_dataset_and_kb_fixes_are_recorded_not_performed(client):
    agent_id = make_agent()
    down(client, agent_id)
    iid = only_issue(client)["id"]
    fix = f"/api/issues/{iid}/fixes"
    # a dataset the existing endpoint built; an unknown one is refused
    assert client.post(fix, json={"action": "dataset", "ref": "nope"}).status_code == 404
    db = SessionLocal()
    try:
        ds = EvalDataset(workspace_id="default", name="badcases", kind="predefined", items=[])
        db.add(ds)
        db.commit()
        ds_id = ds.id
        n_before = db.query(EvalDataset).count()
    finally:
        db.close()
    ok = client.post(fix, json={"action": "dataset", "ref": ds_id})
    assert ok.status_code == 200 and ok.json()["fixes"][0]["ref"] == ds_id
    assert client.post(fix, json={"action": "kb", "ref": "KB123"}).status_code == 200
    assert client.post(fix, json={"action": "rule", "ref": "nope"}).status_code == 404
    assert client.post(fix, json={"action": "other"}).status_code == 422
    db = SessionLocal()
    try:
        assert db.query(EvalDataset).count() == n_before  # nothing was built here
    finally:
        db.close()


def test_the_issue_box_reuses_the_existing_endpoints_and_builds_nothing_itself():
    """The dataset and knowledge-base logic live in one place each. These modules may
    read a dataset row to verify a reference, never construct one or call the builders."""
    forbidden_calls = {"EvalDataset", "build_dataset", "from_sessions", "upload_document",
                       "ingest_documents", "items_from_transcript", "merge_items"}
    for rel in ("services/issues.py", "routers/issues.py", "services/intents.py"):
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
        called = {
            (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", ""))
            for n in ast.walk(tree) if isinstance(n, ast.Call)
        }
        assert not (called & forbidden_calls), (rel, called & forbidden_calls)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                    for a in n.names}
        assert not imported & {"pipelines", "knowledge"}, rel


def test_issues_are_workspace_scoped(client):
    agent_id = make_agent()
    down(client, agent_id)
    db = SessionLocal()
    try:
        from app.models.selfservice import Issue

        db.query(Issue).update({Issue.workspace_id: "elsewhere"})
        db.commit()
    finally:
        db.close()
    assert client.get("/api/issues").json()["items"] == []
    assert client.get("/api/issues/whatever").status_code == 404
