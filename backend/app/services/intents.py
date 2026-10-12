"""Intent view (roadmap T33): what were people trying to do, and where did the agent fail?

Recent sessions are grouped by the user's first turn into business-readable intent
clusters, each with its volume, a quality signal (thumbs-down rate) and the sessions
behind it. Questions the agent could not answer are surfaced explicitly -- that is the
row an owner acts on, and each one can be sent to the issue box.

**Why not the existing insights clustering.** The `Builtin.Insight.UserIntent` insight
(`evaluation/agentcore_eval.parse_insights`) is an asynchronous AgentCore batch
evaluation over CloudWatch spans: minutes of latency, billed per run, and keyed by
trace sessions rather than the ledger's rows. It cannot back an interactive table of
"the last week of conversations", so this view groups with **one Bedrock Converse call**
(through the client funnel, like `services/suggestions`) over the first turns, cached
per (workspace, agent, window, language, content) for ten minutes.

**Fallback.** When the model call fails or returns something unusable the view still
answers: identical questions (after `answer_rules.normalize`) that recur form a cluster
labelled with the question, everything else lands in one "other" cluster, and
`source` is `"fallback"` so the console says the grouping was mechanical.

"Could not answer" is the union of the model's judgement and a deterministic check (an
empty answer, an error turn, or a refusal phrase in English/Chinese), so the fallback
still finds them.
"""

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.config import Config
from sqlalchemy.orm import Session

from app.models.ledger import Agent, ChatFeedback, ChatMessage, ChatSession
from app.services.answer_rules import normalize
from app.services.workspace import WorkspaceContext

# The model intent clustering runs on — a platform feature, so it does not follow the
# agent default (AgentSpec.DEFAULT_MODEL_ID) and its prompts stay on the model they
# were written for.
PLATFORM_MODEL_ID = "global.anthropic.claude-sonnet-5"

logger = logging.getLogger("launchpad.intents")

MAX_SESSIONS = 150
MAX_CLUSTERS = 12
MAX_SESSIONS_PER_CLUSTER = 50
QUESTION_CHARS = 200
ANSWER_CHARS = 120
# Claude 5 reasons before it answers, and the reasoning counts against maxTokens:
# grouping 70 sessions on the test hub spent all of 1800 on reasoning and returned
# no text (stopReason max_tokens), so every multi-agent view fell back. 3695 tokens
# were used once given room; 8000 leaves headroom for MAX_SESSIONS.
MAX_OUTPUT_TOKENS = 8000
CACHE_TTL_SECONDS = 600.0
FALLBACK_TTL_SECONDS = 60.0
MIN_RECURRING = 2

_NO_ANSWER = re.compile(
    r"i (?:don'?t|do not) know|i(?:'m| am) not sure|i (?:couldn'?t|could not|can'?t|cannot) "
    r"(?:find|answer|help)|unable to (?:find|answer|help)|(?:no|not enough) "
    r"(?:relevant )?information|don'?t have (?:any )?(?:relevant )?information|"
    r"not able to (?:answer|help|find)|beyond my|outside (?:of )?my|"
    r"无法回答|不知道|没有找到|未找到|找不到|没有相关|不清楚|无法提供|无法确定|超出.{0,6}范围"
)
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class Turn:
    """The first exchange of one session."""

    session_id: str
    agent_id: str
    message_id: int | None
    question: str
    answer: str
    errored: bool
    by_rule: bool
    down: int = 0
    up: int = 0

    @property
    def unanswered(self) -> bool:
        if self.by_rule:
            return False
        if self.errored or not self.answer.strip():
            return True
        return bool(_NO_ANSWER.search(normalize_keep(self.answer)))

    @property
    def status(self) -> str:
        if self.down:
            return "down"
        if self.unanswered:
            return "unanswered"
        return "up" if self.up else "ok"


def normalize_keep(text: str) -> str:
    """Casefolded, width-normalised text with punctuation kept (apostrophes matter to the
    English refusal patterns)."""
    import unicodedata

    return unicodedata.normalize("NFKC", text or "").casefold()


def now() -> float:
    return time.monotonic()


# ── gathering ───────────────────────────────────────────────────────────────


def collect_turns(
    db: Session, workspace_id: str, *, agent_id: str | None, days: int,
    limit: int = MAX_SESSIONS,
) -> list[Turn]:
    since = datetime.now(UTC) - timedelta(days=days)
    query = db.query(ChatSession).filter(
        ChatSession.workspace_id == workspace_id, ChatSession.last_at >= since
    )
    if agent_id:
        query = query.filter(ChatSession.agent_id == agent_id)
    sessions = query.order_by(ChatSession.last_at.desc()).limit(limit).all()
    if not sessions:
        return []
    presets = {
        a.id for a in db.query(Agent.id).filter(
            Agent.workspace_id == workspace_id, Agent.system_key.isnot(None)
        )
    }
    sessions = [s for s in sessions if s.agent_id not in presets]
    ids = [s.session_id for s in sessions]
    if not ids:
        return []
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.workspace_id == workspace_id, ChatMessage.session_id.in_(ids),
                ChatMessage.role.in_(("user", "agent", "error")))
        .order_by(ChatMessage.id.asc())
        .all()
    )
    first: dict[str, dict[str, ChatMessage]] = {}
    for row in rows:
        slot = first.setdefault(row.session_id, {})
        # first user turn, then the first agent/error reply after it
        if "user" not in slot:
            if row.role == "user":
                slot["user"] = row
        elif "reply" not in slot and row.role in ("agent", "error"):
            slot["reply"] = row
    votes: dict[str, list[int]] = {}
    for sid, verdict in db.query(ChatFeedback.session_id, ChatFeedback.verdict).filter(
        ChatFeedback.workspace_id == workspace_id, ChatFeedback.session_id.in_(ids)
    ):
        bucket = votes.setdefault(sid, [0, 0])
        bucket[0 if verdict == "down" else 1] += 1

    turns: list[Turn] = []
    for s in sessions:
        slot = first.get(s.session_id) or {}
        user, reply = slot.get("user"), slot.get("reply")
        if user is None or not (user.text or "").strip():
            continue
        down, up = votes.get(s.session_id, [0, 0])
        turns.append(Turn(
            session_id=s.session_id, agent_id=s.agent_id,
            message_id=reply.id if reply is not None and reply.role == "agent" else None,
            question=(user.text or "").strip(), answer=(reply.text or "") if reply else "",
            errored=reply is None or reply.role == "error",
            by_rule=bool(reply is not None and reply.answered_by), down=down, up=up,
        ))
    return turns


# ── grouping ────────────────────────────────────────────────────────────────


def _prompt_lines(turns: list[Turn]) -> str:
    return "\n".join(
        f"[{i}] Q: {t.question[:QUESTION_CHARS]!r} | A: {t.answer[:ANSWER_CHARS]!r}"
        for i, t in enumerate(turns)
    )


def _parse(raw: str, count: int) -> tuple[list[dict[str, Any]], set[int]] | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("clusters"), list):
        return None
    seen: set[int] = set()
    clusters: list[dict[str, Any]] = []
    for item in data["clusters"][:MAX_CLUSTERS]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:80]
        members = [
            m for m in (item.get("members") or [])
            if isinstance(m, int) and not isinstance(m, bool) and 0 <= m < count
            and m not in seen
        ]
        if label and members:
            seen.update(members)
            clusters.append({"label": label, "members": members})
    if not clusters:
        return None
    flagged = {
        m for m in (data.get("unanswered") or [])
        if isinstance(m, int) and not isinstance(m, bool) and 0 <= m < count
    }
    return clusters, flagged


def _model_groups(
    workspace: WorkspaceContext, turns: list[Turn], lang: str
) -> tuple[list[dict[str, Any]], set[int]] | None:
    language = "Simplified Chinese" if lang == "zh-CN" else "English"
    system = (
        "You group the first messages of chat sessions with an AI agent by what the user "
        "was trying to do. Reply with ONLY a JSON object: "
        '{"clusters":[{"label":"...","members":[indexes]}],"unanswered":[indexes]}. '
        f"At most {MAX_CLUSTERS} clusters; labels are short business phrases (max 8 words) "
        f"in {language}, not technical terms. Each index appears in at most one cluster. "
        '"unanswered" lists sessions where the answer failed to actually answer the '
        "question (refusal, no information found, an error). No prose."
    )
    client = workspace.client(
        "bedrock-runtime",
        cache_token="intents;read_timeout=90;max_attempts=1",
        config=Config(read_timeout=90, retries={"max_attempts": 1, "mode": "standard"}),
    )
    resp = client.converse(
        modelId=PLATFORM_MODEL_ID,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": _prompt_lines(turns)}]}],
        # No temperature: Claude 5 rejects it ("`temperature` is deprecated for this
        # model"), which silently pushed every view onto the mechanical fallback.
        inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS},
    )
    blocks = (resp.get("output") or {}).get("message", {}).get("content") or []
    raw = "".join(b["text"] for b in blocks if isinstance(b.get("text"), str))
    return _parse(raw, len(turns))


def fallback_groups(turns: list[Turn]) -> list[dict[str, Any]]:
    """Mechanical grouping: identical (normalised) questions that recur, the rest 'other'.
    The 'other' label is filled by the caller, which knows the language."""
    by_key: dict[str, list[int]] = {}
    for i, t in enumerate(turns):
        by_key.setdefault(normalize(t.question), []).append(i)
    clusters: list[dict[str, Any]] = []
    rest: list[int] = []
    for key, members in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
        if key and len(members) >= MIN_RECURRING and len(clusters) < MAX_CLUSTERS - 1:
            clusters.append({"label": turns[members[0]].question[:80], "members": members})
        else:
            rest.extend(members)
    if rest:
        clusters.append({"label": None, "members": sorted(rest)})
    return clusters


# ── assembly ────────────────────────────────────────────────────────────────


def _session_out(t: Turn) -> dict[str, Any]:
    return {
        "session_id": t.session_id, "agent_id": t.agent_id, "message_id": t.message_id,
        "question": t.question[:QUESTION_CHARS], "answer": t.answer[:ANSWER_CHARS * 2],
        "status": t.status,
    }


def _row(cluster_id: str, label: str, members: list[Turn]) -> dict[str, Any]:
    order = {"down": 0, "unanswered": 1, "ok": 2, "up": 3}
    members = sorted(members, key=lambda t: order[t.status])
    down = sum(1 for t in members if t.down)
    rated = sum(1 for t in members if t.down or t.up)
    unanswered = sum(1 for t in members if t.unanswered)
    return {
        "id": cluster_id,
        "label": label,
        "volume": len(members),
        "thumbs_down": down,
        "rated": rated,
        # None when nobody rated: "0% bad" and "unknown" must not look alike
        "down_rate": round(down / rated, 3) if rated else None,
        "unanswered": unanswered,
        "sessions": [_session_out(t) for t in members[:MAX_SESSIONS_PER_CLUSTER]],
    }


def build_view(
    turns: list[Turn], groups: list[dict[str, Any]], flagged: set[int], *, source: str,
    other_label: str,
) -> dict[str, Any]:
    flagged_turns = {id(turns[i]) for i in flagged}
    rows = []
    placed: set[int] = set()
    for n, group in enumerate(groups):
        members = [turns[i] for i in group["members"] if 0 <= i < len(turns)]
        placed.update(group["members"])
        if members:
            rows.append(_row(f"c{n + 1}", group["label"] or other_label, members))
    leftover = [t for i, t in enumerate(turns) if i not in placed]
    if leftover:
        rows.append(_row(f"c{len(rows) + 1}", other_label, leftover))
    rows.sort(key=lambda r: (-r["volume"], r["label"].lower()))
    unanswered = [
        {**_session_out(t), "cluster": next(
            (r["label"] for r in rows if any(s["session_id"] == t.session_id
                                              for s in r["sessions"])), None)}
        for t in turns
        if (t.unanswered or id(t) in flagged_turns) and not t.by_rule
    ]
    return {
        "source": source,
        "sessions_considered": len(turns),
        "clusters": rows,
        "unanswered": unanswered,
    }


def _fingerprint(turns: list[Turn]) -> str:
    blob = json.dumps([[t.session_id, t.question, t.down, t.up] for t in turns])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def intent_view(
    db: Session, workspace: WorkspaceContext, workspace_id: str, *, agent_id: str | None,
    days: int, lang: str = "en", refresh: bool = False,
) -> dict[str, Any]:
    turns = collect_turns(db, workspace_id, agent_id=agent_id, days=days)
    other = "其他问题" if lang == "zh-CN" else "Other questions"
    if not turns:
        return {**build_view([], [], set(), source="model", other_label=other),
                "generated_at": datetime.now(UTC).isoformat()}
    key = f"{workspace_id}|{agent_id or '*'}|{days}|{lang}|{_fingerprint(turns)}"
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and not refresh:
            ttl = CACHE_TTL_SECONDS if hit[1]["source"] == "model" else FALLBACK_TTL_SECONDS
            if now() - hit[0] < ttl:
                return hit[1]
    parsed = None
    try:
        parsed = _model_groups(workspace, turns, lang)
    except Exception:  # noqa: BLE001 - the page must never 500 because a model call failed
        logger.info("intent grouping model call failed", exc_info=True)
    if parsed:
        view = build_view(turns, parsed[0], parsed[1], source="model", other_label=other)
    else:
        view = build_view(turns, fallback_groups(turns), set(), source="fallback",
                          other_label=other)
    view["generated_at"] = datetime.now(UTC).isoformat()
    with _LOCK:
        stamp = now()
        for stale in [k for k, (ts, _) in _CACHE.items() if stamp - ts >= CACHE_TTL_SECONDS]:
            del _CACHE[stale]
        _CACHE[key] = (stamp, view)
    return view


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()
