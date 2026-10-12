"""T26/T27 — blocking release gates, the plan preview, execution, resume and rollback.

AWS is stubbed entirely. The inner deploy runs the REAL `deployer.pipeline` with stubbed
stage functions, so the promotion's provision/deploy/wait path is exercised for real; the
runtime canary, invocation and artifact copy are replaced at their seams. The worker runs
inline (no threads) so every assertion sees a finished run, except the resume test which
uses the real startup resume.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.route_policy import PROD_PROTECTED, PROD_UNPROTECTED_AGENT_ROUTES, ROUTE_POLICY
from app.deployer import pipeline
from app.evaluation.models import EvalRun
from app.main import create_app
from app.models.ledger import (
    Agent,
    Deployment,
    Job,
    Promotion,
    ReleaseBundle,
    Workspace,
)
from app.services import promotion as promotion_service
from app.services import promotion_exec, release_gates
from app.services import users as users_service

ADMIN_CREDS = {"username": "exec-admin", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "exec-builder",
    "email": "exec-builder@acme-corp.com",
    "password": "sufficient-pass",
}
TARGET = {"id": "prod-ws", "name": "Prod", "account_id": "444455556677", "region": "us-west-1"}
NAME = "ship-it"


def spec(prompt: str = "v1", method: str = "harness") -> dict:
    return {
        "name": NAME,
        "method": method,
        "system_prompt": prompt,
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


@pytest.fixture
def admin(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        assert client.post("/api/workspaces", json=TARGET).status_code in (200, 201)
        _target(bootstrap_status="ready", resources={"execution_role_arn": "arn:aws:iam::4:role/x"})
        yield client


@pytest.fixture
def member(gated_app):
    with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.commit()
        finally:
            db.close()
        login = client.post(
            "/api/auth/login",
            json={"username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"]},
        )
        assert login.status_code == 200, login.text
        # granted on the source only — the release-policy refusal below is about the role,
        # and a member never needs the target to be told they are not an administrator
        yield client


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: sleeping advances it, so an observation window costs no real time."""
    state = {"now": datetime.now(UTC)}
    monkeypatch.setattr(promotion_exec, "_now", lambda: state["now"])
    monkeypatch.setattr(
        promotion_exec,
        "_sleep",
        lambda seconds: state.__setitem__("now", state["now"] + timedelta(seconds=seconds)),
    )
    return state


@pytest.fixture
def world(monkeypatch, clock):
    """Stub AWS: pipeline stages, smoke invocation, the policy engine read."""
    calls = {"deploy": 0, "invoke": [], "answer": "hello"}

    def deploy(ctx, agent):
        calls["deploy"] += 1
        db = ctx.session()
        try:
            row = db.get(Agent, agent.id)
            row.arn = "arn:aws:bedrock-agentcore:us-west-1:4:runtime/rt-1"
            row.resource_id = "rt-1"
            row.version = str(calls["deploy"])
            db.commit()
        finally:
            db.close()
        return pipeline.StageResult(detail="stub deployed")

    monkeypatch.setitem(pipeline._METHODS, "harness", {"deploy": deploy})
    monkeypatch.setattr(
        pipeline,
        "start_deploy_async",
        lambda job_id: pipeline.execute_deploy_job(job_id, resume=False),
    )
    monkeypatch.setattr(
        promotion_exec,
        "start_async",
        lambda job_id, **_: promotion_exec.execute_job(job_id, resume=False),
    )

    def invoke(agent_id, prompt, workspace):
        calls["invoke"].append(prompt)
        return calls["answer"]

    monkeypatch.setattr(promotion_exec, "_invoke", invoke)
    monkeypatch.setattr(
        release_gates, "read_policy_mode", lambda _ws: {"engine": "arn:e", "mode": "ENFORCE"}
    )
    return calls


def _target(**fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(Workspace, TARGET["id"])
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def seed_bundle(
    prompt: str = "v1",
    *,
    method: str = "harness",
    score: float | None = 0.9,
    artifact: dict | None = None,
) -> str:
    """A bundle in `default` with (optionally) a completed evaluation run pinned to it."""
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name=NAME, method=method, status="active",
            spec=spec(prompt, method),
        )
        db.add(agent)
        db.flush()
        evaluation: dict = {}
        if score is not None:
            run = EvalRun(
                workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=NAME,
                dataset_id="ds1", dataset_version="2", status="completed",
                scores=[{"evaluatorId": "e", "score": score}],
            )
            db.add(run)
            db.flush()
            evaluation = {
                "run_id": run.id, "dataset_id": "ds1", "dataset_version": "2",
                "scores": run.scores,
            }
        artifact = artifact or {"aws_version": "1"}
        bundle = ReleaseBundle(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=NAME,
            method=method, spec=spec(prompt, method), artifact=artifact,
            evaluation=evaluation, policy={},
            digest=promotion_service.bundle_digest(
                agent_name=NAME, method=method, spec=spec(prompt, method), artifact=artifact
            ),
        )
        db.add(bundle)
        db.commit()
        return bundle.id
    finally:
        db.close()


def seed_promotion(bundle_id: str, status: str = "approved") -> str:
    db = SessionLocal()
    try:
        row = Promotion(
            workspace_id=DEFAULT_WORKSPACE_ID, bundle_id=bundle_id,
            target_workspace_id=TARGET["id"], status=status, requested_by="someone-else",
            change_note="ship", rollback_note="revert", reviewed_by="reviewer",
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def promotion_row(promotion_id: str) -> Promotion:
    db = SessionLocal()
    try:
        row = db.get(Promotion, promotion_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def target_agent() -> Agent | None:
    db = SessionLocal()
    try:
        return db.scalars(
            select(Agent).where(Agent.workspace_id == TARGET["id"], Agent.name == NAME)
        ).first()
    finally:
        db.close()


def stage_status(promotion_id: str) -> dict[str, str]:
    return {s["name"]: s["status"] for s in promotion_row(promotion_id).stages}


def execute(client, promotion_id: str):
    return client.post(f"/api/promotions/{promotion_id}/execute")


# ── T26: blocking gates ──────────────────────────────────────────────────────────


def test_execute_refuses_a_promotion_that_is_not_approved(admin, world):
    promotion_id = seed_promotion(seed_bundle(), status="pending")
    refused = execute(admin, promotion_id)
    assert refused.status_code == 409
    assert refused.json()["code"] == "promotion.not_executable"


def test_a_missing_or_low_evaluation_blocks_execution_and_names_the_gate(admin, world):
    none = seed_promotion(seed_bundle(score=None))
    missing = execute(admin, none)
    assert missing.status_code == 409
    assert missing.json()["code"] == "promotion.gate_failed.evaluation"

    low = seed_promotion(seed_bundle("v2", score=0.4))
    refused = execute(admin, low)
    assert refused.status_code == 409
    body = refused.json()
    assert body["code"] == "promotion.gate_failed.eval_score"
    assert body["detail"]["gate"] == "eval_score"
    check = next(c for c in body["detail"]["checks"] if c["key"] == "eval_score")
    assert check["dataset_version"] == "2" and check["threshold"] == 0.7
    assert "below" in check["detail"]
    assert promotion_row(low).status == "approved"  # nothing started


def test_the_threshold_is_configurable_by_an_administrator(admin, world):
    promotion_id = seed_promotion(seed_bundle(score=0.4))
    assert execute(admin, promotion_id).status_code == 409
    put = admin.put(f"/api/release-policies/{TARGET['id']}", json={"min_eval_score": 0.3})
    assert put.status_code == 200, put.text
    assert put.json()["effective"]["min_eval_score"] == 0.3
    assert execute(admin, promotion_id).status_code == 202


def test_only_an_administrator_edits_the_release_policy(admin, member):
    denied = member.put(f"/api/release-policies/{TARGET['id']}", json={"min_eval_score": 0.1})
    assert denied.status_code == 403
    # reading a workspace's policy needs a grant on it (security review finding: it used
    # to be readable for any workspace id by any member)
    unreadable = member.get(f"/api/release-policies/{TARGET['id']}")
    assert unreadable.status_code == 403
    assert unreadable.json()["code"] == "workspace.forbidden"
    bad = admin.put(f"/api/release-policies/{TARGET['id']}", json={"window": {"start": "9"}})
    assert bad.status_code == 422 and bad.json()["code"] == "release_policy.invalid"


def test_a_prod_target_needs_its_gateway_policy_engine_in_enforce(admin, world, monkeypatch):
    _target(tier="prod")
    promotion_id = seed_promotion(seed_bundle())
    monkeypatch.setattr(
        release_gates, "read_policy_mode", lambda _ws: {"engine": "arn:e", "mode": "LOG_ONLY"}
    )
    refused = execute(admin, promotion_id)
    assert refused.status_code == 409
    assert refused.json()["code"] == "promotion.gate_failed.policy_enforce"
    assert "LOG_ONLY" in refused.json()["message"]

    monkeypatch.setattr(
        release_gates, "read_policy_mode", lambda _ws: {"engine": None, "mode": None}
    )
    assert execute(admin, promotion_id).json()["code"] == "promotion.gate_failed.policy_enforce"

    monkeypatch.setattr(
        release_gates, "read_policy_mode", lambda _ws: {"engine": "arn:e", "mode": "ENFORCE"}
    )
    assert execute(admin, promotion_id).status_code == 202


def test_an_unreadable_policy_engine_fails_the_gate_rather_than_passing(
    admin, world, monkeypatch
):
    _target(tier="prod")

    def boom(_ws):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(release_gates, "read_policy_mode", boom)
    refused = execute(admin, seed_promotion(seed_bundle()))
    assert refused.json()["code"] == "promotion.gate_failed.policy_enforce"


def test_a_dev_target_does_not_require_enforce(admin, world, monkeypatch):
    monkeypatch.setattr(
        release_gates, "read_policy_mode", lambda _ws: (_ for _ in ()).throw(AssertionError)
    )
    assert execute(admin, seed_promotion(seed_bundle())).status_code == 202


def test_a_freeze_refuses_with_the_next_allowed_time(admin, world):
    now = datetime.now(UTC)
    end = (now + timedelta(hours=3)).replace(microsecond=0)
    put = admin.put(
        f"/api/release-policies/{TARGET['id']}",
        json={
            "freezes": [
                {
                    "start": (now - timedelta(hours=1)).isoformat(),
                    "end": end.isoformat(),
                    "reason": "quarter close",
                }
            ]
        },
    )
    assert put.status_code == 200, put.text
    refused = execute(admin, seed_promotion(seed_bundle()))
    assert refused.status_code == 409
    body = refused.json()
    assert body["code"] == "promotion.gate_failed.deploy_window"
    assert "quarter close" in body["message"]
    assert datetime.fromisoformat(body["detail"]["next_allowed_at"]) == end


def test_the_window_math_names_the_next_open_slot():
    policy = {
        "window": {"timezone": "UTC", "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00"},
        "freezes": [],
    }
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    check = release_gates.deploy_window_check(policy, saturday)
    assert not check["ok"]
    assert check["next_allowed_at"] == datetime(2026, 10, 5, 9, 0, tzinfo=UTC).isoformat()
    assert release_gates.deploy_window_check(policy, datetime(2026, 10, 5, 10, 0, tzinfo=UTC))["ok"]
    # 17:00 is the end of the window, exclusive
    late = release_gates.deploy_window_check(policy, datetime(2026, 10, 5, 17, 0, tzinfo=UTC))
    assert not late["ok"]
    assert late["next_allowed_at"] == datetime(2026, 10, 6, 9, 0, tzinfo=UTC).isoformat()
    # a freeze over Monday pushes the slot to the moment it lifts, if that is in-window
    frozen = {
        **policy,
        "freezes": [
            {"start": "2026-10-05T00:00:00+00:00", "end": "2026-10-05T12:00:00+00:00", "reason": ""}
        ],
    }
    check = release_gates.deploy_window_check(frozen, datetime(2026, 10, 5, 10, 0, tzinfo=UTC))
    assert check["next_allowed_at"] == datetime(2026, 10, 5, 12, 0, tzinfo=UTC).isoformat()


def test_an_unmapped_reference_blocks_execution_even_after_approval(admin, world):
    db = SessionLocal()
    try:
        bundle_id = seed_bundle()
        row = db.get(ReleaseBundle, bundle_id)
        row.spec = {**row.spec, "knowledge_bases": [{"kb_id": "KB12345", "name": "hr"}]}
        db.commit()
    finally:
        db.close()
    refused = execute(admin, seed_promotion(bundle_id))
    assert refused.status_code == 409
    assert refused.json()["code"] == "promotion.gate_failed.resource_mapping"


# ── plan preview ─────────────────────────────────────────────────────────────────


def test_the_plan_is_read_only_and_describes_the_target_change(admin, world):
    promotion_id = seed_promotion(seed_bundle(artifact={"image_digest": "sha256:abc"}))
    db = SessionLocal()
    before = (db.scalar(select(func.count()).select_from(Agent)),
              db.scalar(select(func.count()).select_from(Job)))
    db.close()

    plan = admin.get(f"/api/promotions/{promotion_id}/plan")
    assert plan.status_code == 200, plan.text
    body = plan.json()
    items = {item["key"]: item for item in body["items"]}
    assert body["mode"] == "create" and items["agent"]["action"] == "create"
    assert items["iam_role"]["action"] == "reuse"
    assert items["artifact_copy"]["action"] == "copy"
    assert items["registry_record"]["action"] == "none"  # the target has no registry
    assert items["canary"]["action"] == "skip"
    assert {"eval_score", "policy_enforce", "deploy_window"} <= {
        c["key"] for c in body["gates"]["checks"]
    }
    assert body["can_execute"] is True and body["blocked_by"] == []

    db = SessionLocal()
    after = (db.scalar(select(func.count()).select_from(Agent)),
             db.scalar(select(func.count()).select_from(Job)))
    db.close()
    assert before == after and world["deploy"] == 0


def test_the_plan_says_replace_when_the_target_already_runs_the_agent(admin, world):
    first = seed_promotion(seed_bundle())
    assert execute(admin, first).status_code == 202
    second = seed_promotion(seed_bundle("v2"))
    body = admin.get(f"/api/promotions/{second}/plan").json()
    assert body["mode"] == "replace"
    assert next(i for i in body["items"] if i["key"] == "agent")["action"] == "replace"


def test_the_plan_reports_which_gate_blocks(admin, world):
    body = admin.get(f"/api/promotions/{seed_promotion(seed_bundle(score=0.1))}/plan").json()
    assert body["can_execute"] is False
    assert "eval_score" in body["blocked_by"]


# ── T27: execution ───────────────────────────────────────────────────────────────


def test_a_create_release_runs_every_stage_and_lands_active(admin, world):
    promotion_id = seed_promotion(seed_bundle())
    started = execute(admin, promotion_id)
    assert started.status_code == 202, started.text
    assert started.json()["job_id"]

    row = promotion_row(promotion_id)
    assert row.status == "succeeded" and row.error is None
    assert row.started_at and row.finished_at
    statuses = stage_status(promotion_id)
    assert statuses == {
        "resolve": "succeeded", "copy": "skipped", "provision": "succeeded",
        "deploy": "succeeded", "smoke": "succeeded", "canary": "skipped",
        "observe": "skipped", "complete": "succeeded",
    }
    agent = target_agent()
    assert agent.status == "active" and agent.workspace_id == TARGET["id"]
    assert agent.spec["system_prompt"] == "v1"
    assert len(world["invoke"]) == 3 and world["deploy"] == 1

    progress = admin.get(f"/api/promotions/{promotion_id}/execution").json()
    assert progress["promotion"]["status"] == "succeeded"
    assert {line["stage"] for line in progress["log"]} >= {"resolve", "deploy", "smoke", "canary"}
    canary_line = next(
        line for line in progress["log"] if line["stage"] == "canary" and "skipped" in line["msg"]
    )
    assert "no running agent" in canary_line["msg"]  # says why, explicitly
    job = SessionLocal().get(Job, row.job_id)
    assert job.status == "succeeded" and job.workspace_id == TARGET["id"]


def test_a_second_execute_while_executing_is_refused(admin, world):
    promotion_id = seed_promotion(seed_bundle())
    db = SessionLocal()
    row = db.get(Promotion, promotion_id)
    row.status = "executing"
    db.commit()
    db.close()
    refused = execute(admin, promotion_id)
    assert refused.status_code == 409 and refused.json()["code"] == "promotion.already_executing"


def test_a_failing_smoke_test_stops_the_run_and_says_where(admin, world):
    world["answer"] = ""
    promotion_id = seed_promotion(seed_bundle())
    assert execute(admin, promotion_id).status_code == 202
    row = promotion_row(promotion_id)
    assert row.status == "failed"
    assert row.error.startswith("smoke:") and "3/3" in row.error
    statuses = stage_status(promotion_id)
    assert statuses["deploy"] == "succeeded" and statuses["smoke"] == "failed"
    assert statuses["canary"] == statuses["observe"] == statuses["complete"] == "pending"
    body = admin.get(f"/api/promotions/{promotion_id}/execution").json()
    assert body["promotion"]["failed_stage"] == "smoke"
    assert any(line["level"] == "error" and line["stage"] == "smoke" for line in body["log"])
    assert SessionLocal().get(Job, row.job_id).status == "failed"


def test_a_failing_deploy_is_reported_at_the_deploy_stage(admin, world, monkeypatch):
    def broken(ctx, agent):
        raise RuntimeError("CreateAgentRuntime denied")

    monkeypatch.setitem(pipeline._METHODS, "harness", {"deploy": broken})
    promotion_id = seed_promotion(seed_bundle())
    assert execute(admin, promotion_id).status_code == 202
    row = promotion_row(promotion_id)
    assert row.status == "failed" and row.error.startswith("deploy:")
    assert "CreateAgentRuntime denied" in row.error
    assert stage_status(promotion_id)["smoke"] == "pending"
    assert world["invoke"] == []


def test_a_failed_release_can_be_retried(admin, world):
    world["answer"] = ""
    promotion_id = seed_promotion(seed_bundle())
    execute(admin, promotion_id)
    assert promotion_row(promotion_id).status == "failed"
    world["answer"] = "fine now"
    assert execute(admin, promotion_id).status_code == 202
    assert promotion_row(promotion_id).status == "succeeded"


def test_the_artifact_copy_is_called_when_the_bundle_has_one(admin, world, monkeypatch):
    from app.services import artifact_copy

    seen = {}

    def copier(bundle, source, target, **_):
        seen["artifact"] = bundle.artifact
        seen["target_account"] = target.account_id
        return artifact_copy.CopyResult(
            kind="container_image",
            status="copied",
            target_artifact={"image_uri": "444455556677.dkr.ecr.us-west-1.amazonaws.com/x@sha:a"},
            steps=["copied 3 layers"],
        )

    monkeypatch.setattr(artifact_copy, "copy_artifact", copier)
    promotion_id = seed_promotion(seed_bundle(artifact={"image_digest": "sha256:abc"}))
    assert execute(admin, promotion_id).status_code == 202
    row = promotion_row(promotion_id)
    assert row.status == "succeeded", row.error
    assert seen["artifact"]["image_digest"] == "sha256:abc"
    assert seen["target_account"] == TARGET["account_id"]
    assert stage_status(promotion_id)["copy"] == "succeeded"
    assert row.execution["artifact_copy"]["status"] == "copied"


def test_a_copy_refusal_stops_the_run_before_anything_is_created(admin, world, monkeypatch):
    from app.services import artifact_copy

    def refuse(bundle, source, target, **_):
        raise AppError("promotion.artifact_digest_mismatch", "digest differs", status_code=409)

    monkeypatch.setattr(artifact_copy, "copy_artifact", refuse)
    promotion_id = seed_promotion(seed_bundle(artifact={"image_digest": "sha256:abc"}))
    execute(admin, promotion_id)
    row = promotion_row(promotion_id)
    assert row.status == "failed" and row.error.startswith("copy:")
    assert target_agent() is None and world["deploy"] == 0


def test_the_copied_coordinates_are_overlaid_on_a_byoc_spec():
    out = promotion_exec.overlay_target_artifact(
        {"byoc": {"upload_id": "old", "image_uri": "src", "artifact_kind": "code_zip"}},
        {"upload_id": "new"},
    )
    assert out["byoc"] == {"upload_id": "new", "image_uri": "src", "artifact_kind": "code_zip"}
    assert promotion_exec.overlay_target_artifact({"a": 1}, {"upload_id": "x"}) == {"a": 1}


# ── the canary path ──────────────────────────────────────────────────────────────


class FakeCanary:
    """Stands in for `optimization.canary_service`: three ramp stages, scripted verdicts."""

    def __init__(self, block_at: int | None = None):
        self.calls: list[str] = []
        self.stage = 0
        self.traffic_sent = False
        self.verdict_set = False
        self.status = "running"
        self.set_up = False
        self.block_at = block_at

    def start(self, agent_id, spec, workspace):
        self.calls.append("start")
        return "cn1"

    def state(self, canary_id):
        return {
            "status": self.status, "set_up": self.set_up, "ramp_stage": self.stage,
            "weights": {}, "has_traffic": self.traffic_sent, "has_verdict": self.verdict_set,
            "verdict": "treatment-wins",
        }

    def setup(self, canary_id, progress):
        self.calls.append("setup")
        self.set_up = True

    def traffic(self, canary_id, prompts, info, progress):
        self.calls.append(f"traffic@{self.stage}")
        assert len(prompts) >= 3
        self.traffic_sent = True

    def verdict(self, canary_id, progress):
        self.calls.append(f"verdict@{self.stage}")
        self.verdict_set = True

    def check(self, canary_id):
        if self.block_at == self.stage:
            raise AppError("canary.verdict_blocked", "control-wins cannot advance", status_code=409)

    def advance(self, canary_id, progress):
        self.calls.append(f"advance@{self.stage}")
        self.stage += 1
        self.traffic_sent = self.verdict_set = False

    def complete(self, canary_id, progress):
        self.calls.append("complete")
        self.status = "completed"

    def rollback(self, canary_id, progress):
        self.calls.append("rollback")
        self.status = "rolled_back"

    def cleanup(self, canary_id, progress):
        self.calls.append("cleanup")


def seed_running_target(method: str = "zip_runtime") -> None:
    db = SessionLocal()
    try:
        db.add(
            Agent(
                workspace_id=TARGET["id"], name=NAME, method=method, status="active",
                spec=spec("old", method), version="7",
                arn="arn:aws:bedrock-agentcore:us-west-1:4:runtime/rt-old", resource_id="rt-old",
            )
        )
        db.commit()
    finally:
        db.close()


def test_an_existing_runtime_agent_is_released_through_the_canary_ramp(
    admin, world, monkeypatch
):
    fake = FakeCanary()
    monkeypatch.setattr(promotion_exec, "CANARY", fake)
    seed_running_target()
    promotion_id = seed_promotion(seed_bundle(method="zip_runtime"))
    assert execute(admin, promotion_id).status_code == 202, promotion_row(promotion_id).error
    row = promotion_row(promotion_id)
    assert row.status == "succeeded", row.error
    assert fake.calls == [
        "start", "setup",
        "traffic@0", "verdict@0", "advance@0",   # 90/10 -> 50/50
        "traffic@1", "verdict@1", "advance@1",   # 50/50 -> 1/99
        "traffic@2", "verdict@2",
        "complete", "cleanup",
    ]
    statuses = stage_status(promotion_id)
    assert statuses["canary"] == "succeeded" and statuses["deploy"] == "succeeded"
    assert world["deploy"] == 0  # nothing was published over the champion directly


def test_an_existing_harness_agent_is_released_directly_not_through_a_canary(
    admin, world, monkeypatch
):
    # A Harness canary A/Bs two EXISTING versions; minting a candidate from the bundle's
    # spec is runtime-only and called GetAgentRuntime on the Harness id (found by the
    # cross-region release e2e)
    fake = FakeCanary()
    monkeypatch.setattr(promotion_exec, "CANARY", fake)
    seed_running_target("harness")
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter(Agent.workspace_id == TARGET["id"]).one()
        agent.arn = "arn:aws:bedrock-agentcore:us-west-1:4:harness/h-old"
        db.commit()
    finally:
        db.close()
    promotion_id = seed_promotion(seed_bundle(method="harness"))
    assert execute(admin, promotion_id).status_code == 202, promotion_row(promotion_id).error
    row = promotion_row(promotion_id)
    assert row.status == "succeeded", row.error
    assert fake.calls == []
    assert stage_status(promotion_id)["canary"] == "skipped"
    assert world["deploy"] == 1


def test_a_canary_that_blocks_fails_the_release_and_rolls_the_canary_back(
    admin, world, monkeypatch
):
    fake = FakeCanary(block_at=1)
    monkeypatch.setattr(promotion_exec, "CANARY", fake)
    seed_running_target()
    promotion_id = seed_promotion(seed_bundle(method="zip_runtime"))
    execute(admin, promotion_id)
    row = promotion_row(promotion_id)
    assert row.status == "failed" and row.error.startswith("canary:")
    assert "50" in row.error
    assert "complete" not in fake.calls
    # rollback THEN cleanup: rolling back alone left the canary's gateway, A/B test and
    # candidate endpoint in place — billable, and it made the agent undeletable
    # (found by the cross-region release e2e)
    assert fake.calls[-2:] == ["rollback", "cleanup"]
    assert stage_status(promotion_id)["observe"] == "pending"


def test_a_canary_whose_rollback_fails_is_not_torn_down(admin, world, monkeypatch):
    """Cleanup must never run on a canary that may still be routing traffic."""
    fake = FakeCanary(block_at=0)

    def broken_rollback(canary_id, progress):
        fake.calls.append("rollback")
        raise RuntimeError("gateway update failed")

    fake.rollback = broken_rollback
    monkeypatch.setattr(promotion_exec, "CANARY", fake)
    seed_running_target()
    promotion_id = seed_promotion(seed_bundle(method="zip_runtime"))
    execute(admin, promotion_id)
    assert promotion_row(promotion_id).status == "failed"
    assert "rollback" in fake.calls and "cleanup" not in fake.calls


def test_a_prod_release_holds_an_observation_window_then_probes_again(
    admin, world, clock, monkeypatch
):
    _target(tier="prod")
    promotion_id = seed_promotion(seed_bundle())
    start = clock["now"]
    assert execute(admin, promotion_id).status_code == 202
    row = promotion_row(promotion_id)
    assert row.status == "succeeded", row.error
    assert clock["now"] - start >= timedelta(seconds=300)
    assert stage_status(promotion_id)["observe"] == "succeeded"
    assert len(world["invoke"]) == 6  # smoke, then the probe at the end of the window


def test_a_probe_failing_inside_the_window_fails_the_release(admin, world, clock, monkeypatch):
    _target(tier="prod")
    promotion_id = seed_promotion(seed_bundle())
    original = promotion_exec._invoke

    def flaky(agent_id, prompt, workspace):
        if clock["now"] > start + timedelta(seconds=200):
            return ""
        return original(agent_id, prompt, workspace)

    start = clock["now"]
    monkeypatch.setattr(promotion_exec, "_invoke", flaky)
    execute(admin, promotion_id)
    row = promotion_row(promotion_id)
    assert row.status == "failed" and row.error.startswith("observe:")


# ── resume ───────────────────────────────────────────────────────────────────────


def test_a_restart_resumes_at_the_first_unfinished_stage(admin, world, monkeypatch):
    promotion_id = seed_promotion(seed_bundle())
    real_smoke = promotion_exec._stage_smoke
    launched = []
    monkeypatch.setattr(promotion_exec, "start_async", lambda job_id, **_: launched.append(job_id))
    job_id = execute(admin, promotion_id).json()["job_id"]
    assert launched == [job_id]  # admitted and handed to a worker, which then "dies"

    def crash(run):
        raise SystemExit("process died")  # not an Exception: nothing marks the run failed

    monkeypatch.setattr(promotion_exec, "_stage_smoke", crash)
    with pytest.raises(SystemExit):
        promotion_exec.execute_job(job_id, resume=False)
    row = promotion_row(promotion_id)
    assert row.status == "executing"
    assert stage_status(promotion_id)["deploy"] == "succeeded"
    assert stage_status(promotion_id)["smoke"] == "running"
    assert SessionLocal().get(Job, job_id).status == "running"
    assert world["deploy"] == 1

    # a fresh (non-resume) launch must not adopt a job another process left running
    monkeypatch.setattr(promotion_exec, "_stage_smoke", real_smoke)
    promotion_exec.execute_job(job_id, resume=False)
    assert promotion_row(promotion_id).status == "executing"

    # the startup resume finds the job and re-enters at the first unfinished stage
    monkeypatch.setattr(
        promotion_exec,
        "start_resume",
        lambda jid: promotion_exec.execute_job(jid, resume=True),
    )
    assert job_id in pipeline.resume_pending_jobs()
    done = promotion_row(promotion_id)
    assert done.status == "succeeded", done.error
    assert world["deploy"] == 1  # deploy was not repeated
    assert stage_status(promotion_id)["smoke"] == "succeeded"


def test_a_terminal_job_is_inert_on_resume(admin, world):
    promotion_id = seed_promotion(seed_bundle())
    execute(admin, promotion_id)
    job_id = promotion_row(promotion_id).job_id
    calls = world["deploy"]
    promotion_exec.execute_job(job_id, resume=True)
    assert world["deploy"] == calls and promotion_row(promotion_id).status == "succeeded"


# ── rollback ─────────────────────────────────────────────────────────────────────


def test_rollback_redeploys_the_previous_bundle_as_a_new_publish(admin, world):
    first = seed_promotion(seed_bundle("v1"))
    execute(admin, first)
    second = seed_promotion(seed_bundle("v2"))
    execute(admin, second)
    assert target_agent().spec["system_prompt"] == "v2"
    row = promotion_row(second)
    assert row.status == "succeeded" and row.previous_bundle_id == promotion_row(first).bundle_id

    db = SessionLocal()
    deployments = db.scalar(select(func.count()).select_from(Deployment))
    db.close()
    rolled = admin.post(f"/api/promotions/{second}/rollback")
    assert rolled.status_code == 202, rolled.text

    after = promotion_row(second)
    assert after.status == "rolled_back" and after.error is None
    assert target_agent().spec["system_prompt"] == "v1"
    assert target_agent().status == "active"
    db = SessionLocal()
    assert db.scalar(select(func.count()).select_from(Deployment)) == deployments + 1
    db.close()
    statuses = stage_status(second)
    assert statuses["canary"] == "skipped" and statuses["observe"] == "skipped"
    assert statuses["complete"] == "succeeded"


def test_rollback_is_refused_when_there_is_no_previous_bundle(admin, world):
    only = seed_promotion(seed_bundle())
    execute(admin, only)
    refused = admin.post(f"/api/promotions/{only}/rollback")
    assert refused.status_code == 409
    assert refused.json()["code"] == "promotion.no_previous_bundle"
    assert promotion_row(only).status == "succeeded"  # untouched


def test_rollback_needs_a_released_promotion(admin, world):
    refused = admin.post(f"/api/promotions/{seed_promotion(seed_bundle())}/rollback")
    assert refused.status_code == 409 and refused.json()["code"] == "promotion.not_rollbackable"


def test_a_failed_rollback_is_reported_as_failed_and_can_be_retried(admin, world, monkeypatch):
    first = seed_promotion(seed_bundle("v1"))
    execute(admin, first)
    second = seed_promotion(seed_bundle("v2"))
    execute(admin, second)
    world["answer"] = ""
    admin.post(f"/api/promotions/{second}/rollback")
    row = promotion_row(second)
    assert row.status == "failed" and row.error.startswith("rollback failed at smoke")
    world["answer"] = "ok"
    assert admin.post(f"/api/promotions/{second}/rollback").status_code == 202
    assert promotion_row(second).status == "rolled_back"


# ── classification ───────────────────────────────────────────────────────────────


def test_the_new_routes_are_classified_and_have_a_prod_decision():
    for method, path, role in (
        ("GET", "/api/promotions/{promotion_id}/plan", "member"),
        ("GET", "/api/promotions/{promotion_id}/execution", "member"),
        ("POST", "/api/promotions/{promotion_id}/execute", "perm:promotion.approve"),
        ("POST", "/api/promotions/{promotion_id}/rollback", "perm:promotion.approve"),
        ("GET", "/api/release-policies/{workspace_id}", "member"),
        ("PUT", "/api/release-policies/{workspace_id}", "admin"),
    ):
        assert ROUTE_POLICY[(method, path)] == role
    for key in (
        ("POST", "/api/promotions/{promotion_id}/execute"),
        ("POST", "/api/promotions/{promotion_id}/rollback"),
    ):
        # not member-reachable at all, and decided explicitly (with a reason) for prod
        assert key in PROD_UNPROTECTED_AGENT_ROUTES and key not in PROD_PROTECTED


def test_a_plain_member_cannot_execute_or_roll_back(admin, member, world):
    promotion_id = seed_promotion(seed_bundle())
    for action in ("execute", "rollback"):
        denied = member.post(f"/api/promotions/{promotion_id}/{action}")
        assert denied.status_code == 403
        assert denied.json()["detail"]["permission"] == "promotion.approve"
    assert promotion_row(promotion_id).status == "approved"


# ── a stalled probe must not hang the release (found live in the cross-region e2e) ───


def test_a_single_stalled_probe_is_retried_and_the_release_completes(admin, world, monkeypatch):
    """The first call to a Harness that just turned READY stalled forever in the
    cross-region e2e. One stall per prompt is absorbed by the retry."""
    import threading

    monkeypatch.setattr(promotion_exec, "PROBE_TIMEOUT_SECONDS", 0.2)
    release = threading.Event()
    stalled: set[str] = set()

    def first_call_hangs(agent_id, prompt, workspace):
        if prompt not in stalled:
            stalled.add(prompt)
            release.wait(5)  # an abandoned attempt: never answers in time
            return "late"
        return "ok"

    monkeypatch.setattr(promotion_exec, "_invoke", first_call_hangs)
    promotion_id = seed_promotion(seed_bundle())
    assert execute(admin, promotion_id).status_code == 202
    release.set()
    row = promotion_row(promotion_id)
    assert row.status == "succeeded", row.error
    log = admin.get(f"/api/promotions/{promotion_id}/execution").json()["log"]
    assert any("attempt 1/2: timed out" in line["msg"] for line in log)


def _client_error(code: str, message: str = "boom"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "InvokeHarness")


def test_a_probe_hitting_a_cold_start_is_retried_and_the_release_completes(
    admin, world, monkeypatch
):
    """A Harness just redeployed by a rollback answered "Runtime initialization time
    exceeded" to 1 of 3 smoke prompts in the cross-region e2e; the replay answers."""
    failed: set[str] = set()

    def first_call_cold(agent_id, prompt, workspace):
        if prompt not in failed:
            failed.add(prompt)
            raise _client_error("RuntimeClientError", "Runtime initialization time exceeded")
        return "ok"

    monkeypatch.setattr(promotion_exec, "_invoke", first_call_cold)
    promotion_id = seed_promotion(seed_bundle())
    assert execute(admin, promotion_id).status_code == 202
    row = promotion_row(promotion_id)
    assert row.status == "succeeded", row.error
    log = admin.get(f"/api/promotions/{promotion_id}/execution").json()["log"]
    assert any("attempt 1/2: ClientError" in line["msg"] for line in log)


def test_a_probe_with_a_final_error_is_not_retried(admin, world, monkeypatch):
    calls: list[str] = []

    def denied(agent_id, prompt, workspace):
        calls.append(prompt)
        raise _client_error("AccessDeniedException")

    monkeypatch.setattr(promotion_exec, "_invoke", denied)
    promotion_id = seed_promotion(seed_bundle())
    execute(admin, promotion_id)
    row = promotion_row(promotion_id)
    assert row.status == "failed" and row.error.startswith("smoke:")
    assert len(calls) == len(set(calls))  # each prompt tried exactly once


def test_a_probe_that_keeps_stalling_fails_the_release_instead_of_hanging(
    admin, world, monkeypatch
):
    import threading

    monkeypatch.setattr(promotion_exec, "PROBE_TIMEOUT_SECONDS", 0.2)
    never = threading.Event()
    monkeypatch.setattr(
        promotion_exec, "_invoke", lambda agent_id, prompt, workspace: never.wait(5) and ""
    )
    promotion_id = seed_promotion(seed_bundle())
    assert execute(admin, promotion_id).status_code == 202
    never.set()
    row = promotion_row(promotion_id)
    # an honest terminal state: not `executing` forever, and the target is free again
    assert row.status == "failed"
    assert row.error.startswith("smoke:") and "timed out" in row.error
    assert stage_status(promotion_id)["smoke"] == "failed"


def test_a_direct_release_that_fails_smoke_puts_the_previous_spec_back(admin, world):
    """A non-canary update is live once deployed; a smoke failure must not leave it (F4)."""
    seed_running_target("harness")
    db = SessionLocal()
    try:
        target = db.query(Agent).filter(Agent.workspace_id == TARGET["id"]).one()
        target.arn = "arn:aws:bedrock-agentcore:us-west-1:4:harness/h-old"
        db.commit()
        target_id = target.id
    finally:
        db.close()
    world["answer"] = ""  # every smoke prompt comes back empty
    promotion_id = seed_promotion(seed_bundle("v2", method="harness"))
    assert execute(admin, promotion_id).status_code == 202
    row = promotion_row(promotion_id)
    assert row.status == "failed" and row.error.startswith("smoke:")
    assert world["deploy"] == 2  # the release, then the revert
    db = SessionLocal()
    try:
        agent = db.get(Agent, target_id)
        assert agent.spec["system_prompt"] == "old"
        assert agent.status == "active"
    finally:
        db.close()
    log = admin.get(f"/api/promotions/{promotion_id}/execution").json()["log"]
    assert any("reverted" in line["msg"] for line in log)


def test_a_failed_first_release_has_nothing_to_revert(admin, world):
    world["answer"] = ""
    promotion_id = seed_promotion(seed_bundle())
    execute(admin, promotion_id)
    assert promotion_row(promotion_id).status == "failed"
    assert world["deploy"] == 1  # a create has no previous spec to restore


def test_the_agent_dlc_policy_keys_round_trip_through_the_api(admin, world):
    """release_mode / calibration / the spend guard used to be dropped by the request
    model, so a workspace could not be switched to a gated release from the API."""
    put = admin.put(f"/api/release-policies/{TARGET['id']}", json={
        "release_mode": "gated",
        "calibration": {"period_days": 30, "kappa_floor": 0.7},
        "eval_cost_confirm_usd": 2.5,
        "eval_cost_max_usd": 20,
    })
    assert put.status_code == 200, put.text
    policy = put.json()["policy"]
    assert policy["release_mode"] == "gated"
    assert policy["calibration"] == {"period_days": 30, "kappa_floor": 0.7}
    assert policy["eval_cost_confirm_usd"] == 2.5 and policy["eval_cost_max_usd"] == 20
    bad = admin.put(f"/api/release-policies/{TARGET['id']}", json={"release_mode": "yolo"})
    assert bad.status_code == 422 and bad.json()["code"] == "release_policy.invalid"
