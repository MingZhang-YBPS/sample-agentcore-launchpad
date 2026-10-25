"""Account-free annotation links: who may label, what they can see, where it is refused."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.dlc import calibration as cal
from app.main import create_app
from app.models.dlc import Annotation, AnnotationTask, CalibrationRecord
from app.models.ledger import Agent, ShareLink, Workspace
from app.services import annotation_links

WS = DEFAULT_WORKSPACE_ID
ADMIN = {"username": "root", "password": "Admin-pw-1!"}


def _tier(value: str) -> None:
    db = SessionLocal()
    try:
        db.get(Workspace, WS).tier = value
        db.commit()
    finally:
        db.close()


def _clean() -> None:
    db = SessionLocal()
    try:
        for model in (Annotation, CalibrationRecord, AnnotationTask):
            db.query(model).delete()
        db.query(ShareLink).filter(ShareLink.kind == "annotate").delete()
        db.query(Agent).filter(Agent.name.like("al-%")).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    _clean()
    _tier("dev")
    app = create_app()
    with TestClient(app, client=("127.0.0.1", 4321)) as c:
        assert c.post("/api/auth/login", json=ADMIN).status_code == 200
        yield c
    get_settings.cache_clear()
    _clean()
    _tier("dev")


def _task(items=6):
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=WS, name="al-a", method="harness", status="active",
                      spec={"name": "al-a"}, version="1", resource_id="h-al")
        db.add(agent)
        db.commit()
        task = cal.create_task(
            db, workspace_id=WS, agent_id=agent.id, criteria_set_id=None,
            criterion_key="J1", purpose="judge_calibration",
            items=[{"ref": f"i{n}", "input": f"q{n}", "answer": f"a{n}",
                    "judge_label": "pass" if n % 2 else "fail",
                    "judge_explanation": f"because {n}"} for n in range(items)],
            annotators=["alice", "bob"], adjudicator="alice", actor="root",
            run_id=None, dataset_id=None,
        )
        db.commit()
        return task.id
    finally:
        db.close()


def test_a_link_labels_blind_and_counts_as_its_own_annotator(client):
    task_id = _task()
    created = client.post(f"/api/annotation-tasks/{task_id}/links",
                          json={"label": "Legal — Wang Fang"})
    assert created.status_code == 201, created.text
    body = created.json()
    token = body["token"]
    assert body["annotator"].startswith("link_")
    # the raw token is returned once; only its hash is at rest, like an API key
    db = SessionLocal()
    try:
        row = db.get(ShareLink, body["id"])
        assert row.token_hash != token and len(row.token_hash) == 64
        assert row.prefix.startswith("shr_") and row.prefix.endswith("…")
    finally:
        db.close()

    # the link is now an annotator on the task, so its labels count toward κ
    listed = client.get(f"/api/annotation-tasks/{task_id}").json()
    assert body["annotator"] in listed["annotators"]

    queue = client.get(f"/share/annotate/{token}")
    assert queue.status_code == 200, queue.text
    view = queue.json()
    assert view["label"] == "Legal — Wang Fang"
    assert view["total"] == 6 and view["labelled"] == 0
    # blind: neither the judge's verdict nor anyone else's vote is served
    assert all("judge_label" not in item for item in view["items"])
    assert all("labels" not in item for item in view["items"])

    labelled = client.post(f"/share/annotate/{token}/label",
                           json={"item_ref": "i0", "label": "fail", "rationale": "wrong price"})
    assert labelled.status_code == 200, labelled.text
    assert labelled.json()["labelled"] == 1
    assert labelled.json()["items"][0]["my_label"] == "fail"

    # the console sees the label under the link's identity
    db = SessionLocal()
    try:
        rows = db.query(Annotation).filter(Annotation.task_id == task_id).all()
        assert [(r.annotator, r.label) for r in rows] == [(body["annotator"], "fail")]
    finally:
        db.close()


def test_the_public_surface_serves_nothing_but_the_queue(client):
    task_id = _task()
    token = client.post(f"/api/annotation-tasks/{task_id}/links",
                        json={"label": "SME"}).json()["token"]
    # agreement, decisions and the console view are not reachable with a link token
    assert client.get(f"/share/annotate/{token}/agreement").status_code == 404
    decide = client.post(f"/share/annotate/{token}/decide", json={"verdict": "aligned"})
    assert decide.status_code == 404
    bad = client.post(f"/share/annotate/{token}/label", json={"item_ref": "nope", "label": "pass"})
    assert bad.status_code == 404 and bad.json()["code"] == "annotation.item_not_found"
    wrong = client.post(f"/share/annotate/{token}/label", json={"item_ref": "i0", "label": "maybe"})
    assert wrong.status_code == 400 and wrong.json()["code"] == "annotation.bad_label"


def test_every_dead_link_state_is_the_same_404(client):
    task_id = _task()
    created = client.post(f"/api/annotation-tasks/{task_id}/links", json={"label": "SME"}).json()
    token = created["token"]
    assert client.get("/share/annotate/shr_nope").status_code == 404
    assert client.get("/share/annotate/not-even-a-token").status_code == 404
    revoked = client.delete(f"/api/annotation-tasks/{task_id}/links/{created['id']}")
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    gone = client.get(f"/share/annotate/{token}")
    assert gone.status_code == 404 and gone.json()["code"] == "share.not_found"


def test_labels_survive_the_links_revocation(client):
    task_id = _task()
    created = client.post(f"/api/annotation-tasks/{task_id}/links", json={"label": "SME"}).json()
    client.post(f"/share/annotate/{created['token']}/label",
                json={"item_ref": "i0", "label": "pass"})
    client.delete(f"/api/annotation-tasks/{task_id}/links/{created['id']}")
    db = SessionLocal()
    try:
        # a revoked credential does not retract a vote that was really cast
        assert db.query(Annotation).filter(Annotation.task_id == task_id).count() == 1
    finally:
        db.close()


def test_a_prod_workspace_refuses_links_and_closes_the_ones_it_has(client):
    task_id = _task()
    created = client.post(f"/api/annotation-tasks/{task_id}/links", json={"label": "SME"}).json()
    _tier("prod")
    refused = client.post(f"/api/annotation-tasks/{task_id}/links", json={"label": "Another"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "annotation.links_not_allowed"
    assert "member" in refused.json()["message"]
    listed = client.get(f"/api/annotation-tasks/{task_id}/links").json()
    assert listed["allowed"] is False and listed["reason"]
    # a link issued while the workspace was dev stops working once it is prod
    assert client.get(f"/share/annotate/{created['token']}").status_code == 404


def test_a_link_needs_a_name_and_a_live_task(client):
    task_id = _task()
    assert client.post(f"/api/annotation-tasks/{task_id}/links",
                       json={"label": "   "}).status_code == 400
    db = SessionLocal()
    try:
        cal.close_task(db, db.get(AnnotationTask, task_id))
        db.commit()
    finally:
        db.close()
    closed = client.post(f"/api/annotation-tasks/{task_id}/links", json={"label": "SME"})
    assert closed.status_code == 409 and closed.json()["code"] == "annotation.closed"


def test_links_allowed_names_the_remedy_rather_than_just_refusing():
    ok, reason = annotation_links.links_allowed(None)
    assert ok is True and reason == ""
    prod = Workspace(id="w", name="w", tier="prod")
    ok, reason = annotation_links.links_allowed(prod)
    assert ok is False
    assert "judge-calibration" in reason  # what to do instead, not just "no"


def test_link_labels_reach_the_agreement_numbers(client):
    """A link's votes are ordinary votes: they pair with a console annotator's."""
    task_id = _task(items=14)
    created = client.post(f"/api/annotation-tasks/{task_id}/links", json={"label": "SME"}).json()
    db = SessionLocal()
    try:
        task = db.get(AnnotationTask, task_id)
        # the two annotators are now: alice (console) and the link
        task.annotators = ["alice", created["annotator"]]
        db.commit()
    finally:
        db.close()
    for n in range(14):
        label = "pass" if n % 2 else "fail"
        res = client.post(f"/share/annotate/{created['token']}/label",
                          json={"item_ref": f"i{n}", "label": label})
        assert res.status_code == 200
        db = SessionLocal()
        try:
            cal.record_label(db, db.get(AnnotationTask, task_id), annotator="alice",
                             item_ref=f"i{n}", label=label)
            db.commit()
        finally:
            db.close()
    agreement = client.get(f"/api/annotation-tasks/{task_id}/agreement").json()
    assert agreement["pairs"] == 14
    assert agreement["human_human_kappa"] == pytest.approx(1.0)
    assert agreement["judge_human_kappa"] == pytest.approx(1.0)
    assert agreement["suggested_verdict"] == "aligned"


def test_annotate_routes_are_public_hub_global_and_mint_is_prod_protected():
    from app.core.route_policy import PROD_PROTECTED, PUBLIC, ROUTE_POLICY, WORKSPACE_EXEMPT

    public = {k for k in ROUTE_POLICY if k[1].startswith("/share/annotate/")}
    assert public == {("GET", "/share/annotate/{token}"),
                      ("POST", "/share/annotate/{token}/label")}
    assert all(ROUTE_POLICY[k] == PUBLIC and k in WORKSPACE_EXEMPT for k in public)
    # minting a link is a judge-calibration right, not open to any member
    mint = ("POST", "/api/annotation-tasks/{task_id}/links")
    assert ROUTE_POLICY[mint] != PUBLIC
    assert mint not in PROD_PROTECTED  # prod is refused by the service with a remedy


def test_the_person_who_minted_the_links_cannot_certify_the_judge(client):
    """Security review: two links minted by one person are two "agreeing raters" that
    are really one. Labelling through both and then deciding would certify a judge on
    that person's own labels, so the link issuer is refused like an annotator."""
    task_id = _task(items=14)
    first = client.post(f"/api/annotation-tasks/{task_id}/links", json={"label": "A"}).json()
    second = client.post(f"/api/annotation-tasks/{task_id}/links", json={"label": "B"}).json()
    db = SessionLocal()
    try:
        # only the two links are annotators: the issuer is not named on the task
        task = db.get(AnnotationTask, task_id)
        task.annotators = [first["annotator"], second["annotator"]]
        task.adjudicator = None
        db.commit()
    finally:
        db.close()
    for n in range(14):
        label = "pass" if n % 2 else "fail"  # exactly the judge's verdicts
        for link in (first, second):
            res = client.post(f"/share/annotate/{link['token']}/label",
                              json={"item_ref": f"i{n}", "label": label})
            assert res.status_code == 200
    agreement = client.get(f"/api/annotation-tasks/{task_id}/agreement").json()
    assert agreement["suggested_verdict"] == "aligned"  # the numbers look perfect
    decided = client.post(f"/api/annotation-tasks/{task_id}/decide",
                          json={"verdict": "aligned"})
    assert decided.status_code == 403
    assert decided.json()["code"] == "calibration.own_labels"


def test_the_adjudicator_cannot_certify_the_judge_either(client):
    """The adjudicator sets the consensus on disputed items, so it is evidence too."""
    task_id = _task(items=12)
    db = SessionLocal()
    try:
        task = db.get(AnnotationTask, task_id)
        task.adjudicator = ADMIN["username"]
        for who in ("alice", "bob"):
            for n in range(12):
                cal.record_label(db, task, annotator=who, item_ref=f"i{n}",
                                 label="pass" if n % 2 else "fail")
        db.commit()
    finally:
        db.close()
    decided = client.post(f"/api/annotation-tasks/{task_id}/decide",
                          json={"verdict": "aligned"})
    assert decided.status_code == 403
    assert decided.json()["code"] == "calibration.own_labels"
