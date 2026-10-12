"""The account-free annotation page's API (design §7.4).

Mounted under `/share` beside the chat and review routes (PUBLIC + hub-global in
`route_policy`), so no console session and no `X-Workspace` header is ever read: the
link row names the workspace, the task and the annotator.

What this surface deliberately does *not* expose: the judge's verdict (labelling is
blind, and seeing it first would make the agreement number meaningless), the other
annotators' votes, the agreement statistics, and any way to decide a calibration.
An outsider labels; a workspace member with `judge.calibrate` reads the numbers and
records the verdict.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services import annotation_links, share_links

router = APIRouter(prefix="/share/annotate", tags=["share-annotate"])


class LabelIn(BaseModel):
    item_ref: str = Field(min_length=1, max_length=160)
    label: str = Field(default="", max_length=32)
    answer: str = Field(default="", max_length=16000)
    rationale: str = Field(default="", max_length=4000)


@router.get("/{token}", summary="The labelling queue behind an annotation link")
def annotation_queue(token: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    link, task = annotation_links.resolve(db, token)
    annotation_links.enforce_rate_limit(link.id)
    share_links.record_use(link.id)
    return annotation_links.view(db, link, task)


@router.post("/{token}/label", summary="Record one label")
def annotation_label(
    token: str, req: LabelIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    link, task = annotation_links.resolve(db, token)
    annotation_links.enforce_rate_limit(link.id)
    return annotation_links.record(
        db, link, task, item_ref=req.item_ref, label=req.label,
        rationale=req.rationale, answer=req.answer,
    )
