"""Rule overrides / curated answers (roadmap T35).

A business owner fixes one specific question without touching the prompt or
redeploying: an ordered per-agent list of rules, each with a curated answer. The invoke
chain (`services/invoke.invoke_agent_text` and `services/chat.chat_stream`) asks
`match_for_agent` before dispatching; a hit short-circuits the model call and the
response is marked as answered by a rule.

Matching is deliberately deterministic, never fuzzy or semantic. A wrong curated answer
to a *similar but different* question is worse than falling through to the model, so a
rule only fires on text the owner can read and predict:

* `exact`    -- the whole question equals the pattern after normalisation.
* `contains` -- the normalised pattern occurs in the normalised question (whole words
  for Latin text, plain substring for CJK, which has no spaces).

Normalisation is Unicode NFKC (full-width becomes ASCII), case-folding, punctuation
stripped and whitespace collapsed, so "What's the leave policy?" and "whats the LEAVE
policy" are one question and a full-width Chinese question mark does not defeat a rule.
The first *enabled* rule in `position` order wins, so a specific rule sits above a
general one. Rules are skipped for turns that carry attachments: the question then
depends on the file, and a canned answer would ignore it.

Order relative to the T12 PII screen: the prompt is screened first, then rules are
matched against the screened text, then the curated answer is returned **without**
output screening. A `block`-mode agent therefore still refuses a PII-bearing prompt even
when a rule could answer it, and an `anonymize`-mode agent matches on the masked text.
The answer is owner-authored and audited, and masking it would break the very contact
details an owner writes into an HR answer on purpose.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent
from app.models.selfservice import AnswerRule, AnswerRuleSet

logger = logging.getLogger("launchpad.answer_rules")

MATCH_TYPES = ("exact", "contains")
MAX_RULES_PER_AGENT = 100
MAX_PATTERN = 500
MAX_ANSWER = 8000
MAX_NAME = 64
# a "contains" pattern shorter than this would catch far more than its author meant
MIN_CONTAINS_CHARS = 3
MIN_CONTAINS_CJK_CHARS = 2
ANSWERED_BY_PREFIX = "rule:"

_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)
_CJK = re.compile("[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    name: str
    answer: str

    @property
    def answered_by(self) -> str:
        return f"{ANSWERED_BY_PREFIX}{self.rule_id}"

    def event(self) -> dict[str, Any]:
        return {"event": "rule", "data": {"rule_id": self.rule_id, "name": self.name}}


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(_PUNCT.sub(" ", folded).split())


def matches(rule_match: str, pattern: str, question: str) -> bool:
    p, q = normalize(pattern), normalize(question)
    if not p or not q:
        return False
    if rule_match == "exact":
        return p == q
    if _CJK.search(p):
        return p in q
    return f" {p} " in f" {q} "


def validate_pattern(match: str, pattern: str) -> str:
    """The pattern to store (stripped), or a 422 that says what is wrong."""
    if match not in MATCH_TYPES:
        raise AppError("rule.invalid_match", "match must be 'exact' or 'contains'",
                       status_code=422)
    text = (pattern or "").strip()
    norm = normalize(text)
    if not norm:
        raise AppError("rule.empty_pattern", "the question to match is empty", status_code=422)
    if len(text) > MAX_PATTERN:
        raise AppError("rule.pattern_too_long", "the question to match is too long",
                       status_code=422)
    if match == "contains":
        floor = MIN_CONTAINS_CJK_CHARS if _CJK.search(norm) else MIN_CONTAINS_CHARS
        if len(norm.replace(" ", "")) < floor:
            raise AppError(
                "rule.pattern_too_short",
                "a 'contains' rule needs a longer phrase, or it would match too much",
                status_code=422,
            )
    return text


def validate_answer(answer: str) -> str:
    text = (answer or "").strip()
    if not text:
        raise AppError("rule.empty_answer", "the curated answer is empty", status_code=422)
    if len(text) > MAX_ANSWER:
        raise AppError("rule.answer_too_long", "the curated answer is too long",
                       status_code=422)
    return text


# ── the hot path ────────────────────────────────────────────────────────────


def match_for_agent(agent: Agent, prompt: str) -> RuleHit | None:
    """The first enabled rule that answers `prompt`, or None. Never raises: a broken
    rule table must not take a working agent offline, so any error is logged and the
    turn falls through to the model."""
    if not prompt or not agent.workspace_id:
        return None
    db = None
    try:
        db = SessionLocal()
        rule_set = (
            db.query(AnswerRuleSet)
            .filter(AnswerRuleSet.workspace_id == agent.workspace_id,
                    AnswerRuleSet.agent_id == agent.id)
            .first()
        )
        if rule_set is not None and not rule_set.enabled:
            return None
        rules = (
            db.query(AnswerRule)
            .filter(AnswerRule.workspace_id == agent.workspace_id,
                    AnswerRule.agent_id == agent.id, AnswerRule.enabled.is_(True))
            .order_by(AnswerRule.position, AnswerRule.created_at)
            .all()
        )
        for rule in rules:
            if matches(rule.match, rule.pattern, prompt):
                rule.hit_count = (rule.hit_count or 0) + 1
                rule.last_hit_at = datetime.now(UTC)
                hit = RuleHit(rule.id, rule.name, rule.answer)
                db.commit()
                logger.info("answered by rule agent=%s rule=%s", agent.id, rule.id)
                return hit
        return None
    except Exception:  # noqa: BLE001 - the model path is the fallback
        logger.warning("rule matching failed for agent %s", agent.id, exc_info=True)
        return None
    finally:
        if db is not None:
            db.close()


# ── management (console) ────────────────────────────────────────────────────


def rule_out(rule: AnswerRule) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()

    return {
        "id": rule.id,
        "agent_id": rule.agent_id,
        "position": rule.position,
        "name": rule.name,
        "match": rule.match,
        "pattern": rule.pattern,
        "answer": rule.answer,
        "enabled": bool(rule.enabled),
        "created_by": rule.created_by,
        "updated_by": rule.updated_by,
        "source_issue_id": rule.source_issue_id,
        "hit_count": rule.hit_count or 0,
        "last_hit_at": iso(rule.last_hit_at),
        "created_at": iso(rule.created_at),
        "updated_at": iso(rule.updated_at),
    }


def manageable_agent(db: Session, workspace_id: str, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != workspace_id or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    if getattr(agent, "system_key", None):
        raise AppError("rule.agent_not_supported",
                       "system-managed presets cannot carry curated answers", status_code=409)
    return agent


def _rules(db: Session, workspace_id: str, agent_id: str) -> list[AnswerRule]:
    return (
        db.query(AnswerRule)
        .filter(AnswerRule.workspace_id == workspace_id, AnswerRule.agent_id == agent_id)
        .order_by(AnswerRule.position, AnswerRule.created_at)
        .all()
    )


def agent_enabled(db: Session, workspace_id: str, agent_id: str) -> bool:
    row = (
        db.query(AnswerRuleSet)
        .filter(AnswerRuleSet.workspace_id == workspace_id, AnswerRuleSet.agent_id == agent_id)
        .first()
    )
    return True if row is None else bool(row.enabled)


def list_rules(db: Session, workspace_id: str, agent_id: str) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "enabled": agent_enabled(db, workspace_id, agent_id),
        "rules": [rule_out(r) for r in _rules(db, workspace_id, agent_id)],
        "limit": MAX_RULES_PER_AGENT,
    }


def create_rule(
    db: Session, *, workspace_id: str, agent_id: str, name: str, match: str, pattern: str,
    answer: str, enabled: bool, actor: str, source_issue_id: str | None = None,
) -> AnswerRule:
    pattern = validate_pattern(match, pattern)
    answer = validate_answer(answer)
    existing = _rules(db, workspace_id, agent_id)
    if len(existing) >= MAX_RULES_PER_AGENT:
        raise AppError("rule.limit_reached",
                       f"an agent can carry at most {MAX_RULES_PER_AGENT} curated answers",
                       status_code=409)
    rule = AnswerRule(
        workspace_id=workspace_id, agent_id=agent_id,
        position=(max((r.position for r in existing), default=-1) + 1),
        name=(name or "").strip()[:MAX_NAME] or pattern[:MAX_NAME],
        match=match, pattern=pattern, answer=answer, enabled=enabled,
        created_by=actor[:64], updated_by=actor[:64], source_issue_id=source_issue_id,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def get_rule(db: Session, workspace_id: str, agent_id: str, rule_id: str) -> AnswerRule:
    rule = db.get(AnswerRule, rule_id)
    if rule is None or rule.workspace_id != workspace_id or rule.agent_id != agent_id:
        raise NotFoundError("rule.not_found", "curated answer not found")
    return rule


def update_rule(
    db: Session, rule: AnswerRule, *, actor: str, name: str | None = None,
    match: str | None = None, pattern: str | None = None, answer: str | None = None,
    enabled: bool | None = None,
) -> AnswerRule:
    new_match = match if match is not None else rule.match
    new_pattern = pattern if pattern is not None else rule.pattern
    if match is not None or pattern is not None:
        rule.pattern = validate_pattern(new_match, new_pattern)
        rule.match = new_match
    if answer is not None:
        rule.answer = validate_answer(answer)
    if name is not None:
        rule.name = name.strip()[:MAX_NAME] or rule.pattern[:MAX_NAME]
    if enabled is not None:
        rule.enabled = enabled
    rule.updated_by = actor[:64]
    db.commit()
    db.refresh(rule)
    return rule


def reorder(db: Session, workspace_id: str, agent_id: str, ids: list[str]) -> None:
    rules = _rules(db, workspace_id, agent_id)
    if sorted(ids) != sorted(r.id for r in rules):
        raise AppError("rule.reorder_mismatch",
                       "reorder must list every curated answer of the agent exactly once",
                       status_code=422)
    by_id = {r.id: r for r in rules}
    for position, rule_id in enumerate(ids):
        by_id[rule_id].position = position
    db.commit()


def set_agent_enabled(db: Session, workspace_id: str, agent_id: str, enabled: bool,
                      actor: str) -> None:
    row = (
        db.query(AnswerRuleSet)
        .filter(AnswerRuleSet.workspace_id == workspace_id, AnswerRuleSet.agent_id == agent_id)
        .first()
    )
    if row is None:
        row = AnswerRuleSet(workspace_id=workspace_id, agent_id=agent_id)
        db.add(row)
    row.enabled = enabled
    row.updated_by = actor[:64]
    db.commit()


def dry_run(db: Session, workspace_id: str, agent_id: str, question: str) -> dict[str, Any]:
    """Which rule *would* answer this question -- no counters bumped, no model call.
    Lets an owner verify a fix before customers see it."""
    enabled = agent_enabled(db, workspace_id, agent_id)
    hit = None
    if enabled:
        for rule in _rules(db, workspace_id, agent_id):
            if rule.enabled and matches(rule.match, rule.pattern, question):
                hit = rule
                break
    return {
        "agent_enabled": enabled,
        "matched": hit is not None,
        "rule": rule_out(hit) if hit else None,
    }
