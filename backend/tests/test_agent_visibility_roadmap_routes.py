"""Workspace isolation reaches the roadmap / Agent-DLC agent routes too.

Upstream's isolation (`services.agent_access`) covers the agent routes it knew about;
the roadmap line adds more `/api/agents/{agent_id}/...` surfaces (share / review /
channel links, curated answer rules, release bundles, the Agent-DLC release gate and
watch, spec snapshots, suggested questions) and `/api/chat/{agent_id}/feedback`.
Under `isolated` a member must not reach another member's agent through any of
them: each answers the same 404 `agent.not_found` as a missing agent. The Agent-DLC
run routes (`/api/eval/runs/{run_id}/criteria...`, `/api/eval/runs/compare`) hide
another member's eval run the same way (`run.not_found`).
"""

from app.evaluation.models import EvalRun
from tests import test_agent_visibility as tav
from tests import test_visibility as tv
from tests.test_visibility import _add, _isolate, _refused

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob
world, no_real_deploy = tav.world, tav.no_real_deploy

ROUTES = (
    ("GET", "/api/agents/{id}/share-links"),
    ("POST", "/api/agents/{id}/share-links", {"label": "x"}),
    ("GET", "/api/agents/{id}/review-links"),
    ("POST", "/api/agents/{id}/channel-links", {"platform": "slack"}),
    ("GET", "/api/agents/{id}/rules"),
    ("GET", "/api/agents/{id}/release-bundles"),
    ("GET", "/api/agents/{id}/release"),
    ("GET", "/api/agents/{id}/release/gate"),
    ("GET", "/api/agents/{id}/watch"),
    ("GET", "/api/agents/{id}/scorecard"),
    ("GET", "/api/agents/{id}/snapshots"),
    ("GET", "/api/agents/{id}/suggested-questions"),
    ("POST", "/api/chat/{id}/feedback", {"session_id": "s", "message_id": 1, "verdict": "up"}),
)


def _calls(agent_id: str) -> list[tuple]:
    return [(m, url.format(id=agent_id), *body) for m, url, *body in ROUTES]


def test_a_hidden_agent_reads_as_missing_on_every_roadmap_route(world):
    _isolate()
    _refused(world.ada, "agent.not_found", *_calls(world.bob_agent))


def test_the_guard_leaves_visible_agents_and_shared_workspaces_alone(world):
    # shared (the default): ada reaches bob's agent — whatever the handler answers,
    # it is not the isolation 404
    for method, url, *body in _calls(world.bob_agent)[:1]:
        res = world.ada.request(method, url, json=body[0] if body else None)
        assert res.status_code == 200, res.text
    _isolate()
    # isolated: her own agent and a creator-unknown one stay reachable
    for agent_id in (world.ada_agent, world.legacy_agent):
        res = world.ada.get(f"/api/agents/{agent_id}/share-links")
        assert res.status_code == 200, res.text
    # an admin sees everything
    assert world.admin.get(f"/api/agents/{world.bob_agent}/share-links").status_code == 200


def test_a_hidden_run_reads_as_missing_on_the_dlc_run_routes(world):
    def run(by: str | None) -> str:
        return _add(EvalRun(agent_id=world.bob_agent, agent_name="bob-agent",
                            status="completed", created_by=by))

    bobs, adas = run(world.bob_id), run(world.ada_id)
    assert world.ada.get(f"/api/eval/runs/{bobs}/criteria").status_code == 200
    _isolate()
    _refused(
        world.ada, "run.not_found",
        ("GET", f"/api/eval/runs/{bobs}/criteria"),
        ("POST", f"/api/eval/runs/{bobs}/criteria/snapshot"),
        ("GET", f"/api/eval/runs/compare?runs={adas},{bobs}"),
    )
    assert world.ada.get(f"/api/eval/runs/{adas}/criteria").status_code == 200
