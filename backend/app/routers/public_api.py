"""Public /v1 API — the system-integration entrance.

Same invoke chain as the Chat playground (services.chat / services.invoke);
auth is an X-Api-Key header checked against hashed keys in the ledger.

No `X-Workspace` header is involved here: the key itself names the environment
it was minted for, so it both authenticates the caller and scopes what it can
reach. Targeting stays the agent row's business (`invoke._agent_workspace`) —
the two agree because a key only ever resolves agents from its own workspace.

Nor is there an `X-Project` header: a key belongs to the project it was minted
in (`api_keys.project_id`, design projects §5.7), and every /v1 call runs as that
project (`api_key_project`) — the ORM's automatic criteria then confine the agent
lookups, and the rows an invoke writes are stamped with it, exactly as in a
console request. Another project's agent id reads as missing.
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.assistant.sessions import refuse_assistant_session
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context, set_current_project
from app.models.ledger import Agent, ApiKey
from app.routers.apikeys import hash_key
from app.schemas.agent import display_name_of
from app.schemas.attachments import AttachmentRequest, SessionIdField
from app.schemas.strands_builder import PROMPT_VARIABLE_RE
from app.services import agent_access, visibility
from app.services import api_keys as api_keys_service
from app.services.attachments import attachment_capability, prepare_attachments
from app.services.chat import chat_stream, sse_encode
from app.services.invoke import invoke_agent_text, upstream_invoke_error
from app.services.memory import scoped_actor
from app.services.runtime_discovery import invoke_capability, require_invoke_capability
from app.services.workspace import get_workspace_row

router = APIRouter(prefix="/v1", tags=["public-v1"])


def require_api_key(
    x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)
) -> ApiKey:
    """The key behind this call — and with it, the workspace it is scoped to.

    The hash lookup is deliberately global: it is the only way to learn which
    workspace the caller is in, since /v1 carries no header. Everything after it
    filters on `key.workspace_id`.

    A key whose creator has been offboarded is refused (and disabled) here — see
    `services.api_keys`. Then expiry and the per-key rate limit (T16) together with
    the key's project window (`settings.quotas.v1_rate_per_minute`, projects §10.7)
    when the project sets one; an admitted call is counted (`use_count`, the
    per-day usage row) and stamps `last_used_at`.
    """
    if not x_api_key:
        raise AppError("auth.missing_api_key", "X-Api-Key header required", status_code=401)
    with project_context(None):  # which project is what the key itself answers
        key = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(x_api_key)).first()
    if key is None or not key.enabled:
        raise AppError("auth.invalid_api_key", "invalid or disabled API key", status_code=401)
    if get_workspace_row(db, key.workspace_id or "") is None:
        # Its environment is gone, so it names no agents and nothing downstream
        # could resolve an invoke target. Answered like any other unusable key:
        # /v1 never reports whether a workspace once existed.
        raise AppError("auth.invalid_api_key", "invalid or disabled API key", status_code=401)
    if api_keys_service.creator_offboarded(db, key) is not None:
        # The member who minted it is gone (deleted, disabled, expired, or no
        # longer a member of the key's project here), or the project itself is
        # archived or unbound. Disabled for good in the same request — expiry has
        # no write event, so this is where it lands — and answered like any other
        # unusable key: /v1 never says why.
        key.enabled = False
        db.commit()
        raise AppError("auth.invalid_api_key", "invalid or disabled API key", status_code=401)
    if api_keys_service.is_expired(key):
        raise AppError("auth.expired_api_key", "API key has expired", status_code=401)
    api_keys_service.check_rate(
        key, project_limit=api_keys_service.project_rate_limit(db, key)
    )
    # Stamps `last_used_at` and `use_count` on every admitted call (T16), so the
    # throttled `touch_last_used` stamp has nothing left to add here.
    api_keys_service.record_use(db, key)
    return key


async def api_key_project(key: ApiKey = Depends(require_api_key)) -> ApiKey:
    """The admitted key, with its project made current for the rest of the request.

    Async on purpose, like `route_policy.bind_request_project`: it runs in the
    request's own task, so the sync handler and a streaming response's generator
    (each run in the threadpool with a copy of this context) see the project; a
    sync dependency's context change would not outlive it."""
    set_current_project(key.project_id)
    return key


MAX_CONTEXT_KEYS = 20
MAX_CONTEXT_VALUE = 2000


class InvokeV1Request(AttachmentRequest):
    session_id: str | None = SessionIdField
    actor_id: str = "api"
    # strands-builder SB-1: this call's {{variable}} values; the agent fills the
    # prompt variables it declares and ignores any other key
    context: dict[str, str] | None = Field(default=None, max_length=MAX_CONTEXT_KEYS)

    @field_validator("context")
    @classmethod
    def _context_shape(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        for name, text in (value or {}).items():
            if not PROMPT_VARIABLE_RE.match(name):
                raise ValueError(
                    f"context key {name!r} must match {PROMPT_VARIABLE_RE.pattern}")
            if len(text) > MAX_CONTEXT_VALUE:
                raise ValueError(
                    f"context value {name!r} exceeds {MAX_CONTEXT_VALUE} characters")
        return value or None


def _active_agent(db: Session, key: ApiKey, agent_id: str) -> Agent:
    """The agent this key may invoke. One from another workspace — or one its
    creator cannot see under workspace isolation, or one of another project — reads
    exactly like a missing one: the key's scope must not be probeable. So does one
    outside the key's own `agent_ids` allow-list (T16)."""
    agent = agent_access.agent_in(
        db, key.workspace_id, agent_id, viewer=visibility.scope_for_key(db, key)
    )
    if agent is None or not api_keys_service.allows_agent(key, agent_id):
        raise NotFoundError("agent.not_found", "agent not found")
    require_invoke_capability(agent)
    return agent


@router.get("/agents", summary="List active agents")
def v1_list_agents(
    db: Session = Depends(get_db), key: ApiKey = Depends(api_key_project)
) -> dict[str, Any]:
    agents = [
        agent
        for agent in db.query(Agent)
        .filter(
            Agent.status == "active",
            Agent.workspace_id == key.workspace_id,
            agent_access.agent_clause(visibility.scope_for_key(db, key)),
        )
        .all()
        if invoke_capability(agent)["eligible"] and api_keys_service.allows_agent(key, agent.id)
    ]
    return {
        "agents": [
            {"id": a.id, "name": a.name, "display_name": display_name_of(a.spec),
             "method": a.method, "version": a.version,
             "attachment_capability": attachment_capability(a)}
            for a in agents
        ]
    }


@router.post("/agents/{agent_id}/invoke", summary="Invoke an agent (sync)")
def v1_invoke(
    agent_id: str,
    req: InvokeV1Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(api_key_project),
) -> dict[str, Any]:
    agent = _active_agent(db, key, agent_id)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    extra: dict[str, Any] = {"attachments": prepared} if prepared else {}
    if req.context:
        extra["context"] = req.context
    started = time.monotonic()
    # No signed-in console user behind an API key: an as_user tool's consent
    # could never complete, so the invoke layer answers such an ask with 409
    # identity.as_user_requires_console instead of a dead-end consent URL.
    try:
        result = invoke_agent_text(
            agent, req.prompt, session_id=req.session_id,
            actor_id=scoped_actor(agent.id, req.actor_id),
            console_user=False,
            **extra,
        )
    except Exception as exc:
        upstream = upstream_invoke_error(exc, public=True)
        if upstream is None:
            raise
        raise upstream from exc
    body = {
        "agent": agent.name,
        "text": result["text"],
        "session_id": result["session_id"],
        "latency_ms": int((time.monotonic() - started) * 1000),
        # T35: "rule" when a curated answer replied without a model call
        "answered_by": result.get("answered_by", "model"),
    }
    # strands-builder SB-1 (D-4): an agent with an output_schema also answers the
    # parsed object; `text` stays the readable answer
    if result.get("structured") is not None:
        body["structured"] = result["structured"]
    return body


@router.post("/agents/{agent_id}/invoke-stream", summary="Invoke an agent (SSE stream)")
def v1_invoke_stream(
    agent_id: str,
    req: InvokeV1Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(api_key_project),
) -> StreamingResponse:
    agent = _active_agent(db, key, agent_id)
    refuse_assistant_session(agent, req.session_id)  # 404 before the stream opens
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    extra: dict[str, Any] = {"attachments": prepared} if prepared else {}
    if req.context:
        extra["context"] = req.context
    mem_actor = scoped_actor(agent.id, req.actor_id)

    def generate():
        # console_user=False: an as_user consent ask ends the stream with an
        # `error` event (identity.as_user_requires_console) — see v1_invoke
        for event in chat_stream(
            agent, req.prompt, session_id=req.session_id, actor_id=mem_actor,
            console_user=False, **extra,
        ):
            yield sse_encode(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
