"""Suggested test questions for an agent's try-chat panel (roadmap T09).

Derived from what the agent actually has — system prompt, mounted knowledge-base
document names and tools — by one small Bedrock Converse call. Every failure or
empty input degrades to generic questions: the panel must never see a 500.
Results are cached in-process (pattern of ``services/observability.cached``) so
the panel's polling does not re-bill the model.
"""

import json
import logging
import re
import threading
import time
from typing import Any

from botocore.config import Config

from app.services import knowledge
from app.services.workspace import WorkspaceContext

# The model answer suggestions runs on — a platform feature, so it does not follow the
# agent default (AgentSpec.DEFAULT_MODEL_ID) and its prompts stay on the model they
# were written for.
PLATFORM_MODEL_ID = "global.anthropic.claude-sonnet-5"

logger = logging.getLogger("launchpad.suggestions")

MAX_QUESTIONS = 5
MIN_QUESTIONS = 3
MAX_QUESTION_CHARS = 140
MAX_OUTPUT_TOKENS = 300
CACHE_TTL_SECONDS = 600.0
FALLBACK_TTL_SECONDS = 60.0  # do not hammer a failing model on every poll
PROMPT_CHARS = 2000

FALLBACKS: dict[str, list[str]] = {
    "en": [
        "What can you help me with?",
        "Give me an example of a task you handle well.",
        "What information do you need from me to get started?",
    ],
    "zh-CN": [
        "你可以帮我做什么？",
        "举一个你最擅长处理的任务示例。",
        "开始之前你需要我提供哪些信息？",
    ],
}

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _fallback(lang: str) -> list[str]:
    return list(FALLBACKS.get(lang, FALLBACKS["en"]))


def _clean(raw: Any) -> list[str]:
    """Normalise model output to 3-5 short unique strings ([] when unusable)."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        q = " ".join(item.split())
        if q and len(q) <= MAX_QUESTION_CHARS and q not in out:
            out.append(q)
    return out[:MAX_QUESTIONS] if len(out) >= MIN_QUESTIONS else []


def _parse(text: str) -> list[str]:
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        return _clean(json.loads(match.group(0)))
    except ValueError:
        return []


def _tool_names(spec: dict[str, Any]) -> list[str]:
    names = [t.get("name") for t in spec.get("tools") or [] if isinstance(t, dict)]
    names += [str(n) for n in spec.get("toolkits") or []]
    names += [str(n) for n in spec.get("native_tools") or []]
    return [n for n in names if n]


def _kb_context(workspace: WorkspaceContext, spec: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for kb in (spec.get("knowledge_bases") or [])[:2]:
        if not isinstance(kb, dict) or not kb.get("kb_id"):
            continue
        try:
            docs = knowledge.sample_document_names(workspace, kb["kb_id"])
        except Exception:  # noqa: BLE001 - grounding is best effort
            logger.info("kb document sample failed for %s", kb["kb_id"], exc_info=True)
            docs = []
        label = kb.get("name") or kb["kb_id"]
        lines.append(f"Knowledge base '{label}': {kb.get('description', '')}".strip())
        if docs:
            lines.append("  documents: " + ", ".join(docs))
    return lines


def _generate(workspace: WorkspaceContext, spec: dict[str, Any], lang: str) -> list[str]:
    parts: list[str] = []
    prompt = str(spec.get("system_prompt") or "").strip()
    if prompt:
        parts.append("System prompt:\n" + prompt[:PROMPT_CHARS])
    parts += _kb_context(workspace, spec)
    tools = _tool_names(spec)
    if tools:
        parts.append("Tools: " + ", ".join(tools[:15]))
    if not parts:
        return []
    language = "Simplified Chinese" if lang == "zh-CN" else "English"
    system = (
        "You write example questions a new user could ask an AI agent to try it out. "
        f"Reply with ONLY a JSON array of 4 short questions (max 15 words each) in {language}, "
        "each answerable using the agent's described role, documents or tools. No prose."
    )
    client = workspace.client(
        "bedrock-runtime",
        cache_token="suggest;read_timeout=20;max_attempts=1",
        config=Config(read_timeout=20, retries={"max_attempts": 1, "mode": "standard"}),
    )
    resp = client.converse(
        modelId=PLATFORM_MODEL_ID,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": "\n\n".join(parts)}]}],
        # No temperature: Claude 5 rejects it ("`temperature` is deprecated for this
        # model"), which silently replaced every suggestion with the static fallback.
        inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS},
    )
    blocks = (resp.get("output") or {}).get("message", {}).get("content") or []
    return _parse("".join(b["text"] for b in blocks if isinstance(b.get("text"), str)))


def suggested_questions(
    workspace: WorkspaceContext,
    agent_id: str,
    spec: dict[str, Any],
    lang: str = "en",
    *,
    force: bool = False,
) -> dict[str, Any]:
    """``{"questions": [...], "source": "model"|"fallback"}`` — never raises."""
    fingerprint = json.dumps(
        [spec.get("system_prompt"), spec.get("knowledge_bases"), _tool_names(spec)],
        sort_keys=True, default=str,
    )
    key = f"{agent_id}|{lang}|{hash(fingerprint)}"
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and not force:
            ttl = CACHE_TTL_SECONDS if hit[1]["source"] == "model" else FALLBACK_TTL_SECONDS
            if _now() - hit[0] < ttl:
                return hit[1]
    try:
        questions = _generate(workspace, spec, lang)
    except Exception:  # noqa: BLE001 - the panel must never 500
        logger.info("suggested questions model call failed for %s", agent_id, exc_info=True)
        questions = []
    value = (
        {"questions": questions, "source": "model"}
        if questions
        else {"questions": _fallback(lang), "source": "fallback"}
    )
    with _LOCK:
        now = _now()
        for stale in [k for k, (ts, _) in _CACHE.items() if now - ts >= CACHE_TTL_SECONDS]:
            _CACHE.pop(stale, None)
        _CACHE[key] = (now, value)
    return value
