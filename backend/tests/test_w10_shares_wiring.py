"""SHARE-2 wiring (W10-BE): the share-notice routes are mounted and hub-global, a
pipeline never writes into a shared dataset, and the gateway tool lists label the
targets another project lent (`shared_from`)."""

# ruff: noqa: F811 — the `app` / `world` fixtures are imported, then requested by name

from app.core.db import SessionLocal
from app.core.project_scope import project_context
from app.core.route_policy import MEMBER, ROUTE_POLICY, WORKSPACE_EXEMPT
from app.models.lambda_tools import LambdaToolTarget
from app.services import gateway_tools
from tests.test_project_scope import _login, app  # noqa: F401
from tests.test_resource_shares import _share, world  # noqa: F401
from tests.test_resource_shares_use import BETA, WS, _dataset, _in, bea  # noqa: F401

NOTICE_ROUTES = [("GET", "/api/me/share-notices"),
                 ("POST", "/api/me/share-notices/{notice_id}/ack")]


def test_the_notice_routes_are_member_and_hub_global():
    for key in NOTICE_ROUTES:
        assert ROUTE_POLICY[key] == MEMBER
        assert key in WORKSPACE_EXEMPT


def test_a_grantee_owner_reads_and_acknowledges_over_http(world, bea):
    dataset = _dataset("alpha", "lent")
    share = _share(world.olga, resource_id=dataset).json()["id"]
    world.olga.delete(f"/api/projects/alpha/shares/{share}")
    client = _login(world.app, "bea")
    res = client.get("/api/me/share-notices")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["unread"] == 1
    notice = body["items"][0]["id"]
    # another account does not see it, and cannot acknowledge it
    assert world.bob.get("/api/me/share-notices").json()["items"] == []
    assert world.bob.post(f"/api/me/share-notices/{notice}/ack").status_code == 404
    res = client.post(f"/api/me/share-notices/{notice}/ack")
    assert res.status_code == 200 and res.json() == {"ok": True, "notice_id": notice}
    assert client.get("/api/me/share-notices").json()["unread"] == 0


def test_a_pipeline_never_appends_to_a_shared_dataset(world):
    dataset = _dataset("alpha", "lent")
    _share(world.olga, resource_id=dataset, access="use")
    res = world.bob.post("/api/eval/datasets/from-sessions", headers=BETA,
                         json={"session_ids": ["session-0001"], "dataset_id": dataset})
    assert res.status_code == 404, res.text
    assert res.json()["code"] == "share.read_only"


def _lambda_target(pid: str, name: str) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = LambdaToolTarget(workspace_id=WS, target_name=name,
                                   lambda_arn="arn:aws:lambda:us-east-1:1:function:f")
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def test_the_lambda_list_labels_a_lent_target(world, monkeypatch):
    monkeypatch.setattr(gateway_tools.mcp_reviews, "state", lambda *_a, **_k: None)
    lent = _lambda_target("alpha", "lent-tool")
    mine = _lambda_target("beta", "mine-tool")
    _share(world.olga, kind="gateway_target", resource_id=lent)
    rows = {r["id"]: r for r in _in("beta", gateway_tools.list_lambda, WS)}
    assert rows[lent]["shared_from"] == {"project_id": "alpha", "name": "Alpha",
                                         "access": "read"}
    assert "shared_from" not in rows[mine]
    # the owner's own list carries no label
    assert "shared_from" not in {r["id"]: r for r in
                                 _in("alpha", gateway_tools.list_lambda, WS)}[lent]
