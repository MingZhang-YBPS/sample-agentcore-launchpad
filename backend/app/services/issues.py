"""The issue box (roadmap T36): one place a business owner works a problem to its end.

An issue is opened by a thumbs-down (any origin: console, share page, SME review), by
a question the intent view shows the agent could not answer, or by hand. The owner
looks at the session and picks a fix -- a curated answer (`POST
/api/agents/{id}/rules`), documents in the knowledge base, or the evaluation dataset
(`POST /api/eval/datasets/from-sessions`) -- and then marks the issue fixed or won't
fix. **This module performs none of those fixes**: the fix endpoints are the existing
ones, and `add_fix` only records which fix was applied (verifying the reference where
the ledger can). That is what keeps dataset building and knowledge-base upload in one
place each.

Every transition is appended to `history` with who and when, and `created_at ->
resolved_at` is the close time the business measures.
"""

from datetime import UTC, datetime
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.evaluation.models import EvalDataset
from app.models.ledger import Agent, ChatFeedback, ChatMessage
from app.models.selfservice import AnswerRule, Issue

STATUSES = ("open", "fixed", "wont_fix")
KINDS = ("thumbs_down", "unanswered", "review", "manual")
FIX_ACTIONS = ("rule", "kb", "dataset")
PREVIEW = 1500


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _trail(issue: Issue, status: str, actor: str, note: str | None = None) -> None:
    entry: dict[str, Any] = {"status": status, "by": actor[:64], "at": _now().isoformat()}
    if note:
        entry["note"] = note[:500]
    issue.history = [*(issue.history or []), entry]


def _turn(db: Session, workspace_id: str, agent_id: str, session_id: str,
          message_id: int) -> tuple[str, str]:
    """(question, answer) of one answer message: the answer and the user turn before it."""
    answer = db.get(ChatMessage, message_id)
    if (
        answer is None or answer.workspace_id != workspace_id or answer.agent_id != agent_id
        or answer.session_id != session_id or answer.role != "agent"
    ):
        raise NotFoundError("issue.message_not_found", "answer not found")
    question = (
        db.query(ChatMessage.text)
        .filter(ChatMessage.workspace_id == workspace_id,
                ChatMessage.session_id == session_id, ChatMessage.role == "user",
                ChatMessage.id < message_id)
        .order_by(ChatMessage.id.desc())
        .first()
    )
    return (question[0] if question else "")[:PREVIEW], (answer.text or "")[:PREVIEW]


def _existing(db: Session, agent_id: str, message_id: int | None) -> Issue | None:
    if message_id is None:
        return None
    return (
        db.query(Issue)
        .filter(Issue.agent_id == agent_id, Issue.message_id == message_id)
        .first()
    )


def open_from_feedback(db: Session, fb: ChatFeedback) -> Issue | None:
    """Open (or refresh) the issue for a thumbs-down. One issue per answer: a second
    reviewer's down-vote adds their comment to the open issue instead of duplicating it."""
    if fb.verdict != "down":
        return None
    issue = _existing(db, fb.agent_id, fb.message_id)
    if issue is not None:
        if issue.status == "open":
            issue.comment = fb.comment or issue.comment
            issue.correction = fb.correction or issue.correction
            db.commit()
        return issue
    question, answer = _turn(db, fb.workspace_id or "", fb.agent_id, fb.session_id,
                             fb.message_id)
    issue = Issue(
        workspace_id=fb.workspace_id, agent_id=fb.agent_id,
        kind="review" if fb.source == "review" else "thumbs_down",
        session_id=fb.session_id, message_id=fb.message_id, question=question,
        answer=answer, comment=fb.comment, correction=fb.correction,
        opened_by=fb.actor[:64],
    )
    _trail(issue, "open", fb.actor)
    db.add(issue)
    db.commit()
    return issue


def open_manual(
    db: Session, *, workspace_id: str, agent_id: str, session_id: str, message_id: int,
    kind: str, actor: str, note: str | None = None,
) -> Issue:
    """Open an issue from an answer the owner picked (an unanswered question from the
    intent view, or any answer). Idempotent per answer."""
    if kind not in ("unanswered", "manual"):
        raise AppError("issue.invalid_kind", "kind must be 'unanswered' or 'manual'",
                       status_code=422)
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != workspace_id:
        raise NotFoundError("agent.not_found", "agent not found")
    question, answer = _turn(db, workspace_id, agent_id, session_id, message_id)
    issue = _existing(db, agent_id, message_id)
    if issue is not None:
        return issue
    issue = Issue(
        workspace_id=workspace_id, agent_id=agent_id, kind=kind, session_id=session_id,
        message_id=message_id, question=question, answer=answer, comment=note,
        opened_by=actor[:64],
    )
    _trail(issue, "open", actor, note)
    db.add(issue)
    db.commit()
    return issue


def sync_feedback(db: Session, workspace_id: str) -> int:
    """Open an issue for every down-voted answer that has none (the votes cast before
    the issue box existed). Idempotent; returns how many were opened."""
    rows = (
        db.query(ChatFeedback)
        .filter(ChatFeedback.workspace_id == workspace_id, ChatFeedback.verdict == "down")
        .order_by(ChatFeedback.updated_at.desc())
        .limit(200)
        .all()
    )
    opened = 0
    for fb in rows:
        if _existing(db, fb.agent_id, fb.message_id) is not None:
            continue
        try:
            if open_from_feedback(db, fb) is not None:
                opened += 1
        except NotFoundError:  # the answer row is gone; nothing to show
            db.rollback()
    return opened


# ── lifecycle ───────────────────────────────────────────────────────────────


def get_issue(db: Session, workspace_id: str, issue_id: str) -> Issue:
    issue = db.get(Issue, issue_id)
    if issue is None or issue.workspace_id != workspace_id:
        raise NotFoundError("issue.not_found", "issue not found")
    return issue


def resolve(db: Session, issue: Issue, status: str, actor: str,
            note: str | None = None) -> Issue:
    """open -> fixed | wont_fix. Anything else is a 409: a closed issue is reopened first,
    so the trail always shows the reopening."""
    if status not in ("fixed", "wont_fix"):
        raise AppError("issue.invalid_status", "status must be 'fixed' or 'wont_fix'",
                       status_code=422)
    if issue.status != "open":
        raise AppError("issue.invalid_transition",
                       f"an issue that is {issue.status} must be reopened first",
                       status_code=409)
    issue.status = status
    issue.resolved_by = actor[:64]
    issue.resolved_at = _now()
    issue.resolution_note = (note or "").strip()[:1000] or None
    _trail(issue, status, actor, note)
    db.commit()
    return issue


def reopen(db: Session, issue: Issue, actor: str, note: str | None = None) -> Issue:
    if issue.status == "open":
        raise AppError("issue.invalid_transition", "the issue is already open",
                       status_code=409)
    issue.status = "open"
    issue.resolved_by = None
    issue.resolved_at = None
    issue.resolution_note = None
    _trail(issue, "open", actor, note or "reopened")
    db.commit()
    return issue


def add_fix(db: Session, issue: Issue, *, action: str, ref: str | None, actor: str,
            note: str | None = None) -> Issue:
    """Record that a fix was applied *elsewhere*. `rule` and `dataset` references are
    checked against this workspace's ledger; `kb` is free text (the knowledge base lives
    in AWS, and its upload route is the existing one)."""
    if action not in FIX_ACTIONS:
        raise AppError("issue.invalid_fix", f"action must be one of {', '.join(FIX_ACTIONS)}",
                       status_code=422)
    if issue.status != "open":
        raise AppError("issue.invalid_transition",
                       "reopen the issue before recording another fix", status_code=409)
    ref = (ref or "").strip()[:64] or None
    if action == "rule":
        rule = db.get(AnswerRule, ref) if ref else None
        if rule is None or rule.workspace_id != issue.workspace_id \
                or rule.agent_id != issue.agent_id:
            raise NotFoundError("rule.not_found", "curated answer not found")
    elif action == "dataset":
        dataset = db.get(EvalDataset, ref) if ref else None
        if dataset is None or dataset.workspace_id != issue.workspace_id:
            raise NotFoundError("dataset.not_found", "dataset not found")
    issue.fixes = [
        *(issue.fixes or []),
        {"action": action, "ref": ref, "by": actor[:64], "at": _now().isoformat(),
         **({"note": note[:300]} if note else {})},
    ]
    _trail(issue, "open", actor, f"fix: {action}")
    db.commit()
    return issue


# ── read models ─────────────────────────────────────────────────────────────


def issue_out(issue: Issue, agent_names: dict[str, str] | None = None) -> dict[str, Any]:
    closed_in = None
    if issue.resolved_at and issue.created_at:
        closed_in = round(
            (_aware(issue.resolved_at) - _aware(issue.created_at)).total_seconds() / 3600, 2
        )
    return {
        "id": issue.id,
        "agent_id": issue.agent_id,
        "agent_name": (agent_names or {}).get(issue.agent_id, issue.agent_id),
        "kind": issue.kind,
        "status": issue.status,
        "session_id": issue.session_id,
        "message_id": issue.message_id,
        "question": issue.question,
        "answer": issue.answer,
        "comment": issue.comment,
        "correction": issue.correction,
        "opened_by": issue.opened_by,
        "resolved_by": issue.resolved_by,
        "resolution_note": issue.resolution_note,
        "resolved_at": _iso(issue.resolved_at),
        "hours_to_close": closed_in,
        "fixes": issue.fixes or [],
        "history": issue.history or [],
        "created_at": _iso(issue.created_at),
        "updated_at": _iso(issue.updated_at),
    }


def transcript(db: Session, issue: Issue, limit: int = 30) -> list[dict[str, Any]]:
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.workspace_id == issue.workspace_id,
                ChatMessage.agent_id == issue.agent_id,
                ChatMessage.session_id == issue.session_id,
                ChatMessage.role.in_(("user", "agent")))
        .order_by(ChatMessage.id.asc())
        .limit(limit)
        .all()
    )
    return [
        {"id": r.id, "role": r.role, "text": (r.text or "")[:2000],
         "answered_by": r.answered_by, "flagged": r.id == issue.message_id}
        for r in rows
    ]


def list_issues(
    db: Session, workspace_id: str, *, status: str | None = None,
    agent_id: str | None = None, limit: int = 100,
) -> dict[str, Any]:
    base = db.query(Issue).filter(Issue.workspace_id == workspace_id)
    if agent_id:
        base = base.filter(Issue.agent_id == agent_id)
    everything = base.all()
    rows = [i for i in everything if not status or i.status == status]
    rows.sort(key=lambda i: _aware(i.created_at) or _now(), reverse=True)
    rows = rows[:limit]
    names = {
        a.id: a.name
        for a in db.query(Agent).filter(Agent.id.in_({i.agent_id for i in rows} or {""}))
    }
    return {
        "items": [issue_out(i, names) for i in rows],
        "summary": summarize(everything),
    }


def summarize(issues: list[Issue]) -> dict[str, Any]:
    """Counts by state and the close time the business tracks. The median, not the mean:
    one issue left for a quarter should not hide that most close in a day."""
    counts = {s: 0 for s in STATUSES}
    hours: list[float] = []
    now = _now()
    open_ages: list[float] = []
    for i in issues:
        counts[i.status] = counts.get(i.status, 0) + 1
        created = _aware(i.created_at)
        if i.status == "open" and created:
            open_ages.append((now - created).total_seconds() / 3600)
        elif i.resolved_at and created:
            hours.append((_aware(i.resolved_at) - created).total_seconds() / 3600)
    return {
        **counts,
        "total": len(issues),
        "median_hours_to_close": round(median(hours), 2) if hours else None,
        "oldest_open_hours": round(max(open_ages), 2) if open_ages else None,
    }
