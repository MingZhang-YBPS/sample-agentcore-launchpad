"""Annotation links: a domain expert labels without a console account (design §7.4).

Calibration needs *two* people labelling the same items, and the people who know
whether an answer is right are usually not console users — a product owner, a legal
reviewer, a support lead. Making each of them an account before they can label two
dozen sessions is how calibration quietly stops happening.

This reuses the T13 share-link primitive (`ShareLink` with `kind="annotate"`), so the
token is hashed at rest, every unusable state collapses into the same 404
`share.not_found`, and the *row* names the workspace — no header, no session.

Two rules that are not conveniences:

* **A link is an annotator.** Creating one appends `link_<id>` to the task's
  `annotators`, so its labels count toward human–human κ under a stable identity and
  the label (the person's name) is only for display. No link, no vote.
* **Not in prod.** `links_allowed()` refuses a `prod`-tier workspace: there, labelling
  touches real customer transcripts, so an account and a grant are the point rather
  than friction. Dev and staging workspaces may use links.

The annotator never sees the judge's verdict — `task_view(..., privileged=False)`
withholds it until the task closes, which is what makes the agreement number mean
anything.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.dlc import calibration as cal
from app.models.dlc import AnnotationTask
from app.models.ledger import ShareLink, Workspace
from app.services import share_links
from app.services.share_links import TokenBucket

KIND_ANNOTATE = "annotate"
ANNOTATOR_PREFIX = "link_"
# an annotator works through a queue of items quickly; its own bucket so an
# annotation link can never starve a chat link sharing the same id space
annotate_limiter = TokenBucket(capacity=120, refill_per_sec=2.0)


def enforce_rate_limit(link_id: str) -> None:
    wait = annotate_limiter.take(link_id)
    if wait:
        retry_after = max(1, int(wait) + 1)
        raise AppError(
            "share.rate_limited", "too many requests — please slow down",
            {"retry_after_seconds": retry_after}, status_code=429,
            headers={"Retry-After": str(retry_after)},
        )


def annotator_name(link_id: str) -> str:
    """The stable identity a link's labels are recorded under (fits `String(64)`)."""
    return f"{ANNOTATOR_PREFIX}{link_id}"


def is_link_annotator(name: str) -> bool:
    return str(name or "").startswith(ANNOTATOR_PREFIX)


def links_allowed(workspace: Workspace | None) -> tuple[bool, str]:
    """Whether this workspace may hand out account-free annotation links."""
    tier = (workspace.tier if workspace else "dev") or "dev"
    if tier == "prod":
        return False, (
            "a prod workspace labels real customer transcripts — invite the annotator as "
            "a member with judge-calibration access instead of sending a link"
        )
    return True, ""


def create(
    db: Session,
    *,
    task: AnnotationTask,
    workspace: Workspace | None,
    label: str,
    created_by: str,
    expires_in_days: int | None,
) -> tuple[ShareLink, str]:
    ok, reason = links_allowed(workspace)
    if not ok:
        raise AppError("annotation.links_not_allowed", reason, status_code=409)
    if task.status == "closed":
        raise AppError("annotation.closed", "this task is closed", status_code=409)
    if not label.strip():
        raise AppError("annotation.link_label", "name the person this link is for")
    link, raw = share_links.create_link(
        db, workspace_id=task.workspace_id, agent=None, target_id=task.id,
        label=label, created_by=created_by, expires_in_days=expires_in_days,
        kind=KIND_ANNOTATE,
    )
    # a link only counts as an annotator once the task says so
    task.annotators = [*(task.annotators or []), annotator_name(link.id)]
    db.flush()
    return link, raw


def resolve(db: Session, raw_token: str) -> tuple[ShareLink, AnnotationTask]:
    """Token → (live link, its task), or the same opaque 404 for every dead state."""
    link = share_links.resolve_link(db, raw_token, kinds=(KIND_ANNOTATE,))
    task = db.get(AnnotationTask, link.target_id)
    if task is None or task.workspace_id != link.workspace_id:
        raise share_links.not_found()
    workspace = db.get(Workspace, link.workspace_id or "")
    # a workspace promoted to prod after the link was issued closes the link too
    if workspace is None or not links_allowed(workspace)[0]:
        raise share_links.not_found()
    return link, task


def view(db: Session, link: ShareLink, task: AnnotationTask) -> dict[str, Any]:
    """What the account-free page shows: the items, blind, plus this link's progress."""
    me = annotator_name(link.id)
    out = cal.task_view(db, task, viewer=me, privileged=False)
    return {
        "label": link.label,
        "criterion_key": task.criterion_key,
        "purpose": task.purpose,
        "status": task.status,
        "total": out["total"],
        "labelled": sum(1 for item in out["items"] if item.get("my_label")),
        # only this annotator's own labels and the item text — never another's vote
        "items": [
            {
                "ref": item["ref"],
                "input": item.get("input"),
                "answer": item.get("answer"),
                "my_label": item.get("my_label"),
                "my_rationale": item.get("my_rationale"),
                "my_answer": item.get("my_answer"),
            }
            for item in out["items"]
        ],
    }


def record(
    db: Session,
    link: ShareLink,
    task: AnnotationTask,
    *,
    item_ref: str,
    label: str,
    rationale: str,
    answer: str,
) -> dict[str, Any]:
    cal.record_label(
        db, task, annotator=annotator_name(link.id), item_ref=item_ref, label=label,
        rationale=rationale, answer=answer,
    )
    db.commit()
    return view(db, link, task)
