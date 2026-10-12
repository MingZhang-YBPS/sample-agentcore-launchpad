"""TTFA — Time to First Agent, per registered account, within one workspace.

The clock starts at the account's first successful login (`User.first_login_at`,
falling back to `created_at` for accounts that logged in before the column
existed) and stops at the end of the earliest **succeeded** `Deployment` of an
`Agent` in this workspace created by that account: `Agent.created_by_name` (the
creating identity's username, stamped server-side by `services.creators` on create,
convert and assistant approval), falling back to `Agent.owner` for rows created
before creators were recorded (the T-line stamped the username there). It is matched
case-insensitively against `User.username_key`. Agents deleted since still count —
TTFA is a historical fact.

The built-in admin has no `users` row and is never a sample. Pending accounts
(no session possible yet) are excluded; every other account that can reach the
workspace (admins, members granted it, or anyone who owns an agent in it) is
listed, with `ttfa_seconds = None` until they ship their first agent.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ledger import Agent, Deployment, User, UserWorkspace
from app.services.users import as_utc


def _iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


def compute_ttfa(db: Session, workspace_id: str) -> dict[str, Any]:
    # earliest succeeded deployment per lowercased owner, this workspace only
    firsts: dict[str, datetime] = {}
    rows = (
        db.query(
            func.lower(func.coalesce(Agent.created_by_name, Agent.owner)),
            Deployment.ended_at,
            Deployment.started_at,
        )
        .join(Deployment, Deployment.agent_id == Agent.id)
        .filter(Agent.workspace_id == workspace_id, Deployment.status == "succeeded")
        .all()
    )
    for owner, ended_at, started_at in rows:
        moment = as_utc(ended_at or started_at)
        if owner and moment and (owner not in firsts or moment < firsts[owner]):
            firsts[owner] = moment

    granted = {
        uid
        for (uid,) in db.query(UserWorkspace.user_id).filter(
            UserWorkspace.workspace_id == workspace_id
        )
    }
    users: list[dict[str, Any]] = []
    samples: list[float] = []
    for user in db.query(User).order_by(User.created_at).all():
        if user.status == "pending":
            continue
        first_agent = firsts.get(user.username_key)
        if not (user.role == "admin" or user.id in granted or first_agent):
            continue
        start = as_utc(user.first_login_at) or as_utc(user.created_at)
        ttfa: float | None = None
        if first_agent and start:
            # an agent owned before the account's first login (e.g. the admin
            # created it for them) counts as immediate, never negative
            ttfa = max(0.0, (first_agent - start).total_seconds())
            samples.append(ttfa)
        users.append(
            {
                "username": user.username,
                "ttfa_seconds": ttfa,
                "first_login_at": _iso(user.first_login_at),
                "first_agent_at": _iso(first_agent),
            }
        )
    return {
        "median_seconds": float(median(samples)) if samples else None,
        "samples": len(samples),
        "users": users,
    }
