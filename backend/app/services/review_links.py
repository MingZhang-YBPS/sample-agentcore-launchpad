"""SME review links (roadmap T34): a domain expert who is not a console user rates real
answers.

This reuses the T13 share-link primitive rather than inventing a second one: a
`ShareLink` row with `kind="review"`, so the token is hashed at rest, every unusable
state (unknown, malformed, wrong kind, disabled, revoked, expired, agent gone or in
another workspace) is the same 404 `share.not_found`, and the row -- not any header --
names the workspace. Two differences, both deliberate:

* the rate limit is its own bucket (`review_limiter`, 60 ratings burst then one per
  second, per link). A reviewer works through a queue quickly; the chat limiter's
  12-per-minute ceiling is sized for a model call, and sharing the bucket would let a
  review link starve a chat link of the same id space.
* the reviewer sees **real user questions**. For an agent with the PII guardrail on, the
  questions are screened in `anonymize` mode before they leave the server (the answers
  were already screened on the way out). Screening fails open, like the rest of T12; a
  link is a reviewer's credential and should be issued to a person you trust with the
  transcript, which the label records.

Submissions land in `chat_feedback` through `feedback.record_feedback` -- the same store
the console thumbs and the share page write, with `source="review"` and the reviewer's
`correction` text -- so one pipeline (bad cases -> datasets, the issue box) consumes all
three.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.ledger import Agent, ChatFeedback, ChatMessage, ShareLink
from app.services import guardrail, share_links
from app.services.share_links import TokenBucket
from app.services.workspace import get_workspace_row, workspace_context

KIND_REVIEW = "review"  # ShareLink.kind of a reviewer link
QUEUE_SIZE = 25
TEXT_CHARS = 1500
REVIEW_ACTOR_PREFIX = "review_"

review_limiter = TokenBucket(capacity=60, refill_per_sec=1.0)


def enforce_rate_limit(link_id: str) -> None:
    wait = review_limiter.take(link_id)
    if wait:
        # same envelope as the chat limiter
        from app.core.errors import AppError

        retry_after = max(1, int(wait) + 1)
        raise AppError(
            "share.rate_limited", "too many requests — please slow down",
            {"retry_after_seconds": retry_after}, status_code=429,
            headers={"Retry-After": str(retry_after)},
        )


def reviewer_actor(link_id: str) -> str:
    return f"{REVIEW_ACTOR_PREFIX}{link_id}"


def create(db: Session, *, workspace_id: str, agent: Agent, label: str, created_by: str,
           expires_in_days: int | None) -> tuple[ShareLink, str]:
    return share_links.create_link(
        db, workspace_id=workspace_id, agent=share_links.shareable_agent(agent),
        label=label, created_by=created_by, expires_in_days=expires_in_days,
        kind=KIND_REVIEW,
    )


def resolve(db: Session, raw_token: str) -> tuple[ShareLink, Agent]:
    return share_links.resolve(db, raw_token, kinds=(KIND_REVIEW,))


def _mask(db: Session, link: ShareLink, agent: Agent, texts: list[str]) -> list[str]:
    cfg = guardrail.guardrail_config(agent.spec)
    if not cfg:
        return texts
    row = get_workspace_row(db, link.workspace_id or "")
    if row is None:
        raise share_links.not_found()
    ctx = workspace_context(row)
    return [guardrail.screen(t, source="INPUT", mode="anonymize", workspace=ctx).text
            for t in texts]


def queue(db: Session, link: ShareLink, agent: Agent) -> list[dict[str, Any]]:
    """Recent answers of the agent, newest first, unrated-by-this-reviewer ones on top."""
    answers = (
        db.query(ChatMessage)
        .filter(ChatMessage.workspace_id == link.workspace_id,
                ChatMessage.agent_id == agent.id, ChatMessage.role == "agent")
        .order_by(ChatMessage.id.desc())
        .limit(QUEUE_SIZE * 2)
        .all()
    )
    answers = [a for a in answers if (a.text or "").strip()]
    mine = {
        f.message_id: f
        for f in db.query(ChatFeedback).filter(
            ChatFeedback.workspace_id == link.workspace_id,
            ChatFeedback.actor == reviewer_actor(link.id),
            ChatFeedback.message_id.in_([a.id for a in answers] or [0]),
        )
    }
    answers.sort(key=lambda a: (a.id in mine, -a.id))
    answers = answers[:QUEUE_SIZE]
    questions: list[str] = []
    for a in answers:
        q = (
            db.query(ChatMessage.text)
            .filter(ChatMessage.workspace_id == link.workspace_id,
                    ChatMessage.session_id == a.session_id, ChatMessage.role == "user",
                    ChatMessage.id < a.id)
            .order_by(ChatMessage.id.desc())
            .first()
        )
        questions.append((q[0] if q else "")[:TEXT_CHARS])
    questions = _mask(db, link, agent, questions)
    out = []
    for a, question in zip(answers, questions, strict=True):
        given = mine.get(a.id)
        out.append({
            "message_id": a.id,
            "question": question,
            "answer": (a.text or "")[:TEXT_CHARS],
            "curated": bool(a.answered_by),
            "verdict": given.verdict if given else None,
            "comment": given.comment if given else None,
            "correction": given.correction if given else None,
        })
    return out


def answer_session(db: Session, link: ShareLink, agent: Agent, message_id: int) -> str:
    """The session id of an answer of this link's agent, or the opaque 404. The reviewer
    never holds a session id; the server derives it, so a link cannot address another
    agent's (or workspace's) messages."""
    row = db.get(ChatMessage, message_id)
    if (
        row is None or row.role != "agent" or row.agent_id != agent.id
        or row.workspace_id != link.workspace_id
    ):
        raise NotFoundError("review.item_not_found", "answer not found")
    return row.session_id
