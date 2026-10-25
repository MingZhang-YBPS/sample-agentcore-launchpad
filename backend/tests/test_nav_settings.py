"""The rail modules an administrator hid (services/nav_settings, routers/nav_settings).

Authorization (member reads, administrator writes, a member's PUT 403s) is the route
table's and asserted by tests/test_route_policy.py; this file covers the setting.
"""

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.ledger import AuditEvent
from app.models.platform_settings import PlatformSetting
from app.services import nav_settings


def _audit_rows() -> list[AuditEvent]:
    with SessionLocal() as db:
        return list(db.scalars(select(AuditEvent).where(
            AuditEvent.action == "console.nav_hidden")))


def test_nothing_is_hidden_until_an_administrator_says_so(client):
    body = client.get("/api/nav/hidden-modules").json()
    assert body == {"hidden": [], "updated_by": "", "updated_at": None}


def test_a_put_stores_a_sorted_deduplicated_list_and_reads_back(client):
    put = client.put("/api/nav/hidden-modules", json={"hidden": [
        "/v2/skill-lab", "/v2/eval/experiments", "/v2/skill-lab", "/v3/costs?view=project",
    ]})
    assert put.status_code == 200, put.text
    want = ["/v2/eval/experiments", "/v2/skill-lab", "/v3/costs?view=project"]
    assert put.json()["hidden"] == want
    assert client.get("/api/nav/hidden-modules").json()["hidden"] == want


def test_an_empty_list_shows_everything_again(client):
    client.put("/api/nav/hidden-modules", json={"hidden": ["/v2/skill-lab"]})
    assert client.put("/api/nav/hidden-modules", json={"hidden": []}).json()["hidden"] == []


def test_a_change_is_journaled_once_and_a_no_op_is_not(client):
    client.put("/api/nav/hidden-modules", json={"hidden": ["/v2/skill-lab"]})
    client.put("/api/nav/hidden-modules", json={"hidden": ["/v2/skill-lab"]})
    rows = _audit_rows()
    assert len(rows) == 1
    assert "hidden+/v2/skill-lab" in rows[0].target
    client.put("/api/nav/hidden-modules", json={"hidden": []})
    assert any("shown+/v2/skill-lab" in r.target for r in _audit_rows())


def test_a_path_that_is_not_a_rail_entry_is_refused(client):
    for bad in ["https://evil.example/", "/api/users", "javascript:alert(1)", "/v2/<b>"]:
        response = client.put("/api/nav/hidden-modules", json={"hidden": [bad]})
        assert response.status_code == 422, bad
    assert client.get("/api/nav/hidden-modules").json()["hidden"] == []


def test_too_many_entries_are_refused(client):
    paths = [f"/v2/m{i}" for i in range(nav_settings.MAX_ENTRIES + 1)]
    assert client.put("/api/nav/hidden-modules", json={"hidden": paths}).status_code == 422


def test_an_unreadable_stored_value_reads_as_nothing_hidden():
    with SessionLocal() as db:
        db.add(PlatformSetting(key=nav_settings.HIDDEN_MODULES_SETTING, value="{not json"))
        db.commit()
        assert nav_settings.hidden_modules(db) == []
        row = db.get(PlatformSetting, nav_settings.HIDDEN_MODULES_SETTING)
        row.value = '["/v2/skill-lab", 7, "https://x"]'
        db.commit()
        assert nav_settings.hidden_modules(db) == ["/v2/skill-lab"]
