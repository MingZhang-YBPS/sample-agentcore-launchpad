"""Who is acting, by immutable account id (design §3.6, §5.8, §5.10).

The console names an actor by username everywhere (`requested_by`, `signed_by`,
`audit_events.actor`…). A username can be re-registered after its account is
deleted; a `users.id` cannot. So the writers that record a person also record the
id beside the name, and the separation-of-duties checks compare ids when both sides
carry one, else the names exactly as before.

The id comes from the request: `auth_middleware` sets the caller once per request,
and `user_id_for(actor)` answers only when the actor being recorded IS that caller
(the same username) — a worker thread, a system actor, or a name passed through from
some other row gets None rather than the wrong person's id. The built-in admin has
no `users` row, so its id is always None.
"""

from contextvars import ContextVar

_CURRENT_ACTOR: ContextVar[tuple[str, str | None] | None] = ContextVar(
    "launchpad_current_actor", default=None
)


def set_current_actor(username: str | None, user_id: str | None) -> None:
    """Set for the rest of the current context (one request's task)."""
    _CURRENT_ACTOR.set((username, user_id) if username else None)


def user_id_for(actor: str | None, explicit: str | None = None) -> str | None:
    """The `users.id` to record beside `actor`: `explicit` when given, else the
    request caller's when `actor` is the caller's username, else None."""
    if explicit:
        return explicit
    current = _CURRENT_ACTOR.get()
    if not actor or current is None or current[0] != actor:
        return None
    return current[1]


def same_actor(
    name_a: str | None, user_id_a: str | None, name_b: str | None, user_id_b: str | None
) -> bool:
    """True when two recorded actors are the same person (the SoD comparison).

    Both ids known → the ids decide (a re-registered username is someone else).
    Otherwise the names, as before the ids existed; an empty name matches nothing.
    """
    if user_id_a and user_id_b:
        return user_id_a == user_id_b
    return bool(name_a) and name_a == name_b
