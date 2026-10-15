"""The structure-only platform administrator — "steward" (结构管理员), projects §11.4.

A steward manages what an administrator manages *about the platform's shape* —
accounts, departments, projects, groups, bindings, workspaces — and never its data:
``access.has_data_bypass`` is False for it, ``access.can_manage_structure`` True, it
holds no grant or membership of its own, and ``core/route_policy.py`` refuses it every
``ADMIN_DATA`` route.

That is not cryptographic: whoever creates accounts and adds members could make a
puppet account, add it to a project and sign in as it. So a steward's reach into a
project *leaves a trace* instead:

1. it cannot add itself to a project (``services.projects._refuse_self``), nor any
   steward account (a steward is never a member);
2. every membership change it makes is journaled (``steward.members.change``, naming
   the project) and noticed to the project's owners — the accounts holding
   ``project.members`` there — through ``GET /api/steward/notices``;
3. with ``platform_settings.steward_two_person = true`` its membership changes are
   *pending* until another steward, an administrator or one of the project's owners
   confirms them (``POST /api/steward/pending/{id}/confirm``).

Notices and pending changes are small JSON documents in ``platform_settings`` rows
(``steward.notice.<id>`` / ``steward.pending.<id>``): no new table, hub-global like
the rest of structure. A confirmed or rejected change drops its row; the journal keeps
the history.
"""

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import DEFAULT_WORKSPACE_ID
from app.core.errors import AppError, NotFoundError
from app.models.ledger import User
from app.models.platform_settings import PlatformSetting
from app.services.audit import record_audit_event
from app.services.users import ROLE_ADMIN, ROLE_STEWARD, STATUS_ACTIVE

TWO_PERSON_SETTING = "steward_two_person"
NOTICE_PREFIX = "steward.notice."
PENDING_PREFIX = "steward.pending."
NOTICE_LIMIT = 50


def is_steward(identity: Any) -> bool:
    return getattr(identity, "role", None) == ROLE_STEWARD


def _now() -> str:
    return datetime.now(UTC).isoformat()


def journal(db: Session, actor: Any, action: str, target: str) -> None:
    """A steward event: hub-global like the structure journal (no project, filed
    under the hub's workspace), `target` naming `project:<id>` so the project's audit
    view finds it. Joins the caller's transaction."""
    record_audit_event(
        workspace_id=DEFAULT_WORKSPACE_ID, actor=actor.username, action=action, target=target,
        db=db, actor_user_id=getattr(actor, "user_id", None), project_id=None,
    )


# ── the two-person setting ────────────────────────────────────────────────────


def two_person(db: Session) -> bool:
    row = db.get(PlatformSetting, TWO_PERSON_SETTING)
    return bool(row and row.value.strip().lower() == "true")


def set_two_person(db: Session, enabled: bool, actor: Any) -> bool:
    row = db.get(PlatformSetting, TWO_PERSON_SETTING)
    if row is None:
        row = PlatformSetting(key=TWO_PERSON_SETTING)
        db.add(row)
    row.value = "true" if enabled else "false"
    row.updated_by = actor.username[:64]
    row.updated_at = datetime.now(UTC)
    journal(db, actor, "steward.two_person", f"steward_two_person={row.value}")
    return enabled


def settings_view(db: Session) -> dict[str, Any]:
    return {"steward_two_person": two_person(db)}


# ── documents in platform_settings ────────────────────────────────────────────


def _rows(db: Session, prefix: str) -> list[tuple[PlatformSetting, dict[str, Any]]]:
    out = []
    for row in db.scalars(
        select(PlatformSetting).where(PlatformSetting.key.like(f"{prefix}%"))
    ):
        try:
            doc = json.loads(row.value or "{}")
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append((row, doc))
    out.sort(key=lambda item: str(item[1].get("created_at") or ""), reverse=True)
    return out


def _put(db: Session, prefix: str, doc: dict[str, Any], actor: Any) -> None:
    db.add(PlatformSetting(
        key=f"{prefix}{doc['id']}", value=json.dumps(doc, sort_keys=True),
        updated_by=actor.username[:64], updated_at=datetime.now(UTC),
    ))


# ── the project's owners ──────────────────────────────────────────────────────


def owner_ids(db: Session, project: Any) -> list[str]:
    """Active accounts holding `project.members` in the project (direct or through
    its group) — the people a steward's change is noticed to and who may confirm it."""
    from app.services import projects as projects_service

    return sorted(
        m["user_id"]
        for m in projects_service.members_view(db, project, full_email=False)
        if "project.members" in m["permissions"] and m["status"] == STATUS_ACTIVE
        and m["account_role"] not in (ROLE_ADMIN, ROLE_STEWARD)
    )


def _usernames(db: Session, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    return {u.id: u.username for u in db.scalars(select(User).where(User.id.in_(sorted(ids))))}


def _changes(db: Session, upsert: list[dict[str, Any]], remove: list[str]) -> list[dict[str, Any]]:
    names = _usernames(db, {u["user_id"] for u in upsert} | set(remove))
    return [
        *({"op": "upsert", "user_id": u["user_id"], "username": names.get(u["user_id"]),
           "role_key": u["role_key"]} for u in upsert),
        *({"op": "remove", "user_id": uid, "username": names.get(uid)} for uid in remove),
    ]


def _describe(changes: list[dict[str, Any]]) -> str:
    return " ".join(
        f"{c['op']}:{c.get('username') or c['user_id']}"
        + (f":{c['role_key']}" if c.get("role_key") else "")
        for c in changes
    )


# ── notices ───────────────────────────────────────────────────────────────────


def notify_owners(
    db: Session, actor: Any, project: Any, kind: str, changes: list[dict[str, Any]],
    *, pending_id: str | None = None,
) -> str | None:
    """Leave the project's owners a notice of a steward's change (`kind`: `changed`,
    `pending`). None when the project has no owner to tell (the journal still has it)."""
    recipients = [uid for uid in owner_ids(db, project) if uid != getattr(actor, "user_id", None)]
    if not recipients:
        return None
    doc = {
        "id": uuid.uuid4().hex, "kind": kind, "project_id": project.id,
        "project_name": project.name, "actor": actor.username,
        "actor_user_id": getattr(actor, "user_id", None), "changes": changes,
        "pending_id": pending_id, "recipients": recipients, "read_by": [],
        "created_at": _now(),
    }
    _put(db, NOTICE_PREFIX, doc, actor)
    return doc["id"]


def notices_for(db: Session, identity: Any) -> dict[str, Any]:
    me = getattr(identity, "user_id", None)
    items = []
    for _, doc in _rows(db, NOTICE_PREFIX):
        if me and me in (doc.get("recipients") or []):
            view = {k: v for k, v in doc.items() if k not in ("recipients", "read_by")}
            view["read"] = me in (doc.get("read_by") or [])
            items.append(view)
    return {
        "items": items[:NOTICE_LIMIT],
        "unread": sum(1 for i in items if not i["read"]),
    }


def acknowledge(db: Session, identity: Any, notice_id: str) -> None:
    me = getattr(identity, "user_id", None)
    row = db.get(PlatformSetting, f"{NOTICE_PREFIX}{notice_id}")
    doc = json.loads(row.value) if row is not None else {}
    if not me or me not in (doc.get("recipients") or []):
        raise NotFoundError("steward.notice_not_found", f"notice '{notice_id}' not found",
                            {"notice_id": notice_id})
    if me not in doc["read_by"]:
        doc["read_by"].append(me)
        assert row is not None
        row.value = json.dumps(doc, sort_keys=True)


# ── the change itself ─────────────────────────────────────────────────────────


def after_member_change(
    db: Session, actor: Any, project: Any, upsert: list[dict[str, Any]],
    result: dict[str, list[str]], *, confirmed_by: Any = None,
) -> None:
    """A steward's applied membership change: journal + the owners' notice."""
    changed = set(result["added"]) | set(result["updated"])
    applied = [u for u in upsert if u["user_id"] in changed]
    changes = _changes(db, applied, result["removed"])
    if not changes:
        return
    target = f"project:{project.id} {_describe(changes)}"
    if confirmed_by is not None:
        target += f" confirmed_by:{confirmed_by.username}"
    journal(db, actor, "steward.members.change", target)
    notify_owners(db, actor, project, "changed", changes)


def request_change(
    db: Session, actor: Any, project: Any, upsert: list[dict[str, Any]], remove: list[str],
) -> str:
    """Two-person rule: stage a steward's membership change instead of applying it."""
    doc = {
        "id": uuid.uuid4().hex, "project_id": project.id, "project_name": project.name,
        "upsert": upsert, "remove": remove, "changes": _changes(db, upsert, remove),
        "requested_by": actor.username, "requested_by_user_id": getattr(actor, "user_id", None),
        "created_at": _now(),
    }
    _put(db, PENDING_PREFIX, doc, actor)
    journal(db, actor, "steward.pending.request",
            f"project:{project.id} pending:{doc['id']} {_describe(doc['changes'])}")
    notify_owners(db, actor, project, "pending", doc["changes"], pending_id=doc["id"])
    return doc["id"]


def _may_confirm(db: Session, identity: Any, doc: dict[str, Any]) -> bool:
    """Another steward, an administrator, or one of the project's owners — never the
    steward who asked."""
    from app.services import access

    me = getattr(identity, "user_id", None)
    if me is not None and me == doc.get("requested_by_user_id"):
        return False
    if access.has_data_bypass(identity) or is_steward(identity):
        return True
    membership = access.snapshot_for(db, identity).memberships.get(doc["project_id"])
    return membership is not None and "project.members" in membership.permissions


def pending_for(db: Session, identity: Any) -> dict[str, Any]:
    """The pending changes the caller may confirm, and the ones the caller asked for."""
    me = getattr(identity, "user_id", None)
    items = []
    for _, doc in _rows(db, PENDING_PREFIX):
        mine = me is not None and me == doc.get("requested_by_user_id")
        can_confirm = _may_confirm(db, identity, doc)
        if mine or can_confirm:
            view = {k: v for k, v in doc.items() if k not in ("upsert", "remove")}
            items.append({**view, "mine": mine, "can_confirm": can_confirm})
    return {"items": items}


def _pending(db: Session, pending_id: str) -> tuple[PlatformSetting, dict[str, Any]]:
    row = db.get(PlatformSetting, f"{PENDING_PREFIX}{pending_id}")
    if row is None:
        raise NotFoundError("steward.pending_not_found",
                            f"pending change '{pending_id}' not found", {"pending_id": pending_id})
    return row, json.loads(row.value)


def confirm(db: Session, identity: Any, pending_id: str) -> dict[str, Any]:
    """Apply a pending change as its requester did, noting who confirmed it."""
    from app.services import access
    from app.services import projects as projects_service

    row, doc = _pending(db, pending_id)
    if not _may_confirm(db, identity, doc):
        if getattr(identity, "user_id", None) == doc.get("requested_by_user_id"):
            raise AppError("steward.confirm_self",
                           "another steward or the project's owner confirms this change",
                           {"pending_id": pending_id}, status_code=403)
        raise NotFoundError("steward.pending_not_found",
                            f"pending change '{pending_id}' not found", {"pending_id": pending_id})
    me = getattr(identity, "user_id", None)
    targets = {u["user_id"] for u in doc["upsert"]} | set(doc["remove"])
    if me is not None and me in targets and not access.has_data_bypass(identity):
        raise AppError("project.self_membership", "you cannot confirm a change to your own "
                       "membership", {"user_id": me}, status_code=403)
    project = projects_service.get_project(db, doc["project_id"])
    requester = SimpleNamespace(
        username=doc["requested_by"], user_id=doc.get("requested_by_user_id"), role=ROLE_STEWARD,
    )
    db.delete(row)
    result = projects_service.upsert_members(
        db, requester, project, doc["upsert"], doc["remove"], check_self=False,
        confirmed_by=identity,
    )
    journal(db, identity, "steward.pending.confirm",
            f"project:{project.id} pending:{pending_id} requested_by:{doc['requested_by']}")
    return {"pending_id": pending_id, "project_id": project.id, **result}


def reject(db: Session, identity: Any, pending_id: str) -> dict[str, Any]:
    """Drop a pending change: its requester withdraws it, or a confirmer refuses it."""
    row, doc = _pending(db, pending_id)
    mine = getattr(identity, "user_id", None) == doc.get("requested_by_user_id")
    if not mine and not _may_confirm(db, identity, doc):
        raise NotFoundError("steward.pending_not_found",
                            f"pending change '{pending_id}' not found", {"pending_id": pending_id})
    db.delete(row)
    journal(db, identity, "steward.pending.withdraw" if mine else "steward.pending.reject",
            f"project:{doc['project_id']} pending:{pending_id} requested_by:{doc['requested_by']}")
    return {"pending_id": pending_id, "project_id": doc["project_id"], "rejected": True}
