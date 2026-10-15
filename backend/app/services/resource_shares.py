"""Cross-project sharing grants (projects design §11.2, G11-share).

The owner project's `project.share` holder lends one resource to another project bound
to the same workspace: `read` (see it) or `use` (its agents pin it, its evaluations run
on it). Within one department — or between two projects without one — the grant is
active at once; across departments it waits as `requested` until the grantee's
department administrator (an administrator when it has none) approves, unless the
granting caller is that approver already. A share expires (default 90 days, the
scheduler's `project.share_expiry` sweep), and its owner revokes it at any time; every
change bumps the access epoch in the same transaction, so the next request of the
grantee no longer sees the resource.

What a share opens, per kind (`load_received` turns grants into row ids):

* `eval_dataset` — the dataset row; for a golden set its three splits, and the
  criteria lineage it is scored against (read-only, like the set);
* `criteria_set` — every version of a template lineage and their criteria;
* `gateway_target` — the console Lambda target or adopted MCP target row;
* `knowledge_base` / `evaluator` — AWS-only kinds: their ownership row
  (`resource_creators`), and — `use` only — the pin checks (`agent_access.check_pins`)
  and the isolated project's boundary (`boundary_statements`, registered in
  `project_iam`). Lists of these kinds come from AWS and are filtered by
  `creators.outside_current_project`, which consults `received_ids`.

Never a write: the ORM criteria widen reads only, and a flush that changes a shared
row fails as a missing id (`project_scope`); routes that act on AWS before any flush ask
`refused_ids` first. Journal: `project.share.grant / request / approve / reject / revoke /
expire`, filed under the owner project with the grantee in the target.

Lists label what another project lent (`shared_from`: `{project_id, name, access}`). When
a share ends (`revoke`, the expiry sweep) the grantee's owners get a notice
(`share.notice.<id>` documents in `platform_settings`, like the steward's), and an isolated
grantee that used a shared KB gets a provisioning run once the change is committed, so its
boundary is rendered again without the KB (`project_isolation.queue_provision`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event, or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.core.project_scope import forget_shared_rows, project_context
from app.models.projects import Project, ProjectWorkspace
from app.models.shares import (
    ACCESS_LEVELS,
    ACCESS_USE,
    KIND_CRITERIA_SET,
    KIND_EVAL_DATASET,
    KIND_EVALUATOR,
    KIND_GATEWAY_TARGET,
    KIND_KNOWLEDGE_BASE,
    OPEN_STATUSES,
    SHARE_KINDS,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_REJECTED,
    STATUS_REQUESTED,
    STATUS_REVOKED,
    ResourceShare,
)
from app.services import access
from app.services.audit import record_audit_event

logger = logging.getLogger(__name__)

DEFAULT_EXPIRY_DAYS = 90  # §11.2 risk: shares expire by default
MAX_EXPIRY_DAYS = 365
_AWS_ONLY_KINDS = (KIND_KNOWLEDGE_BASE, KIND_EVALUATOR)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _actor(identity: Any) -> tuple[str, str | None]:
    return identity.username, getattr(identity, "user_id", None)


def _journal(db: Session, identity: Any | None, share: ResourceShare, action: str,
             extra: str = "") -> None:
    actor, actor_user_id = _actor(identity) if identity is not None else ("scheduler", None)
    target = f"{share.kind}:{share.resource_id} -> project:{share.grantee_project_id}"
    record_audit_event(
        workspace_id=share.workspace_id or "", actor=actor, actor_user_id=actor_user_id,
        action=f"project.share.{action}", target=f"{target}{extra}"[:256], db=db,
        project_id=share.owner_project_id,
    )


# ── what a project receives (the access snapshot's loader) ───────────────────


def load_received(db: Session, project_id: str) -> access.SharedResources:
    """The active, unexpired shares `project_id` receives and the row ids they open.

    Core statements on the session's connection: they run inside the ORM's
    `do_orm_execute` hook, so they must not re-enter it (and see every project)."""
    from app.evaluation.models import EvalDataset
    from app.models.dlc import CriteriaSet, Criterion
    from app.models.lambda_tools import ExternalMcpTarget, LambdaToolTarget
    from app.models.ledger import ResourceCreator

    now = _now()
    conn = db.connection()
    t = ResourceShare.__table__
    grants = conn.execute(
        select(t.c.kind, t.c.resource_id, t.c.workspace_id, t.c.owner_project_id,
               t.c.access, t.c.expires_at)
        .where(t.c.grantee_project_id == project_id, t.c.status == STATUS_ACTIVE,
               or_(t.c.expires_at.is_(None), t.c.expires_at > now))
    ).fetchall()
    if not grants:
        return access.SharedResources()
    by_kind: dict[str, dict[str, str]] = {}
    owners: dict[str, dict[str, str]] = {}
    rows: dict[str, set[str]] = {}
    valid_until: datetime | None = None

    def add(table: str, ids: Any) -> None:
        rows.setdefault(table, set()).update(i for (i,) in ids)

    for kind, rid, ws, owner, level, expires in grants:
        expires = _aware(expires)
        if expires is not None and (valid_until is None or expires < valid_until):
            valid_until = expires
        current = by_kind.setdefault(kind, {})
        if current.get(rid) != ACCESS_USE:  # two grants of one id: the wider one wins
            current[rid] = level
        owners.setdefault(kind, {})[rid] = owner
        if kind in _AWS_ONLY_KINDS:
            rc = ResourceCreator.__table__.c
            add("resource_creators", conn.execute(select(rc.id).where(
                rc.workspace_id == ws, rc.kind == kind, rc.resource_id == rid,
                rc.project_id == owner)))
        elif kind == KIND_EVAL_DATASET:
            dc = EvalDataset.__table__.c
            found = conn.execute(select(dc.id, dc.criteria_set_id).where(
                dc.workspace_id == ws, dc.project_id == owner,
                or_(dc.id == rid, dc.split_of == rid))).fetchall()
            add("eval_datasets", ((r[0],) for r in found))
            for lineage in {r[1] for r in found if r[1]}:
                _add_lineage(conn, rows, CriteriaSet, Criterion, ws, owner, lineage)
        elif kind == KIND_CRITERIA_SET:
            _add_lineage(conn, rows, CriteriaSet, Criterion, ws, owner, rid)
        elif kind == KIND_GATEWAY_TARGET:
            for model in (LambdaToolTarget, ExternalMcpTarget):
                c = model.__table__.c
                add(model.__tablename__, conn.execute(select(c.id).where(
                    c.workspace_id == ws, c.project_id == owner, c.id == rid)))
    return access.SharedResources(
        by_kind=by_kind, owners=owners,
        rows={table: frozenset(ids) for table, ids in rows.items() if ids},
        valid_until=valid_until,
    )


def _add_lineage(conn: Any, rows: dict[str, set[str]], set_model: Any, criterion_model: Any,
                 ws: str | None, owner: str, lineage: str) -> None:
    sc = set_model.__table__.c
    set_ids = [r[0] for r in conn.execute(select(sc.id).where(
        sc.workspace_id == ws, sc.project_id == owner, sc.lineage_id == lineage))]
    if not set_ids:
        return
    rows.setdefault("criteria_sets", set()).update(set_ids)
    cc = criterion_model.__table__.c
    rows.setdefault("criteria", set()).update(
        r[0] for r in conn.execute(select(cc.id).where(cc.set_id.in_(set_ids))))


def received_ids(db: Session, kind: str, *, use: bool = False,
                 project_id: str | None = None) -> frozenset[str]:
    """The resource ids of `kind` shared with the (current) project — `use` level only
    when `use`. Empty without a current project."""
    from app.core.project_scope import current_project

    pid = project_id or current_project()
    if not pid:
        return frozenset()
    return access.shared_for(db, pid).ids(kind, use=use)


def shared_from(db: Session, kind: str, ids: Any, *, project_id: str | None = None
                ) -> dict[str, dict[str, str]]:
    """`{resource_id: {project_id, name, access}}` for the ids of `kind` the (current)
    project receives from another one — the "from project X (read / use)" label of
    lists. Empty without a current project or when nothing is shared (no query)."""
    from app.core.project_scope import current_project

    pid = project_id or current_project()
    if not pid:
        return {}
    shared = access.shared_for(db, pid)
    granted = shared.by_kind.get(kind) or {}
    hits = {i for i in ids if i in granted}
    if not hits:
        return {}
    owners = shared.owners.get(kind) or {}
    names = project_names(db, {owners[i] for i in hits if i in owners})
    return {i: {"project_id": owners.get(i, ""),
                "name": names.get(owners.get(i, ""), owners.get(i, "")),
                "access": granted[i]} for i in hits}


def refused_ids(db: Session, kind: str, ids: Any, need: str, *,
                project_id: str | None = None) -> set[str]:
    """Of `ids`, those the (current) project reaches only through a share that does not
    allow `need`: `use` (an evaluation runs on it, an agent pins it) refuses read-only
    shares; `own` (any write, any AWS change) refuses every share. A project's own ids
    and ids it cannot see at all are not this function's to answer."""
    from app.core.project_scope import current_project

    pid = project_id or current_project()
    wanted = {i for i in ids if i}
    if not pid or not wanted:
        return set()
    shared = access.shared_for(db, pid)
    if need == "own":
        return wanted & shared.ids(kind)
    return wanted & (shared.ids(kind) - shared.ids(kind, use=True))


def revoked_ids(db: Session, kind: str, resource_ids: set[str], project_id: str) -> set[str]:
    """Of `resource_ids`, those a share once lent `project_id` that is no longer
    active (revoked or expired) — the "dependency revoked" set of §11.2."""
    if not resource_ids:
        return set()
    live = access.shared_for(db, project_id).ids(kind)
    with project_context(None):
        ended = {rid for (rid,) in db.execute(
            select(ResourceShare.resource_id).where(
                ResourceShare.kind == kind, ResourceShare.grantee_project_id == project_id,
                ResourceShare.resource_id.in_(sorted(resource_ids)),
                ResourceShare.status.in_((STATUS_REVOKED, STATUS_EXPIRED)),
            ))}
    return ended - live


# ── validating a grant ────────────────────────────────────────────────────────


def _bound(db: Session, project_id: str, workspace_id: str) -> bool:
    return db.get(ProjectWorkspace, (project_id, workspace_id)) is not None


def _is_workspace_default_owner(db: Session, project_id: str, workspace_id: str) -> bool:
    bindings = access.workspace_bindings(db, workspace_id)
    return len(bindings) <= 1 or any(
        b.project_id == project_id and b.is_workspace_default for b in bindings)


def resource_of(db: Session, owner_id: str, workspace_id: str, kind: str, resource_id: str
                ) -> str | None:
    """The display name of a resource `owner_id` holds in `workspace_id`, or None when
    it holds no such resource (another project's, a split, a missing id)."""
    from app.evaluation.models import EvalDataset
    from app.models.dlc import CriteriaSet
    from app.models.lambda_tools import ExternalMcpTarget, LambdaToolTarget
    from app.models.ledger import ResourceCreator

    with project_context(None):
        if kind in _AWS_ONLY_KINDS:
            owner = db.execute(select(ResourceCreator.project_id).where(
                ResourceCreator.workspace_id == workspace_id, ResourceCreator.kind == kind,
                ResourceCreator.resource_id == resource_id)).first()
            if owner is not None:
                return resource_id if owner[0] == owner_id else None
            # no ownership row: the workspace default project's (design §5.5)
            if _is_workspace_default_owner(db, owner_id, workspace_id):
                return resource_id
            return None
        if kind == KIND_EVAL_DATASET:
            row = db.get(EvalDataset, resource_id)
            ok = (row is not None and row.project_id == owner_id
                  and row.workspace_id == workspace_id and not row.split_of)
            return row.name if ok else None
        if kind == KIND_CRITERIA_SET:
            row = db.scalars(select(CriteriaSet).where(
                CriteriaSet.lineage_id == resource_id, CriteriaSet.workspace_id == workspace_id,
                CriteriaSet.project_id == owner_id, CriteriaSet.kind == "template",
            ).order_by(CriteriaSet.version.desc())).first()
            return row.name if row is not None else None
        if kind == KIND_GATEWAY_TARGET:
            for model in (LambdaToolTarget, ExternalMcpTarget):
                row = db.get(model, resource_id)
                if (row is not None and row.project_id == owner_id
                        and row.workspace_id == workspace_id):
                    return row.target_name
            return None
    return None


def _share_not_found(share_id: str) -> NotFoundError:
    return NotFoundError("share.not_found", f"share '{share_id}' not found",
                         {"share_id": share_id})


def _get(db: Session, share_id: str) -> ResourceShare:
    row = db.get(ResourceShare, share_id)
    if row is None:
        raise _share_not_found(share_id)
    return row


def _expires(days: int | None) -> datetime:
    days = DEFAULT_EXPIRY_DAYS if days is None else days
    if not 1 <= days <= MAX_EXPIRY_DAYS:
        raise AppError("share.invalid_expiry",
                       f"a share lasts 1–{MAX_EXPIRY_DAYS} days", {"days": days},
                       status_code=422)
    return _now() + timedelta(days=days)


def grant(
    db: Session,
    identity: Any,
    owner: Project,
    *,
    workspace_id: str,
    kind: str,
    resource_id: str,
    grantee_project_id: str,
    access_level: str,
    expires_in_days: int | None = None,
    resource_name: str = "",
    note: str = "",
) -> ResourceShare:
    """Share one of `owner`'s resources with another project of the workspace. The
    caller holds `project.share` in `owner` (the route policy checked it). Bumps the
    epoch; the caller commits."""
    if kind not in SHARE_KINDS:
        raise AppError("share.invalid_kind", f"'{kind}' cannot be shared",
                       {"kind": kind, "kinds": list(SHARE_KINDS)}, status_code=422)
    if access_level not in ACCESS_LEVELS:
        raise AppError("share.invalid_access", "access is read or use",
                       {"access": access_level}, status_code=422)
    resource_id = (resource_id or "").strip()
    if not resource_id or len(resource_id) > 128:
        raise AppError("share.invalid_resource", "name the resource to share",
                       status_code=422)
    if grantee_project_id == owner.id:
        raise AppError("share.self", "a project cannot share with itself", status_code=422)
    if not _bound(db, owner.id, workspace_id):
        raise NotFoundError("workspace.not_found",
                            f"project '{owner.id}' is not bound to '{workspace_id}'",
                            {"workspace_id": workspace_id})
    grantee = db.get(Project, grantee_project_id)
    if grantee is None or not _bound(db, grantee.id, workspace_id):
        # shares never leave the workspace, and nothing else is the owner's to learn
        raise NotFoundError("project.not_found", f"project '{grantee_project_id}' not found",
                            {"project_id": grantee_project_id})
    if grantee.status != "active":
        raise AppError("project.archived", f"project '{grantee.id}' is archived",
                       {"project_id": grantee.id}, status_code=409)
    name = resource_of(db, owner.id, workspace_id, kind, resource_id)
    if name is None:
        raise NotFoundError("share.resource_not_found",
                            f"{kind} '{resource_id}' is not this project's",
                            {"kind": kind, "resource_id": resource_id})
    with project_context(None):
        open_row = db.scalar(select(ResourceShare.id).where(
            ResourceShare.kind == kind, ResourceShare.resource_id == resource_id,
            ResourceShare.grantee_project_id == grantee.id,
            ResourceShare.status.in_(OPEN_STATUSES)))
    if open_row is not None:
        raise AppError("share.exists", "this resource is already shared with that project "
                       "— revoke the existing share first", {"share_id": open_row},
                       status_code=409)
    actor, actor_user_id = _actor(identity)
    cross = (owner.department_id or None) != (grantee.department_id or None)
    # across departments the grantee's department decides (§11.2); a caller who is
    # that approver already (an administrator, its department administrator) grants
    approver = not cross or _may_approve(db, identity, grantee)
    row = ResourceShare(
        id=uuid.uuid4().hex, kind=kind, resource_id=resource_id, workspace_id=workspace_id,
        owner_project_id=owner.id, grantee_project_id=grantee.id, access=access_level,
        status=STATUS_ACTIVE if approver else STATUS_REQUESTED,
        resource_name=(resource_name.strip() or name)[:128], note=(note or "")[:2000],
        granted_by=actor[:64], granted_by_user_id=actor_user_id,
        expires_at=_expires(expires_in_days),
    )
    if approver and cross:
        row.approved_by, row.approved_by_user_id = actor[:64], actor_user_id
        row.approved_at = _now()
    db.add(row)
    db.flush()
    access.bump_epoch(db)
    forget_shared_rows()
    _journal(db, identity, row, "grant" if approver else "request")
    return row


def _may_approve(db: Session, identity: Any, grantee: Project) -> bool:
    if grantee.department_id:
        return access.can_manage_structure(identity, grantee.department_id, db=db)
    return access.can_manage_structure(identity)


def decide(db: Session, identity: Any, grantee_project_id: str, share_id: str,
           *, approve: bool) -> ResourceShare:
    """Approve or reject a cross-department request (the grantee side). The route lets
    administrators and the grantee's department administrators through."""
    row = _get(db, share_id)
    if row.grantee_project_id != grantee_project_id:
        raise _share_not_found(share_id)
    grantee = db.get(Project, grantee_project_id)
    if grantee is None or not _may_approve(db, identity, grantee):
        raise _share_not_found(share_id)
    if row.status != STATUS_REQUESTED:
        raise AppError("share.not_requested", f"share '{share_id}' is {row.status}",
                       {"status": row.status}, status_code=409)
    actor, actor_user_id = _actor(identity)
    row.status = STATUS_ACTIVE if approve else STATUS_REJECTED
    row.approved_by, row.approved_by_user_id, row.approved_at = actor[:64], actor_user_id, _now()
    access.bump_epoch(db)
    forget_shared_rows()
    _journal(db, identity, row, "approve" if approve else "reject")
    return row


def dependents(db: Session, row: ResourceShare) -> list[str]:
    """The grantee's live agents pinning a shared KB (names) — told on revoke: their
    next publish fails `check_pins` until the pin goes (§11.2)."""
    if row.kind != KIND_KNOWLEDGE_BASE:
        return []
    from app.models.ledger import Agent

    with project_context(None):
        agents = db.scalars(select(Agent).where(
            Agent.project_id == row.grantee_project_id, Agent.workspace_id == row.workspace_id,
            Agent.status != "deleted"))
        out = []
        for agent in agents:
            pins = (agent.spec or {}).get("knowledge_bases") or []
            if any(isinstance(p, dict) and p.get("kb_id") == row.resource_id for p in pins):
                out.append(agent.name)
    return sorted(out)


def revoke(db: Session, identity: Any, owner_project_id: str, share_id: str) -> ResourceShare:
    """End an open share (the owner side; administrators pass the route too)."""
    row = _get(db, share_id)
    if row.owner_project_id != owner_project_id:
        raise _share_not_found(share_id)
    if row.status not in OPEN_STATUSES:
        raise AppError("share.not_open", f"share '{share_id}' is {row.status}",
                       {"status": row.status}, status_code=409)
    actor, actor_user_id = _actor(identity)
    was_active = row.status == STATUS_ACTIVE
    row.status = STATUS_REVOKED
    row.revoked_by, row.revoked_by_user_id, row.revoked_at = actor[:64], actor_user_id, _now()
    access.bump_epoch(db)
    forget_shared_rows()
    pinned = dependents(db, row)
    _journal(db, identity, row, "revoke",
             f" (pinned by {', '.join(pinned)})" if pinned else "")
    if was_active:
        _after_end(db, identity, row, NOTICE_REVOKED, pinned)
    return row


def expire_due(db: Session) -> dict[str, Any]:
    """The scheduler's sweep: open shares past `expires_at` become `expired` (one epoch
    bump for the batch). Journaled under each owner project."""
    now = _now()
    with project_context(None):
        due = list(db.scalars(select(ResourceShare).where(
            ResourceShare.status.in_(OPEN_STATUSES), ResourceShare.expires_at.is_not(None),
            ResourceShare.expires_at <= now)))
    ended: list[ResourceShare] = []
    for row in due:
        if row.status == STATUS_ACTIVE:
            ended.append(row)
        with project_context(row.owner_project_id):
            row.status = STATUS_EXPIRED
            _journal(db, None, row, "expire")
    if due:
        access.bump_epoch(db)
        forget_shared_rows()
    for row in ended:  # a request that expired unanswered never reached the grantee
        _after_end(db, None, row, NOTICE_EXPIRED, dependents(db, row))
    db.commit()
    return {"expired": [row.id for row in due]}


# ── when a share ends: the grantee's owners, the isolated grantee's boundary ──

NOTICE_PREFIX = "share.notice."
NOTICE_REVOKED, NOTICE_EXPIRED = "revoked", "expired"
NOTICE_LIMIT = 50
_RERENDER = "launchpad_share_boundary_rerender"  # Session.info key: committed → queued


def _after_end(db: Session, identity: Any | None, row: ResourceShare, kind: str,
               pinned: list[str]) -> None:
    """An active share just ended (in the caller's transaction): tell the grantee's
    owners, and re-render an isolated grantee's boundary once the change is committed."""
    try:
        notify_grantee(db, identity, row, kind, pinned)
    except Exception:  # noqa: BLE001 — the revocation stands whatever the notice does
        logger.warning("notice of share %s unwritten", row.id, exc_info=True)
    if row.kind == KIND_KNOWLEDGE_BASE and row.access == ACCESS_USE and row.workspace_id:
        actor, actor_user_id = _actor(identity) if identity is not None else ("scheduler", None)
        db.info.setdefault(_RERENDER, set()).add(
            (row.grantee_project_id, row.workspace_id, actor, actor_user_id))


def rerender_boundary(project_id: str, workspace_id: str, *, actor: str,
                      actor_user_id: str | None = None, start: bool = True) -> str | None:
    """Queue (and start) a provisioning run of an active isolated project in one
    workspace — its `boundary` stage renders the policy without the KBs whose share
    ended (`boundary_statements`); the other stages are idempotent. None for a shared
    project (no boundary of its own) or when nothing was queued."""
    from app.core.db import SessionLocal
    from app.services import project_isolation

    db = SessionLocal()
    try:
        with project_context(None):
            project = db.get(Project, project_id)
            if not project_isolation.is_isolated(project) or project.status != "active":
                return None
            if not _bound(db, project_id, workspace_id):
                return None
        job, created = project_isolation.queue_provision(
            db, project_id, workspace_id, actor=actor, actor_user_id=actor_user_id)
    finally:
        db.close()
    if not created:
        return None  # a run already in flight reads the shares when its stage starts
    if start:
        project_isolation.start_jobs([job.id])
    return job.id


@event.listens_for(Session, "after_commit")
def _rerender_after_commit(session: Session) -> None:
    pending = session.info.pop(_RERENDER, None)
    for project_id, workspace_id, actor, actor_user_id in sorted(pending or ()):
        try:
            rerender_boundary(project_id, workspace_id, actor=actor,
                              actor_user_id=actor_user_id)
        except Exception:  # noqa: BLE001 — the next deploy renders it anyway
            logger.warning("boundary re-render of %s in %s not queued", project_id,
                           workspace_id, exc_info=True)


@event.listens_for(Session, "after_rollback")
def _forget_rerender(session: Session) -> None:
    session.info.pop(_RERENDER, None)


def notify_grantee(db: Session, identity: Any | None, row: ResourceShare, kind: str,
                   pinned: list[str]) -> str | None:
    """Leave the grantee project's owners (`project.members` holders, as the steward's
    notices) a notice that a share ended. Joins the caller's transaction. None when the
    grantee has nobody to tell (the journal still has it)."""
    import json

    from app.models.platform_settings import PlatformSetting
    from app.services import steward

    with project_context(None):
        grantee = db.get(Project, row.grantee_project_id)
        if grantee is None:
            return None
        me = getattr(identity, "user_id", None) if identity is not None else None
        recipients = [uid for uid in steward.owner_ids(db, grantee) if uid != me]
    if not recipients:
        return None
    names = project_names(db, {row.owner_project_id})
    actor = _actor(identity)[0] if identity is not None else "scheduler"
    doc = {
        "id": uuid.uuid4().hex, "kind": kind, "share_id": row.id,
        "project_id": grantee.id, "project_name": grantee.name,
        "owner_project_id": row.owner_project_id,
        "owner_project_name": names.get(row.owner_project_id, row.owner_project_id),
        "resource_kind": row.kind, "resource_id": row.resource_id,
        "resource_name": row.resource_name or row.resource_id, "access": row.access,
        "workspace_id": row.workspace_id, "pinned_by": list(pinned), "actor": actor,
        "recipients": recipients, "read_by": [], "created_at": _now().isoformat(),
    }
    db.add(PlatformSetting(key=f"{NOTICE_PREFIX}{doc['id']}",
                           value=json.dumps(doc, sort_keys=True), updated_by=actor[:64],
                           updated_at=_now()))
    return doc["id"]


def notices_for(db: Session, identity: Any) -> dict[str, Any]:
    """The caller's notices of ended shares, newest first (only the caller's)."""
    import json

    from app.models.platform_settings import PlatformSetting

    me = getattr(identity, "user_id", None)
    items: list[dict[str, Any]] = []
    if me:
        for row in db.scalars(select(PlatformSetting).where(
                PlatformSetting.key.like(f"{NOTICE_PREFIX}%"))):
            try:
                doc = json.loads(row.value or "{}")
            except ValueError:
                continue
            if not isinstance(doc, dict) or me not in (doc.get("recipients") or []):
                continue
            view = {k: v for k, v in doc.items() if k not in ("recipients", "read_by")}
            view["read"] = me in (doc.get("read_by") or [])
            items.append(view)
    items.sort(key=lambda i: str(i.get("created_at") or ""), reverse=True)
    return {"items": items[:NOTICE_LIMIT], "unread": sum(1 for i in items if not i["read"])}


def acknowledge(db: Session, identity: Any, notice_id: str) -> None:
    """Mark one of the caller's notices read; anyone else's answers 404."""
    import json

    from app.models.platform_settings import PlatformSetting

    me = getattr(identity, "user_id", None)
    row = db.get(PlatformSetting, f"{NOTICE_PREFIX}{notice_id}")
    try:
        doc = json.loads(row.value) if row is not None else {}
    except ValueError:
        doc = {}
    if not me or me not in (doc.get("recipients") or []):
        raise NotFoundError("share.notice_not_found", f"notice '{notice_id}' not found",
                            {"notice_id": notice_id})
    if me not in doc.setdefault("read_by", []):
        doc["read_by"].append(me)
        assert row is not None
        row.value = json.dumps(doc, sort_keys=True)


# ── views ─────────────────────────────────────────────────────────────────────


def project_names(db: Session, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    return dict(db.execute(select(Project.id, Project.name).where(Project.id.in_(sorted(ids)))
                           ).all())


def view(row: ResourceShare, names: dict[str, str], *, extra: dict[str, Any] | None = None
         ) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        value = _aware(value)
        return value.isoformat() if value else None

    status = row.status
    expires = _aware(row.expires_at)
    if status == STATUS_ACTIVE and expires is not None and expires <= _now():
        status = STATUS_EXPIRED  # the sweep has not run yet; it no longer counts
    return {
        "id": row.id, "kind": row.kind, "resource_id": row.resource_id,
        "resource_name": row.resource_name or row.resource_id,
        "workspace_id": row.workspace_id,
        "owner_project_id": row.owner_project_id,
        "owner_project_name": names.get(row.owner_project_id, row.owner_project_id),
        "grantee_project_id": row.grantee_project_id,
        "grantee_project_name": names.get(row.grantee_project_id, row.grantee_project_id),
        "access": row.access, "status": status, "note": row.note,
        "granted_by": row.granted_by, "granted_at": iso(row.granted_at),
        "approved_by": row.approved_by, "approved_at": iso(row.approved_at),
        "revoked_by": row.revoked_by, "revoked_at": iso(row.revoked_at),
        "expires_at": iso(row.expires_at),
        **(extra or {}),
    }


def list_for(db: Session, project_id: str, direction: str = "all", *,
             include_closed: bool = False) -> dict[str, Any]:
    """The shares a project grants (`outgoing`) and receives (`incoming`)."""
    if direction not in ("all", "outgoing", "incoming"):
        raise AppError("share.invalid_direction", "direction is outgoing, incoming or all",
                       status_code=422)
    out: dict[str, list[ResourceShare]] = {}
    for side, column in (("outgoing", ResourceShare.owner_project_id),
                         ("incoming", ResourceShare.grantee_project_id)):
        if direction not in ("all", side):
            continue
        query = select(ResourceShare).where(column == project_id)
        if not include_closed:
            query = query.where(ResourceShare.status.in_(OPEN_STATUSES))
        with project_context(None):
            out[side] = list(db.scalars(query.order_by(ResourceShare.granted_at.desc())))
    ids = {p for rows in out.values() for r in rows
           for p in (r.owner_project_id, r.grantee_project_id)}
    names = project_names(db, ids)
    return {side: [view(r, names) for r in rows] for side, rows in out.items()}


def options(db: Session, owner: Project, workspace_id: str) -> dict[str, Any]:
    """What `owner` can share in a workspace, and with whom (the share form)."""
    from app.evaluation.models import EvalDataset
    from app.models.dlc import CriteriaSet
    from app.models.lambda_tools import ExternalMcpTarget, LambdaToolTarget
    from app.models.ledger import ResourceCreator

    if not _bound(db, owner.id, workspace_id):
        raise NotFoundError("workspace.not_found",
                            f"project '{owner.id}' is not bound to '{workspace_id}'",
                            {"workspace_id": workspace_id})
    resources: list[dict[str, Any]] = []
    with project_context(None):
        for row in db.scalars(select(EvalDataset).where(
                EvalDataset.project_id == owner.id, EvalDataset.workspace_id == workspace_id,
                EvalDataset.split_of.is_(None)).order_by(EvalDataset.name)):
            resources.append({"kind": KIND_EVAL_DATASET, "id": row.id, "name": row.name,
                              "detail": row.role})
        latest: dict[str, CriteriaSet] = {}
        for row in db.scalars(select(CriteriaSet).where(
                CriteriaSet.project_id == owner.id, CriteriaSet.workspace_id == workspace_id,
                CriteriaSet.kind == "template").order_by(CriteriaSet.version)):
            latest[row.lineage_id] = row
        for lineage, row in sorted(latest.items(), key=lambda kv: kv[1].name):
            resources.append({"kind": KIND_CRITERIA_SET, "id": lineage, "name": row.name,
                              "detail": f"v{row.version}"})
        for model in (LambdaToolTarget, ExternalMcpTarget):
            for row in db.scalars(select(model).where(
                    model.project_id == owner.id, model.workspace_id == workspace_id)
                    .order_by(model.target_name)):
                resources.append({"kind": KIND_GATEWAY_TARGET, "id": row.id,
                                  "name": row.target_name, "detail": model.__tablename__})
        for row in db.scalars(select(ResourceCreator).where(
                ResourceCreator.project_id == owner.id,
                ResourceCreator.workspace_id == workspace_id,
                ResourceCreator.kind.in_(_AWS_ONLY_KINDS)).order_by(ResourceCreator.resource_id)):
            resources.append({"kind": row.kind, "id": row.resource_id, "name": row.resource_id,
                              "detail": ""})
        grantees = db.execute(
            select(Project.id, Project.name, Project.department_id)
            .join(ProjectWorkspace, ProjectWorkspace.project_id == Project.id)
            .where(ProjectWorkspace.workspace_id == workspace_id, Project.id != owner.id,
                   Project.status == "active").order_by(Project.name)
        ).all()
    return {
        "workspace_id": workspace_id, "kinds": list(SHARE_KINDS),
        "default_expiry_days": DEFAULT_EXPIRY_DAYS, "max_expiry_days": MAX_EXPIRY_DAYS,
        "resources": resources,
        "grantees": [{"id": g[0], "name": g[1],
                      "cross_department": (g[2] or None) != (owner.department_id or None)}
                     for g in grantees],
    }


# ── runtime: the isolated grantee's boundary (R10 hook) ──────────────────────

KB_READ_ACTIONS = ["bedrock:GetKnowledgeBase", "bedrock:Retrieve*"]


def boundary_statements(inp: Any) -> list[dict[str, Any]]:
    """`project_iam.EXTRA_BOUNDARY_STATEMENT_BUILDERS`: an isolated project's boundary
    allows reading the KBs shared with it at `use` level in the boundary's workspace,
    by explicit ARN (the design's fallback to the `launchpad:shared-with` tag, which
    needs no tagging job). KBs the project's agents already pin are listed by the
    boundary itself. A revoked share leaves with the next boundary render (deploy)."""
    from app.core.db import SessionLocal
    from app.models.ledger import Workspace

    db = SessionLocal()
    try:
        with project_context(None):
            workspaces = [w for (w,) in db.execute(select(Workspace.id).where(
                Workspace.account_id == inp.account_id, Workspace.region == inp.region))]
            if not workspaces:
                return []
            now = _now()
            kb_ids = sorted({rid for (rid,) in db.execute(select(ResourceShare.resource_id).where(
                ResourceShare.kind == KIND_KNOWLEDGE_BASE,
                ResourceShare.grantee_project_id == inp.project_id,
                ResourceShare.workspace_id.in_(workspaces),
                ResourceShare.status == STATUS_ACTIVE, ResourceShare.access == ACCESS_USE,
                or_(ResourceShare.expires_at.is_(None), ResourceShare.expires_at > now),
            ))} - set(inp.kb_ids))
    except Exception:  # noqa: BLE001 — no ledger (a pure document test): nothing shared
        logger.debug("shared KBs for the boundary of %s unreadable", inp.project_id,
                     exc_info=True)
        return []
    finally:
        db.close()
    if not kb_ids:
        return []
    kb = f"arn:{inp.partition}:bedrock:{inp.region}:{inp.account_id}:knowledge-base"
    return [{"Sid": "KbShared", "Effect": "Allow", "Action": KB_READ_ACTIONS,
             "Resource": [f"{kb}/{kb_id}" for kb_id in kb_ids]}]
