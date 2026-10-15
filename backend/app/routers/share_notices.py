"""The caller's notices of ended cross-project shares (projects design §11.2, SHARE-2).

When a share another project lent ends — its owner revoked it, or it expired — the
grantee project's owners (`project.members` holders) get a notice, like the steward's
(`services/resource_shares.notify_grantee`). Hub-global under `/api/me`: every account
reads only its own notices and marks them read; anyone else's answers 404.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.services import resource_shares as svc

router = APIRouter(prefix="/api/me/share-notices", tags=["projects"])


@router.get("")
def share_notices(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    return svc.notices_for(db, require_identity(request))


@router.post("/{notice_id}/ack")
def acknowledge_share_notice(
    notice_id: str, request: Request, db: Session = Depends(get_db),
) -> dict[str, Any]:
    svc.acknowledge(db, require_identity(request), notice_id)
    db.commit()
    return {"ok": True, "notice_id": notice_id}
