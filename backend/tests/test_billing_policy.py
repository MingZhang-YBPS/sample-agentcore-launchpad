"""The budget classification (`core/billing_policy.py`): every mutating route under
the billable prefixes has a row, every row names a live route, reads are never
billable, and the classification carries the operations the monthly budget gates."""

from app.core.billing_policy import (
    BILLABLE_PREFIXES,
    BILLING,
    CANARY_BILLABLE_ACTIONS,
    EXPERIMENT_BILLABLE_ACTIONS,
    Billable,
    billable_routes,
    is_billable_prefix,
)
from app.main import create_app
from tests.test_route_policy import _api_routes


def _live() -> set[tuple[str, str]]:
    return _api_routes(create_app())


def test_every_mutating_route_under_the_billable_prefixes_is_classified():
    missing = sorted(
        (method, path) for method, path in _live()
        if method != "GET" and is_billable_prefix(path) and (method, path) not in BILLING
    )
    assert not missing, (
        "classify these routes in BILLING (app/core/billing_policy.py) — Billable if "
        f"they start work AWS bills per use, FREE otherwise: {missing}")


def test_every_row_names_a_live_route_under_a_billable_prefix():
    live = _live()
    stale = sorted(key for key in BILLING if key not in live)
    assert not stale, f"BILLING rows with no route: {stale}"
    outside = sorted(key for key in BILLING if not is_billable_prefix(key[1]))
    assert not outside, f"rows outside BILLABLE_PREFIXES: {outside}"


def test_reads_are_never_billable():
    assert all(method != "GET" for method, _ in BILLING)
    assert all(p.startswith("/api/") for p in BILLABLE_PREFIXES)


def test_the_operations_that_spend_are_billable():
    billable = set(billable_routes())
    for key in [
        ("POST", "/api/agents"),
        ("POST", "/api/agents/{agent_id}/redeploy"),
        ("POST", "/api/agents/{agent_id}/invoke"),
        ("POST", "/api/chat/{agent_id}"),
        ("POST", "/api/eval/runs"),
        ("POST", "/api/eval/online"),
        ("POST", "/api/experiments/{exp_id}/action"),
        ("POST", "/api/runtime-canaries/{canary_id}/action"),
        ("POST", "/api/skill-lab/jobs"),
        ("POST", "/api/assistant/architect/conversations/{conversation_id}/turns"),
        ("POST", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"),
    ]:
        assert key in billable, key
    # a stop / cleanup that ends spend is never refused
    for key in [("POST", "/api/eval/runs/{run_id}/stop"),
                ("POST", "/api/chat/{agent_id}/sessions/{session_id}/stop"),
                ("POST", "/api/skill-lab/jobs/{job_id}/cancel")]:
        assert key not in billable, key
    assert "cleanup" not in EXPERIMENT_BILLABLE_ACTIONS | CANARY_BILLABLE_ACTIONS
    assert "verdict" not in EXPERIMENT_BILLABLE_ACTIONS | CANARY_BILLABLE_ACTIONS
    # the project of a create / convert is named in the body: the handler gates it
    assert BILLING[("POST", "/api/agents")] == Billable("deploy", in_handler=True)
