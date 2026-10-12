"""Replayed evaluation sessions must not share a long-term memory actor.

Live: every replay invoked the runtime with the bare "default" actor, so with
long-term memory on a fact from one golden item (a 轻享 fare of ¥680) was recalled in
another item that deliberately withholds the fare, and the answer quoted ¥680.
"""
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from tests.conftest import ws_ctx


def _run(monkeypatch, items, repeats=1):
    from app.evaluation import service as svc
    from app.evaluation.models import EvalRun
    from tests.evaluation.test_runs_flow import stub_environment

    stub_environment(monkeypatch)
    calls: list[tuple[str, str | None, str]] = []

    def fake_invoke(client, arn, prompt, session_id=None, actor_id="default", **kwargs):
        calls.append((prompt, session_id, actor_id))
        return {"text": "ok", "session_id": session_id or f"s{len(calls):032d}"}

    monkeypatch.setattr(svc.rt, "invoke_runtime_text", fake_invoke)
    db = SessionLocal()
    run = EvalRun(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a" * 32, agent_name="mem-agent",
        mode="evaluators", evaluators=["Builtin.Correctness"], status="queued",
    )
    db.add(run)
    db.commit()
    rid = run.id
    db.close()
    svc.execute_run(
        rid, workspace=ws_ctx(), agent_arn="arn:rt", method="zip_runtime",
        service_name="svc.DEFAULT", log_group="/lg", items=items,
        evaluators=["Builtin.Correctness"], mode="evaluators", wait_seconds=0,
        agent_id="a" * 32, repeats=repeats,
    )
    return rid, calls


def test_each_replayed_session_gets_its_own_memory_actor(monkeypatch):
    rid, calls = _run(monkeypatch, [{"prompt": "fare is 680"}, {"prompt": "how much back?"}])
    actors = [actor for _, _, actor in calls]
    assert "default" not in actors
    assert len(set(actors)) == 2
    assert actors[0] == f"{'a' * 32}__eval-{rid}-1"


def test_turns_of_one_scenario_share_its_actor_and_repeats_do_not(monkeypatch):
    items = [{"scenario_id": "m-1", "turns": [{"input": "t1"}, {"input": "t2"}]}]
    _, calls = _run(monkeypatch, items, repeats=2)
    assert [p for p, _, _ in calls] == ["t1", "t2", "t1", "t2"]
    first, second = {calls[0][2], calls[1][2]}, {calls[2][2], calls[3][2]}
    assert len(first) == 1 and len(second) == 1
    assert first != second
