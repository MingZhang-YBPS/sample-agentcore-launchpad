"""Console rail settings: the modules an administrator hid (``services/nav_settings``).

``GET`` is every signed-in account's (the rail reads it to drop the hidden entries);
``PUT`` is the administrators'. A view filter only: hiding an entry closes no route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import Identity, require_admin, require_identity
from app.services import nav_settings

router = APIRouter(tags=["nav"])


class HiddenModules(BaseModel):
    hidden: list[str] = Field(default_factory=list, max_length=nav_settings.MAX_ENTRIES)


@router.get("/api/nav/hidden-modules")
def get_hidden_modules(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    require_identity(request)
    return nav_settings.view(db)


@router.put("/api/nav/hidden-modules")
def put_hidden_modules(
    body: HiddenModules,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_admin),
) -> dict[str, Any]:
    result = nav_settings.set_hidden_modules(db, body.hidden, identity)
    db.commit()
    return result
