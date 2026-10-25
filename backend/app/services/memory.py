"""AgentCore Memory helpers — session events (short-term) + records (long-term).

The default memory is the current project's (`project_memory.effective_memory_id`):
an isolated project's own memory once provisioned, the workspace's bootstrap memory
for every other project. While an isolated project is in its dual-read window the
readers here also look in the old shared memory, for the project's own agents' actors
only (`project_memory.dual_read`); an explicit ``memory_id`` (an agent-pinned memory)
is read alone.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

# memory calls get their own short-timeout client (agentcore.client.memory_data_client)
from app.services.agentcore.client import memory_data_client as data_client
from app.services.workspace import WorkspaceContext


def memory_id_or_none(workspace: WorkspaceContext) -> str | None:
    """The current project's memory id in this workspace, or None before the
    workspace's bootstrap has run.

    The console's landing view needs to distinguish "not bootstrapped" from
    "AWS call failed", so it reads the id without raising.
    """
    from app.services import project_memory

    return project_memory.effective_memory_id(workspace)


def _dual_read(workspace: WorkspaceContext, memory_id: str | None):
    """The shared memory to read as well (`project_memory.DualRead`), or None — always
    None when the caller pinned a memory."""
    if memory_id:
        return None
    from app.services import project_memory

    return project_memory.dual_read(workspace)


def _not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code == "ResourceNotFoundException"


def _memory_id(workspace: WorkspaceContext) -> str:
    memory_id = memory_id_or_none(workspace)
    if not memory_id:
        raise RuntimeError(
            "memory_id missing from this workspace's resource map — run its bootstrap"
        )
    return memory_id


def spec_memory_id(agent_spec: dict | None) -> str | None:
    """The per-agent memory an agent spec pins, or None for the workspace default.

    Reads the raw ledger JSON so callers that hold an ``Agent`` row don't have to
    re-validate the whole spec just to learn which memory its turns land in.
    """
    return ((agent_spec or {}).get("memory") or {}).get("memory_id") or None


def memory_arn_for(workspace: WorkspaceContext, memory_id: str) -> str:
    """Memory id → ARN in this workspace's account/region (GetMemory-free)."""
    return (
        f"arn:{workspace.partition}:bedrock-agentcore:{workspace.region}:"
        f"{workspace.account_id}:memory/{memory_id}"
    )


SCOPE_SEP = "__"

# AgentCore Memory's actorId constraint (CreateEvent / ListEvents ValidationException).
# Console usernames may carry "." (users._USERNAME_RE) and /v1 callers send free-form
# actor ids, so a compound id that breaks this pattern is escaped (encode_actor_part).
# AWS spells it `[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*`; the
# trailing class there only repeats what the last segment already matches, and the
# overlap backtracks polynomially on a long `0----…` actor id (CodeQL py/polynomial-
# redos), so it is dropped here — the same language, linear to match.
ACTOR_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_/]*(?::[a-zA-Z0-9\-_/]+)*$")
ACTOR_ESCAPE = "_"
_ESCAPES = {".": "d", "-": "h", "_": "u"}
_UNESCAPES = {v: k for k, v in _ESCAPES.items()}


def encode_actor_part(human: str) -> str:
    """A reversible, Memory-safe form of a human actor id: ``_`` then each
    character — letters and digits as they are, ``.`` ``-`` ``_`` as ``_d`` ``_h``
    ``_u``, anything else as ``_x<hex>_``. Prefix-free escapes make it injective,
    and the leading ``_`` (a console username never starts with one) keeps it
    apart from every id that needed no escaping."""
    out = [ACTOR_ESCAPE]
    for ch in human:
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif ch in _ESCAPES:
            out.append(f"_{_ESCAPES[ch]}")
        else:
            out.append(f"_x{ord(ch):x}_")
    return "".join(out)


def decode_actor_part(part: str) -> str:
    """Inverse of ``encode_actor_part``; anything that is not one comes back as is."""
    if not part.startswith(ACTOR_ESCAPE) or len(part) < 2:
        return part
    out: list[str] = []
    i = 1
    while i < len(part):
        ch = part[i]
        if ch != "_":
            if not (ch.isascii() and ch.isalnum()):
                return part
            out.append(ch)
            i += 1
            continue
        tag = part[i + 1 : i + 2]
        if tag in _UNESCAPES:
            out.append(_UNESCAPES[tag])
            i += 2
        elif tag == "x":
            end = part.find("_", i + 2)
            if end < 0:
                return part
            try:
                out.append(chr(int(part[i + 2 : end], 16)))
            except ValueError:
                return part
            i = end + 1
        else:
            return part
    return "".join(out)

# Strategies do not agree on a record payload shape: SEMANTIC stores prose in
# content.text, while USER_PREFERENCE (and SUMMARIZATION) store a structured
# JSON object there. Showing that object verbatim is unreadable, so pick the
# first present display field — in decreasing specificity — and hand the parsed
# object back so callers can render the rest.
# Episodic records (consolidated episodes: situation/intent/assessment/
# justification/reflection; reflections: title/use_cases/hints) sit between the
# purpose-built keys and the generic fallbacks.
_RECORD_DISPLAY_KEYS = (
    "preference",
    "summary",
    "fact",
    "title",
    "intent",
    "situation",
    "context",
    "text",
)


def decode_record_text(raw: str) -> tuple[str, dict | None]:
    """Return (display text, parsed object or None) for one memory record."""
    text = (raw or "").strip()
    if not text.startswith("{"):
        return raw, None
    try:
        parsed = json.loads(text)
    except ValueError:
        return raw, None
    if not isinstance(parsed, dict):
        return raw, None
    for key in _RECORD_DISPLAY_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value, parsed
    # a structured record with no known display field: keep the raw JSON rather
    # than inventing a summary, but still expose the parsed fields
    return raw, parsed


def scoped_actor(agent_id: str, base_actor: str = "river") -> str:
    """Fold the agent id into the memory actor id so memory partitions per agent.

    AgentCore Memory has no notion of "agent": its long-term namespace templates
    (``/facts/{actorId}``, ``/preferences/{actorId}``) and its short-term events
    are both keyed only on ``actorId`` (plus ``sessionId``). Scoping the actor is
    therefore the single lever that separates BOTH stores by agent — long-term
    records land in ``/facts/<agent>__<actor>`` and short-term events are written
    under the same compound actor, so one agent's memory never bleeds into
    another's for the same human.

    Agent ids are uuid4 hex (``[0-9a-f]{32}``). A human actor outside the actorId
    charset (``blank.tan``) is escaped (``_blank_dtan``) — it would otherwise fail
    every Memory call (ListEvents ValidationException) for that user.
    """
    actor = f"{agent_id}{SCOPE_SEP}{base_actor}"
    if ACTOR_ID_RE.match(actor):
        return actor  # every id that was valid keeps its existing memory partition
    return f"{agent_id}{SCOPE_SEP}{encode_actor_part(base_actor)}"


def create_turn_event(
    workspace: WorkspaceContext, actor_id: str, session_id: str, prompt: str, answer: str
) -> dict[str, Any]:
    """Persist one conversation turn into short-term memory (feeds extraction)."""
    return data_client(workspace).create_event(
        memoryId=_memory_id(workspace),
        actorId=actor_id,
        sessionId=session_id,
        eventTimestamp=datetime.now(UTC),
        payload=[
            {"conversational": {"role": "USER", "content": {"text": prompt}}},
            {"conversational": {"role": "ASSISTANT", "content": {"text": answer}}},
        ],
    )


def list_events(
    workspace: WorkspaceContext,
    actor_id: str,
    session_id: str,
    max_results: int = 20,
    memory_id: str | None = None,
) -> list[dict]:
    """Short-term events of one session; ``memory_id`` selects an agent-pinned memory.

    In a dual-read window a session the project memory does not have (written before
    the agent moved) is read from the shared memory."""
    client = data_client(workspace)
    legacy = _dual_read(workspace, memory_id)

    def read(mem_id: str) -> list[dict]:
        return client.list_events(
            memoryId=mem_id,
            actorId=actor_id,
            sessionId=session_id,
            includePayloads=True,
            maxResults=min(max_results, 100),
        ).get("events", [])

    if legacy is None or not legacy.allows_actor(actor_id):
        return read(memory_id or _memory_id(workspace))
    try:
        events = read(_memory_id(workspace))
    except Exception as exc:
        if not _not_found(exc):
            raise
        events = []
    return events or read(legacy.memory_id)


def list_actor_ids(
    workspace: WorkspaceContext, prefix: str | None = None, max_pages: int = 5
) -> list[str]:
    """Every actor id in the memory store, optionally filtered by prefix.

    ListActors has no server-side filter, so the prefix is applied client-side
    while paging (100/page, capped at `max_pages` — the store holds tens of
    actors, and callers only need a bounded candidate set).
    """
    client = data_client(workspace)

    def read(mem_id: str) -> list[str]:
        found: list[str] = []
        token: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"memoryId": mem_id, "maxResults": 100}
            if token:
                params["nextToken"] = token
            page = client.list_actors(**params)
            found += [
                summary["actorId"]
                for summary in page.get("actorSummaries", [])
                if summary.get("actorId")
            ]
            token = page.get("nextToken")
            if not token:
                break
        return found

    actor_ids = read(_memory_id(workspace))
    legacy = _dual_read(workspace, None)
    if legacy is not None:
        # the project's own agents' actors from before the move, once each
        actor_ids = list(dict.fromkeys(actor_ids + legacy.filter_actors(read(legacy.memory_id))))
    if prefix:
        return [actor_id for actor_id in actor_ids if actor_id.startswith(prefix)]
    return actor_ids


def list_records(
    workspace: WorkspaceContext,
    namespace_prefix: str,
    max_results: int = 20,
    memory_id: str | None = None,
) -> list[dict]:
    client = data_client(workspace)

    def read(mem_id: str) -> list[dict]:
        return client.list_memory_records(
            memoryId=mem_id,
            namespacePath=namespace_prefix,
            maxResults=min(max_results, 100),
        ).get("memoryRecordSummaries", [])

    records = read(memory_id or _memory_id(workspace))
    legacy = _dual_read(workspace, memory_id)
    if legacy is not None and legacy.allows_namespace(namespace_prefix):
        records = merge_records(records, read(legacy.memory_id))[: min(max_results, 100)]
    return records


def merge_records(primary: list[dict], legacy: list[dict]) -> list[dict]:
    """Primary records first, then the legacy ones not already listed (by id)."""
    seen = {r.get("memoryRecordId") for r in primary if r.get("memoryRecordId")}
    return primary + [r for r in legacy if r.get("memoryRecordId") not in seen]


def retrieve_records(
    workspace: WorkspaceContext, namespace: str, query: str, top_k: int = 3
) -> list[dict]:
    client = data_client(workspace)

    def read(mem_id: str) -> list[dict]:
        return client.retrieve_memory_records(
            memoryId=mem_id,
            namespace=namespace,
            searchCriteria={"searchQuery": query, "topK": top_k},
        ).get("memoryRecordSummaries", [])

    records = read(_memory_id(workspace))
    legacy = _dual_read(workspace, None)
    if legacy is not None and legacy.allows_namespace(namespace):
        merged = merge_records(records, read(legacy.memory_id))
        records = sorted(merged, key=lambda r: -(r.get("score") or 0))[:top_k]
    return records


def session_memory_summary(
    workspace: WorkspaceContext,
    actor_id: str,
    session_id: str,
    memory_id: str | None = None,
) -> dict[str, Any]:
    """Right-rail panel data: event count + long-term records for the actor.

    ``memory_id`` selects the agent's own memory when its spec pins one — the
    write side (the deployed runtime) targets that memory, so the rail must
    read the same one back.
    """
    events = list_events(workspace, actor_id, session_id, memory_id=memory_id)
    records: list[dict[str, Any]] = []
    # actor_id is already agent-scoped (see scoped_actor); the display label
    # keeps just the strategy — the actor/agent is implied by the session.
    # Facts and preferences are per actor; the summary and the episodes are per
    # session, so those two are read for THIS session only (the namespace is
    # exact, not a prefix, so the actor-level reflections under /episodes/<actor>
    # stay out of the rail). A memory without the strategy lists nothing — the
    # API answers an unknown namespace with an empty page, not an error.
    namespaces = (
        ("/preferences", f"/preferences/{actor_id}"),
        ("/facts", f"/facts/{actor_id}"),
        ("/summaries", f"/summaries/{actor_id}/{session_id}"),
        ("/episodes", f"/episodes/{actor_id}/{session_id}"),
    )
    for label, namespace in namespaces:
        for record in list_records(workspace, namespace, max_results=10, memory_id=memory_id):
            content = record.get("content", {})
            # /preferences, /summaries and /episodes records are structured JSON,
            # /facts records prose — decode so the rail shows a sentence, not a
            # serialized object.
            display, _ = decode_record_text(content.get("text", ""))
            records.append(
                {
                    "namespace": label,
                    "text": display[:200],
                    "record_id": record.get("memoryRecordId"),
                }
            )
    return {
        "event_count": len(events),
        "events": [
            {
                "id": e.get("eventId"),
                "at": str(e.get("eventTimestamp", "")),
                "payload": [
                    {
                        "role": p["conversational"].get("role"),
                        "text": p["conversational"].get("content", {}).get("text", "")[:120],
                    }
                    for p in e.get("payload", [])
                    if "conversational" in p
                ],
            }
            for e in events
        ],
        "records": records,
    }
