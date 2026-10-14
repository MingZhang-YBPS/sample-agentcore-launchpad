"""Admin-only console user management (registered accounts + statistics).

Every route requires an administrator identity (`Depends(require_admin)`), which
resolves to the built-in admin, an admin-role registered account, or — when the
console gate is disabled entirely — the implicit local operator.

The built-in admin is listed too (`builtin`, id `builtin-admin`), so its password can
be changed here; it is config-driven and has no `users` row, so its role, status and
validity are fixed. Every write is journaled in `audit_events` (hub-global rows; the
changed field names, never a password).

Administrator management (design projects §7.3, §7.10) rides on the same routes: a
`PATCH {role}` that grants or revokes the admin role also journals the structure
event `admin.grant` / `admin.revoke`; nobody demotes themselves, and never the last
admin who can sign in. `POST /api/users/{id}/sessions/revoke` is the forced logout
(`users.session_epoch` + 1), and `GET /api/admins` lists the administrators — the
built-in one first. Paths are spelled out (no router prefix) because `/api/admins`
sits beside `/api/users`.

Projects (design projects §7.4–§7.6, §7.10): every account row carries its
`projects` (direct and group-sourced, with role and sources) and `groups`; the list
filters by `role`, `project` and `group`; `POST`/`PATCH` take `projects` /
`groups` (full replacement on PATCH, like `workspaces`), so an approval places a
pending account into projects in the same transaction; `GET /api/users/{id}/access`
is that account's `/api/me/access`; `POST /api/users/bulk/projects` puts several
accounts into one project. They write the same rows as `PUT /api/projects/{id}/members`.
"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, StrictBool, model_validator
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, get_db
from app.core.errors import AppError
from app.routers.auth import Identity, require_admin
from app.services import admin_credential
from app.services import projects as projects_service
from app.services import users as users_service
from app.services.audit import record_audit_event, record_structure_event

router = APIRouter(tags=["users"])


class ProjectMembershipIn(BaseModel):
    """One direct project membership: a built-in template, a project custom role,
    or `inherit` (default project only). `overrides` omitted keeps the row's own."""

    project_id: str = Field(min_length=1, max_length=32)
    role_key: str = Field(min_length=1, max_length=32)
    overrides: dict[str, StrictBool] | None = None


class GroupMembershipIn(BaseModel):
    """One project-group membership (built-in templates only)."""

    group_id: str = Field(min_length=1, max_length=32)
    role_key: str = Field(min_length=1, max_length=32)


def _memberships(items: list[BaseModel] | None) -> list[dict[str, Any]] | None:
    # exclude_unset: an item without `overrides` keeps the existing row's
    return None if items is None else [item.model_dump(exclude_unset=True) for item in items]


class UserPatch(BaseModel):
    """Partial admin update. Only the keys actually sent are applied, so
    `{"expires_at": null}` means "never expires" while omitting it keeps the
    current validity."""

    status: str | None = None
    role: str | None = None
    extend_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None
    password: str | None = Field(default=None, max_length=256)
    # {permission_key: bool}; unsent keys stay granted, null resets to
    # all-granted. Strict so "no"/"0" cannot coerce into a denial.
    permissions: dict[str, StrictBool] | None = None
    # Full replacement of the account's workspace grants (null clears them) —
    # the per-user shape. `PUT /api/workspaces/{id}/grants` is the workspace-side
    # bulk one; both write only `user_workspaces`.
    workspaces: list[str] | None = None
    # Full replacement of the direct project / group memberships (null clears them;
    # the default project's `inherit` rows keep following `workspaces`).
    projects: list[ProjectMembershipIn] | None = None
    groups: list[GroupMembershipIn] | None = None

    @model_validator(mode="after")
    def _require_one_field(self) -> "UserPatch":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class UserCreate(BaseModel):
    """An admin-created account, active immediately (no approval step).

    `password` omitted / null / "" ⇒ the platform generates one and returns it once.
    `workspaces` omitted ⇒ `default`. Expiry: `valid_days`, a future `expires_at`, or
    `never_expires`; none of them ⇒ the same default validity an approval starts.
    """

    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=160)
    password: str | None = Field(default=None, max_length=256)
    role: str = users_service.ROLE_MEMBER
    permissions: dict[str, StrictBool] | None = None
    workspaces: list[str] | None = None
    valid_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None
    never_expires: StrictBool = False
    projects: list[ProjectMembershipIn] | None = None
    groups: list[GroupMembershipIn] | None = None


class BulkProjectAdd(BaseModel):
    """Design §7.6: several accounts into one project with one role."""

    user_ids: list[str] = Field(min_length=1, max_length=users_service.BULK_LIMIT)
    project_id: str = Field(min_length=1, max_length=32)
    role_key: str = Field(min_length=1, max_length=32)
    overrides: dict[str, StrictBool] | None = None


def _audit(db: Session, actor: Identity, action: str, target: str) -> None:
    # hub-global, journaled under the hub's own workspace (audit rows must name one)
    record_audit_event(
        workspace_id=DEFAULT_WORKSPACE_ID, actor=actor.username, action=action, target=target,
        actor_user_id=actor.user_id,
    )


@router.post("/api/users", status_code=201)
def create_user(
    req: UserCreate,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_admin),
) -> dict[str, Any]:
    user, generated = users_service.create_user(
        db,
        username=req.username,
        email=req.email,
        password=req.password,
        role=req.role,
        permissions=req.permissions,
        workspaces=req.workspaces,
        valid_days=req.valid_days,
        expires_at=req.expires_at,
        never_expires=req.never_expires,
        created_by=identity.username,
        settings=get_settings(),
        projects=_memberships(req.projects),
        groups=_memberships(req.groups),
        actor=identity,
    )
    _audit(
        db,
        identity,
        "user.create",
        f"{user.username} ({user.id}) role={user.role} "
        f"password={'generated' if generated else 'provided'}",
    )
    payload = users_service.user_detail(db, user)
    if generated is not None:
        # shown once in the console; only the hash is persisted
        payload["generated_password"] = generated
    return payload


@router.get("/api/users")
def list_users(
    q: str | None = None,
    status: str = "all",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    role: str | None = Query(default=None, max_length=16),
    project: str | None = Query(default=None, max_length=32),
    group: str | None = Query(default=None, max_length=32),
    # the design's spellings (§7.10), same meaning
    project_id: str | None = Query(default=None, max_length=32),
    group_id: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    if status not in users_service.STATUS_FILTERS:
        raise AppError(
            "validation.invalid_request",
            f"status must be one of {', '.join(users_service.STATUS_FILTERS)}",
            status_code=422,
        )
    project_filter = (project or project_id or "").strip() or None
    group_filter = (group or group_id or "").strip() or None
    page = users_service.list_users(
        db, q=q, status=status, limit=limit, offset=offset,
        role=role or None, project_id=project_filter, group_id=group_filter,
    )
    settings = get_settings()
    builtin = None
    # the built-in admin holds no membership, so a project / group filter leaves it out
    pinned = role in (None, "", users_service.ROLE_ADMIN) and not (project_filter or group_filter)
    if pinned and users_service.builtin_admin_enabled(settings):
        # Outside `items`/`total` (it has no row to page over): the console pins it
        # first when it matches the same search and state filter.
        candidate = admin_credential.serialize_builtin(db, settings)
        needle = (q or "").strip().lower()
        if (not needle or needle in candidate["username"].lower()) and status in ("all", "active"):
            builtin = candidate
    return {
        "items": page.items,
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "builtin": builtin,
    }


@router.get("/api/users/stats")
def user_stats(
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    return users_service.compute_stats(db, get_settings())


@router.patch("/api/users/{user_id}")
def update_user(
    user_id: str,
    patch: UserPatch,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_admin),
) -> dict[str, Any]:
    sent = {key: getattr(patch, key) for key in patch.model_fields_set}
    if user_id == admin_credential.BUILTIN_ADMIN_ID:
        return _patch_builtin(db, sent, identity)
    user = users_service.get_user(db, user_id)
    was_admin = user.role == users_service.ROLE_ADMIN
    role_change = "role" in sent and (sent["role"] == users_service.ROLE_ADMIN) != was_admin
    if role_change and was_admin and identity.user_id == user.id:
        # the last-admin answer first: it is the stronger reason, and the one the
        # console's disabled button explains
        users_service.assert_not_last_admin(db, user, get_settings())
        raise AppError(
            "users.self_demote",
            "You cannot remove your own administrator role — ask another administrator",
            status_code=409,
        )
    if "projects" in sent:
        sent["projects"] = _memberships(patch.projects)
    if "groups" in sent:
        sent["groups"] = _memberships(patch.groups)
    generated = users_service.apply_patch(db, user, sent, get_settings(), actor=identity)
    fields = sorted(sent)
    if "password" in sent:
        fields = [f for f in fields if f != "password"] + [
            "password=generated" if generated else "password=set"
        ]
    if "workspaces" in sent:
        fields = [f for f in fields if f != "workspaces"] + [
            f"workspaces={','.join(sorted(set(sent['workspaces'] or []))) or '-'}"
        ]
    for name, key in (("projects", "project_id"), ("groups", "group_id")):
        if name in sent:
            listed = ",".join(f"{i[key]}:{i['role_key']}" for i in sent[name] or []) or "-"
            fields = [f for f in fields if f != name] + [f"{name}={listed}"]
    _audit(db, identity, "user.update", f"{user.username} ({user.id}) {' '.join(fields)}")
    if role_change:
        # the semantic event beside `user.update role=…`, so the audit filter finds
        # every grant and revocation of the admin role (design §7.3)
        record_structure_event(
            actor=identity.username,
            action="admin.revoke" if was_admin else "admin.grant",
            target=f"user:{user.username} ({user.id}) role={user.role}",
            actor_user_id=identity.user_id,
        )
    payload = users_service.user_detail(db, user)
    if generated is not None:
        # shown once in the console; only the hash is persisted
        payload["generated_password"] = generated
    return payload


@router.post("/api/users/bulk/projects")
def bulk_add_to_project(
    req: BulkProjectAdd,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Design §7.6: the user list's "add to project…" — every selected account gets a
    direct membership with `role_key` (an existing member's role is replaced). Each
    change is journaled `project.members.add` / `.update`; workspace grants are not
    touched. Administrators are reported under `skipped`."""
    result = users_service.bulk_add_to_project(
        db, identity, project_id=req.project_id, user_ids=req.user_ids,
        role_key=req.role_key, overrides=req.overrides,
    )
    db.commit()
    return result


@router.get("/api/users/{user_id}")
def get_user(
    user_id: str,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """The detail page's account: the list row's shape (grants, projects, groups)."""
    if user_id == admin_credential.BUILTIN_ADMIN_ID:
        settings = get_settings()
        if not users_service.builtin_admin_enabled(settings):
            raise AppError("users.not_found", "User not found", status_code=404)
        return admin_credential.serialize_builtin(db, settings)
    return users_service.user_detail(db, users_service.get_user(db, user_id))


@router.get("/api/users/{user_id}/access")
def user_access(
    user_id: str,
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """The account's effective access (design §7.10): reachable workspaces → projects →
    keys and their sources — `/api/me/access` as that account would see it. An
    administrator's is every project by bypass; `state` says whether it can sign in
    to use any of it."""
    settings = get_settings()
    if user_id == admin_credential.BUILTIN_ADMIN_ID:
        if not users_service.builtin_admin_enabled(settings):
            raise AppError("users.not_found", "User not found", status_code=404)
        who = SimpleNamespace(
            username=settings.auth_username, user_id=None, role=users_service.ROLE_ADMIN
        )
        return {**projects_service.access_view(db, who), "state": users_service.STATUS_ACTIVE}
    user = users_service.get_user(db, user_id)
    who = SimpleNamespace(username=user.username, user_id=user.id, role=user.role)
    return {
        **projects_service.access_view(db, who),
        "state": users_service.account_state(user),
    }


def _patch_builtin(db: Session, sent: dict[str, Any], identity: Identity) -> dict[str, Any]:
    """The built-in admin: only its password can change (role/status/validity are
    fixed by design, so it can neither be demoted nor locked out)."""
    settings = get_settings()
    if not users_service.builtin_admin_enabled(settings):
        raise AppError("users.not_found", "User not found", status_code=404)
    immutable = sorted(set(sent) - {"password"})
    if immutable:
        raise AppError(
            "users.builtin_immutable",
            "The built-in administrator's role, status, validity, permissions and "
            f"workspaces are fixed; only its password can change ({', '.join(immutable)})",
            {"fields": immutable},
            status_code=400,
        )
    chosen = sent["password"]
    password = chosen or users_service.generate_password()
    admin_credential.set_password(db, password, identity.username)
    db.commit()
    _audit(
        db,
        identity,
        "admin.password_change",
        f"{settings.auth_username} (builtin) by user management "
        f"password={'set' if chosen else 'generated'}",
    )
    payload = admin_credential.serialize_builtin(db, settings)
    if not chosen:
        payload["generated_password"] = password
    return payload


@router.delete("/api/users/{user_id}")
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_admin),
) -> dict[str, bool]:
    if user_id == admin_credential.BUILTIN_ADMIN_ID:
        raise AppError(
            "users.builtin_immutable",
            "The built-in administrator cannot be deleted",
            status_code=400,
        )
    user = users_service.get_user(db, user_id)
    target = f"{user.username} ({user.id})"
    users_service.delete_user(db, user, get_settings())
    _audit(db, identity, "user.delete", target)
    return {"ok": True}


@router.post("/api/users/{user_id}/sessions/revoke")
def revoke_sessions(
    user_id: str,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """Forced logout: every session the account holds ends on its next request.

    Cookies are stateless, so this bumps `users.session_epoch`, which is part of the
    stamp every cookie carries. The built-in admin has no row (its sessions end when
    its password changes)."""
    if user_id == admin_credential.BUILTIN_ADMIN_ID:
        raise AppError(
            "users.builtin_immutable",
            "The built-in administrator's sessions end when its password changes",
            status_code=400,
        )
    user = users_service.get_user(db, user_id)
    epoch = users_service.revoke_sessions(db, user)
    record_structure_event(
        actor=identity.username,
        action="user.sessions.revoke",
        target=f"user:{user.username} ({user.id}) epoch={epoch}",
        actor_user_id=identity.user_id,
    )
    return {"ok": True, "user_id": user.id, "session_epoch": epoch}


@router.get("/api/admins")
def list_admins(
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_admin),
) -> dict[str, Any]:
    """The administrator page's list (design §7.3): the built-in admin first (while
    the login gate is on), then every admin-role account. `can_demote` is false for
    the built-in admin, the caller themselves and the last admin who can sign in —
    the cases `PATCH /api/users/{id}` refuses."""
    settings = get_settings()
    items: list[dict[str, Any]] = []
    if users_service.builtin_admin_enabled(settings):
        builtin = admin_credential.serialize_builtin(db, settings)
        items.append({**builtin, "can_sign_in": True, "can_demote": False})
    for row in users_service.list_admins(db, settings):
        row["can_demote"] = row["id"] != identity.user_id and not row["last_signable"]
        items.append(row)
    return {
        "items": items,
        "total": len(items),
        "can_sign_in": users_service.admins_who_can_sign_in(db, settings),
    }
