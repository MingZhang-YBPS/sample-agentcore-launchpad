"""Coding agent ledger: one member's repos and the turns they ran.

The ledger keeps identifiers and derived progress only (parent design §7.1): the
repo's files, git history, conversation (codex rollouts) and event log live on
the repo's runtime session storage, with a durable S3 snapshot; AWS — the
runtime — is the source of truth for what a turn did.

* ``coding_repos`` — private to its creator in every visibility mode. Ownership
  binds to the IMMUTABLE principal (``assistant/principal.principal_of``:
  ``user:<users.id>`` | ``config-admin`` | ``local-operator``), never to a username;
  a NULL principal matches nobody. ``session_id`` is the repo's
  ``runtimeSessionId`` on the platform coding runtime — minted once at create,
  never reused or changed. ``status`` is ``idle`` | ``running`` (one turn at a
  time, claimed with a conditional UPDATE); ``active_turn_id`` is codex's id of
  the running turn (NULL in the short window between the claim and the runtime's
  answer). ``last_seq`` is the watcher's event-log cursor.
* ``coding_turns`` — one row per turn. ``settled_at`` is the idempotency guard of
  turn settlement (usage recorded + snapshot requested): ``UPDATE … SET
  settled_at=… WHERE id=… AND settled_at IS NULL`` — only the winner snapshots.

Earlier releases named the repo table ``coding_projects`` and the turn column
``coding_turns.project_id``; ``core/db._migrate_coding_repo_rename`` renames them in
place on startup. A ledger from a release with per-person budgets may still carry
the unused ``coding_turns.payer_user_id`` / ``payer_name`` columns (nullable, never
read).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now

REPO_IDLE = "idle"
REPO_RUNNING = "running"
# a DELETE in flight (shuts out turn starts; reverted to idle when the delete fails)
REPO_DELETING = "deleting"

TURN_RUNNING = "running"
TURN_COMPLETED = "completed"
TURN_INTERRUPTED = "interrupted"
TURN_FAILED = "failed"
TURN_ABANDONED = "abandoned"
TURN_STATUSES = (TURN_RUNNING, TURN_COMPLETED, TURN_INTERRUPTED, TURN_FAILED, TURN_ABANDONED)

NAME_MAX = 80
DESCRIPTION_MAX = 500
PROMPT_STORED_MAX = 2000


class CodingRepo(Base):
    __tablename__ = "coding_repos"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # Ownership: the immutable principal. Nullable only so a NULL fails closed.
    owner_principal: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    # The creating console principal (`app.services.creators`), display + audit.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str] = mapped_column(String(NAME_MAX))
    description: Mapped[str] = mapped_column(String(DESCRIPTION_MAX), default="")
    # The repo's runtimeSessionId (64 chars) — never reused, never changed.
    session_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=REPO_IDLE)
    active_turn_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # Event-log seqs are 64-bit: a log (re)started on an empty or restored session
    # storage begins at Date.now() * 1000 (~1.8e15, image/src/eventlog.mjs advanceTo).
    # The watcher's event-log cursor (the runtime's `next_after`); readers keep their own.
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    # Event-log seq of the newest settled turn's end record: an idle repo's stream
    # resuming at or past it has nothing to replay, so it never wakes the microVM.
    end_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    # The model / effort of the latest turn (the composer's default next time).
    model: Mapped[str | None] = mapped_column(String(160), default=None)
    effort: Mapped[str | None] = mapped_column(String(16), default=None)
    # The Agent this repo deployed through the byoc pipeline; redeploys update it.
    agent_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # Newest durable snapshot (`coding/<ws>/<repo>/snapshots/<ts>.tgz`).
    snapshot_key: Mapped[str | None] = mapped_column(String(512), default=None)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # A manual save not yet in a snapshot (the watcher snapshots it, debounced).
    dirty_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CodingTurn(Base):
    __tablename__ = "coding_turns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    repo_id: Mapped[str] = mapped_column(String(32), index=True)
    # codex's ids — the event stream's keys
    turn_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    thread_id: Mapped[str | None] = mapped_column(String(64), default=None)
    prompt: Mapped[str] = mapped_column(Text, default="")  # first PROMPT_STORED_MAX chars
    model: Mapped[str] = mapped_column(String(160), default="")
    effort: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default=TURN_RUNNING)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # {input, cached_input, cache_write, output, reasoning_output} (codex counts:
    # input includes cached_input and cache_write; output includes reasoning_output)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # NULL until usage recorded + snapshot requested — the settlement idempotency guard
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
