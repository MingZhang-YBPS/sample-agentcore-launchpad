"""`scripts/check_api_docs.py`: which classified routes an API reference mentions."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_api_docs.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_api_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cad = _load()


def test_normalize_drops_param_names_and_query():
    assert cad.normalize("/api/agents/{agent_id}?lang=en") == "/api/agents/{}"
    assert cad.normalize("/api/agents/{id}/") == "/api/agents/{}"


def test_table_rows_inline_mentions_and_suffix_cells():
    text = "\n".join([
        "| `GET` | `/api/feedback?verdict=up\\|down&limit=` | member | x |",
        "| `PATCH/DELETE` | `/api/departments/{department_id}` | admin | x |",
        "| `POST` | `/api/departments/{department_id}/archive` / `…/unarchive` | admin | x |",
        "Call `PUT /api/agents/{id}/watch` first, then POST /api/issues/sync.",
    ])
    found = cad.mentioned_routes(text)
    assert ("GET", "/api/feedback") in found
    assert {("PATCH", "/api/departments/{}"), ("DELETE", "/api/departments/{}")} <= found
    assert ("POST", "/api/departments/{}/unarchive") in found
    assert ("PUT", "/api/agents/{}/watch") in found
    assert ("POST", "/api/issues/sync") in found
    # a later cell of the row is not a path of the row
    assert ("GET", "/api/feedback?verdict=up") not in found


def test_missing_routes_keeps_the_policy_spelling():
    policy = {
        ("GET", "/api/agents/{agent_id}"): "member",
        ("DELETE", "/api/agents/{agent_id}"): "perm:agents.delete",
    }
    assert cad.missing_routes("| `GET` | `/api/agents/{id}` | member | x |", policy) == [
        ("DELETE", "/api/agents/{agent_id}"),
    ]


def test_the_english_reference_mentions_every_classified_route():
    import app.main  # noqa: F401 — routers append their route-policy blocks on import
    from app.core.route_policy import ROUTE_POLICY

    doc = Path(__file__).resolve().parents[2] / "docs" / "api.md"
    assert cad.missing_routes(doc.read_text(encoding="utf-8"), ROUTE_POLICY) == []
