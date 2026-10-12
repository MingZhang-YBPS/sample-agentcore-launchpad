"""Replayed evaluation sessions must not share a long-term memory actor.

Live: every replay invoked the runtime with the bare "default" actor, so with
long-term memory on a fact from one golden item (a 轻享 fare of ¥680) was recalled in
another item that deliberately withholds the fare, and the answer quoted ¥680.
"""
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from tests.conftest import ws_ctx


def _run(monkeypatch, items, repeats=1, method="zip_runtime", protocol="http"):
    """Replay ``items``; ``calls`` holds (prompt, session id in, actor) per invoke —
    the actor an A2A runtime ends up on is the one its template derives."""
    from app.evaluation import service as svc
    from app.evaluation.models import EvalRun
    from tests.evaluation.test_runs_flow import stub_environment

    stub_environment(monkeypatch)
    calls: list[tuple[str, str | None, str]] = []

    def fake_invoke(client, arn, prompt, session_id=None, actor_id="default", **kwargs):
        calls.append((prompt, session_id, actor_id))
        return {"text": "ok", "session_id": session_id or f"s{len(calls):032d}"}

    def fake_a2a(client, arn, prompt, session_id=None, **kwargs):
        # the platform A2A template: actor_id=f"{AGENT_NAME}__a2a__{context_id}",
        # and the console sends the session id as the context id
        assert session_id, "an A2A replay must name its session"
        calls.append((prompt, session_id, f"mem-agent__a2a__{session_id}"))
        return {"text": "ok", "session_id": session_id}

    monkeypatch.setattr(svc.rt, "invoke_runtime_text", fake_invoke)
    monkeypatch.setattr(svc.hc, "invoke_harness_text", fake_invoke)
    monkeypatch.setattr(svc.rt, "invoke_a2a_text", fake_a2a)
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
        rid, workspace=ws_ctx(), agent_arn="arn:rt", method=method, protocol=protocol,
        service_name="svc.DEFAULT", log_group="/lg", items=items,
        evaluators=["Builtin.Correctness"], mode="evaluators", wait_seconds=0,
        agent_id="a" * 32, repeats=repeats, actor_model_id="m",
    )
    return rid, calls


def _attempts(rid):
    from app.evaluation.models import EvalRun

    db = SessionLocal()
    run = db.get(EvalRun, rid)
    db.close()
    return run


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


def test_the_run_records_each_sessions_actor_for_the_transcript_reader(monkeypatch):
    from app.evaluation.models import EvalRun
    from app.evaluation.service import eval_session_actors

    rid, calls = _run(monkeypatch, [{"prompt": "q1"}, {"prompt": "q2"}])
    db = SessionLocal()
    run = db.get(EvalRun, rid)
    db.close()
    assert [a["actor_id"] for a in run.attempts] == [actor for _, _, actor in calls]
    for entry in run.attempts:
        assert eval_session_actors(run, entry["session_id"])[0] == entry["actor_id"]
        assert eval_session_actors(run, entry["session_id"])[-1] == "default"


# ── managed Harness and A2A agents (2cfc398f covered only runtime agents) ─────
def test_harness_replays_pass_each_session_its_own_actor_and_record_it(monkeypatch):
    """Live: Harness replays still invoked InvokeHarness with actorId "default", so a
    session's long-term memory was recalled by the next session of the same run."""
    from app.evaluation.service import eval_actor, eval_session_actors

    rid, calls = _run(monkeypatch, [{"prompt": "fare is 680"}, {"prompt": "how much?"}],
                      method="harness")
    actors = [actor for _, _, actor in calls]
    assert actors == [eval_actor("a" * 32, rid, 1), eval_actor("a" * 32, rid, 2)]
    run = _attempts(rid)
    assert [a["actor_id"] for a in run.attempts] == actors
    for entry in run.attempts:
        assert eval_session_actors(run, entry["session_id"])[0] == entry["actor_id"]


def test_harness_turns_of_one_scenario_share_its_actor_and_repeats_do_not(monkeypatch):
    items = [{"scenario_id": "m-1", "turns": [{"input": "t1"}, {"input": "t2"}]}]
    _, calls = _run(monkeypatch, items, repeats=2, method="harness")
    first, second = {calls[0][2], calls[1][2]}, {calls[2][2], calls[3][2]}
    assert len(first) == 1 and len(second) == 1 and first != second
    assert "default" not in first | second


def test_a2a_replays_open_a_fresh_session_each_so_each_gets_its_own_actor(monkeypatch):
    """An A2A runtime cannot be handed an actor (the JSON-RPC body passes through and
    Strands' A2AServer gives the agent factory only the context id), so the template
    keys memory on the session. Each replayed scenario opens a fresh session and the
    run records the actor the template writes under."""
    from app.evaluation.service import a2a_session_actor, eval_session_actors

    items = [{"scenario_id": "m-1", "turns": [{"input": "t1"}, {"input": "t2"}]},
             {"prompt": "q2"}]
    rid, calls = _run(monkeypatch, items, method="zip_runtime", protocol="a2a")
    assert calls[0][1] == calls[1][1]  # one scenario, one session
    assert calls[1][2] == calls[0][2]
    assert calls[2][1] != calls[0][1] and calls[2][2] != calls[0][2]
    run = _attempts(rid)
    assert [a["actor_id"] for a in run.attempts] == [calls[0][2], calls[2][2]]
    for entry in run.attempts:
        assert entry["actor_id"] == a2a_session_actor("mem-agent", entry["session_id"])
        assert eval_session_actors(run, entry["session_id"], protocol="a2a")[0] == \
            entry["actor_id"]


def test_simulated_persona_turns_use_the_replays_per_session_actor(monkeypatch):
    """Persona scenarios drove harness/runtime calls with no actor at all; they now go
    through the replay's own invoke and so get the per-session actor."""
    from app.evaluation import service as svc
    from app.evaluation.service import eval_actor

    def fake_sim(_data, *, scenario, invoke_text, **_kw):
        sid = invoke_text("hi", None)["session_id"]
        return invoke_text("again", sid)["session_id"]

    monkeypatch.setattr(svc.simulation, "run_simulated_scenario", fake_sim)
    persona = {"scenario_id": "p-1", "scenario_description": "d", "input": "hi",
               "actor_profile": {"goal": "g", "context": "c", "traits": {}}}
    rid, calls = _run(monkeypatch, [persona, {**persona, "scenario_id": "p-2"}],
                      method="harness")
    actors = [actor for _, _, actor in calls]
    assert actors[0] == actors[1] == eval_actor("a" * 32, rid, 1)
    assert actors[2] == actors[3] == eval_actor("a" * 32, rid, 2)
    assert [a["actor_id"] for a in _attempts(rid).attempts] == [actors[0], actors[2]]
