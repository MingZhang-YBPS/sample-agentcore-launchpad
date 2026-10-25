"""The immutable principal an assistant conversation belongs to.

Usernames are display names: an account can be deleted and the same username
registered again under a new ``users.id``. Ownership therefore binds to
``user:<users.id>`` for registered accounts, to the explicit stable
``config-admin`` for the built-in (row-less) administrator, and to
``local-operator`` when the login gate is off. A stored NULL principal (legacy row)
matches nobody.

Who may *work on* a conversation is wider than who owns it: while an admin shares
it, every principal collaborates (``may_collaborate`` / ``collaborator_clause``).
Who may *read* it is wider still: an administrator sees every conversation of the
project, read-only unless it is shared (``may_view`` / ``viewer_clause``).

Conversations are project-owned (design §5.5): "shared" opens one to every member of
its project, and an administrator's list is the selected project's. What a caller
may *do* is the project's effective keys (``project_can``), not the account's.
"""

from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.project_scope import current_project, project_context
from app.models.assistant import AssistantConversation
from app.routers.auth import Identity
from app.routers.auth import enabled as auth_enabled

CONFIG_ADMIN = "config-admin"
LOCAL_OPERATOR = "local-operator"

_CURRENT: Any = object()


def principal_of(identity: Identity) -> str:
    if not auth_enabled():
        return LOCAL_OPERATOR
    if identity.user_id:
        return f"user:{identity.user_id}"
    return CONFIG_ADMIN


def may_collaborate(row: AssistantConversation | None, principal: str) -> bool:
    """The owner, or anyone while an admin shares the conversation. A legacy row with
    no principal is closed to everybody, shared or not."""
    return (
        row is not None
        and row.owner_principal is not None
        and (row.owner_principal == principal or bool(row.shared))
    )


def collaborator_clause(principal: str) -> Any:
    """``may_collaborate`` as a SQL predicate on ``assistant_conversations``, for the
    conditional writes that must re-check it inside the statement itself."""
    return and_(
        AssistantConversation.owner_principal.is_not(None),
        or_(AssistantConversation.owner_principal == principal,
            AssistantConversation.shared.is_(True)),
    )


def may_view(row: AssistantConversation | None, principal: str, *, is_admin: bool) -> bool:
    """A collaborator, or — read-only — an administrator on any conversation that has
    an owner. A legacy row with no principal stays closed to everybody."""
    if may_collaborate(row, principal):
        return True
    return is_admin and row is not None and row.owner_principal is not None


def viewer_clause(principal: str, *, is_admin: bool) -> Any:
    """``may_view`` as a SQL predicate on ``assistant_conversations``."""
    if is_admin:
        return AssistantConversation.owner_principal.is_not(None)
    return collaborator_clause(principal)


def project_can(
    db: Session, who: Any, workspace_id: str, key: str, project_id: Any = _CURRENT,
) -> bool:
    """Whether ``who`` (an ``Identity``, or anything carrying ``user_id`` and ``role``)
    holds ``key`` in ``project_id`` — by default the current project — re-resolved
    from the database (design §3.4), so a membership or role change is honoured. With
    no project (an unscoped caller, a row older than projects) the account's keys
    decide, as before."""
    from app.services import access

    pid = current_project() if project_id is _CURRENT else project_id
    if pid is None:
        return bool(who.can(key))
    with project_context(None):  # access tables are not project-owned anyway
        try:
            scope = access.resolve_project(db, who, workspace_id, pid, "GET")
        except AppError:  # no longer a member, project unbound…
            return False
    return scope.can(key)
