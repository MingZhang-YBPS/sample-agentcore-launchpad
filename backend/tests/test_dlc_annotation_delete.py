"""An undecided labelling task can be discarded; a decided one is evidence and stays.

A create that outlived the proxy's timeout still finished on the server, so the
person who clicked Save again was left with duplicate tasks and no way to remove them
(live 2026-10-18: five identical S2 tasks)."""

from fastapi.testclient import TestClient

from app.core.db import SessionLocal
from app.models.dlc import Annotation, AnnotationTask
from tests.test_dlc_api import (  # noqa: F401 — fixtures
    ENG,
    admin,
    app,
    engineer,
)


def _task(client: TestClient) -> str:
    res = client.post("/api/annotation-tasks", json={
        "criterion_key": "R1", "annotators": [ENG["username"], "root"],
        "items": [{"ref": "i0", "input": "q", "answer": "a", "judge_label": "pass"}]})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_an_open_task_is_deleted_with_its_labels(engineer: TestClient):  # noqa: F811
    task_id = _task(engineer)
    labelled = engineer.post(f"/api/annotation-tasks/{task_id}/labels",
                             json={"item_ref": "i0", "label": "pass"})
    assert labelled.status_code == 201, labelled.text
    res = engineer.delete(f"/api/annotation-tasks/{task_id}")
    assert res.status_code == 200, res.text
    assert res.json()["labels"] == 1
    assert engineer.get(f"/api/annotation-tasks/{task_id}").status_code == 404
    with SessionLocal() as db:
        assert db.get(AnnotationTask, task_id) is None
        assert db.query(Annotation).filter(Annotation.task_id == task_id).count() == 0


def test_a_closed_task_stays(engineer: TestClient):  # noqa: F811
    task_id = _task(engineer)
    with SessionLocal() as db:
        db.get(AnnotationTask, task_id).status = "closed"
        db.commit()
    res = engineer.delete(f"/api/annotation-tasks/{task_id}")
    assert res.status_code == 409 and res.json()["code"] == "annotation.closed"
