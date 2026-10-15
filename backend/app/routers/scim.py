"""SCIM 2.0 provisioning endpoints (docs/design sso §2.4; SSO-3).

The SCIM base URL is `<console>/api/scim/v2`: `/Users`, `/Groups` and
`/ServiceProviderConfig` beneath it. Under `/api` so the reference edge (nginx
forwards `/api/` and `/v1/`, the dev server proxies `/api`) reaches it unchanged; no
console session and no workspace — `auth._OPEN_API_PATTERN` lets it through and the
`Authorization: Bearer` token names the identity provider (`sso_scim.authenticate`).
Hub-global like the rest of SSO: accounts and their mappings span projects.

Every answer is `application/scim+json`; every refusal a SCIM error document
(RFC 7644 §3.12), including a malformed body — `ScimRoute` renders them, so an IdP
never sees the console's own error envelope. Journal: `auth.scim.user.*` /
`auth.scim.group.*`, actor `scim:<idp>`.
"""

import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.core.db import DEFAULT_WORKSPACE_ID, get_db
from app.core.errors import AppError
from app.models.sso import ConsoleIdentityProvider
from app.services import sso_scim
from app.services.audit import record_audit_event
from app.services.sso_scim import ScimError

logger = logging.getLogger(__name__)

MEDIA_TYPE = "application/scim+json"


def _scim(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, media_type=MEDIA_TYPE)


class ScimRoute(APIRoute):
    """Render every refusal of a SCIM route as a SCIM error document."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except ScimError as err:
                return _scim(err.body(), err.status)
            except RequestValidationError as err:
                return _scim(ScimError(400, f"invalid request: {err.errors()[:3]}",
                                       "invalidSyntax").body(), 400)
            except AppError as err:
                return _scim(ScimError(err.status_code, err.message).body(), err.status_code)

        return route


router = APIRouter(prefix="/api/scim/v2", tags=["scim"], route_class=ScimRoute)


def _provider(request: Request, db: Session = Depends(get_db)) -> ConsoleIdentityProvider:
    return sso_scim.authenticate(db, request.headers.get("authorization"))


async def _body(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise ScimError(413, "the request body is over 2 MB", "tooMany")
    try:
        return json.loads(raw or b"null")
    except ValueError as exc:
        raise ScimError(400, "the request body is not JSON", "invalidSyntax") from exc


def _audit(row: ConsoleIdentityProvider, action: str, summary: str) -> None:
    record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor=f"scim:{row.id}"[:64],
                       action=action, target=f"idp:{row.id} {summary}"[:256],
                       actor_user_id=None, project_id=None)


def _members_excluded(excluded: str | None) -> bool:
    return "members" in {a.strip().lower() for a in (excluded or "").split(",")}


@router.get("/ServiceProviderConfig")
def service_provider_config(_: ConsoleIdentityProvider = Depends(_provider)) -> JSONResponse:
    return _scim(sso_scim.service_provider_config())


# --- Users -------------------------------------------------------------------------------


@router.get("/Users")
def list_users(
    filter: str | None = None,  # noqa: A002 — the SCIM query parameter's name
    startIndex: int | None = None,  # noqa: N803
    count: int | None = None,
    db: Session = Depends(get_db), row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    return _scim(sso_scim.list_users(db, row, filter, startIndex, count))


@router.post("/Users")
async def create_user(
    request: Request, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    resource, summary = sso_scim.create_user(db, row, await _body(request))
    _audit(row, "auth.scim.user.create", summary)
    return _scim(resource, 201)


@router.get("/Users/{user_id}")
def get_user(
    user_id: str, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    return _scim(sso_scim.get_user(db, row, user_id))


@router.put("/Users/{user_id}")
async def replace_user(
    user_id: str, request: Request, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    resource, summary = sso_scim.replace_user(db, row, user_id, await _body(request))
    _audit(row, "auth.scim.user.update", summary)
    return _scim(resource)


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: str, request: Request, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    resource, summary = sso_scim.patch_user(db, row, user_id, await _body(request))
    _audit(row, "auth.scim.user.update", summary)
    return _scim(resource)


@router.delete("/Users/{user_id}", status_code=204)
def delete_user(
    user_id: str, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> Response:
    _audit(row, "auth.scim.user.delete", sso_scim.delete_user(db, row, user_id))
    return Response(status_code=204)


# --- Groups ------------------------------------------------------------------------------


@router.get("/Groups")
def list_groups(
    filter: str | None = None,  # noqa: A002
    startIndex: int | None = None,  # noqa: N803
    count: int | None = None,
    excludedAttributes: str | None = None,  # noqa: N803
    db: Session = Depends(get_db), row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    return _scim(sso_scim.list_groups(db, row, filter, startIndex, count,
                                      members=not _members_excluded(excludedAttributes)))


@router.post("/Groups")
async def create_group(
    request: Request, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    resource, summary = sso_scim.create_group(db, row, await _body(request))
    _audit(row, "auth.scim.group.create", summary)
    return _scim(resource, 201)


@router.get("/Groups/{group_id}")
def get_group(
    group_id: str, excludedAttributes: str | None = None,  # noqa: N803
    db: Session = Depends(get_db), row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    return _scim(sso_scim.get_group(db, row, group_id,
                                    members=not _members_excluded(excludedAttributes)))


@router.put("/Groups/{group_id}")
async def replace_group(
    group_id: str, request: Request, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> JSONResponse:
    resource, summary = sso_scim.replace_group(db, row, group_id, await _body(request))
    _audit(row, "auth.scim.group.update", summary)
    return _scim(resource)


@router.patch("/Groups/{group_id}")
async def patch_group(
    group_id: str, request: Request, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> Response:
    resource, summary = sso_scim.patch_group(db, row, group_id, await _body(request))
    _audit(row, "auth.scim.group.update", summary)
    return _scim(resource)


@router.delete("/Groups/{group_id}", status_code=204)
def delete_group(
    group_id: str, db: Session = Depends(get_db),
    row: ConsoleIdentityProvider = Depends(_provider),
) -> Response:
    _audit(row, "auth.scim.group.delete", sso_scim.delete_group(db, row, group_id))
    return Response(status_code=204)
