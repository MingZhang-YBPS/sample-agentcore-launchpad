"""No runtime path hard-codes a Bedrock model id (the model-switching guarantee).

Accounts without OpenAI or Anthropic access must be able to switch every model the
platform uses, so a model id literal in backend/app is only allowed where
tests/model_id_allowlist.txt says why (the settings defaults, label tables, docs).
"""

import io
import re
import tokenize
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
ALLOWLIST = Path(__file__).with_name("model_id_allowlist.txt")
TEMPLATE_SUFFIXES = {".tmpl", ".txt", ".yml", ".yaml", ".json"}

_FAMILIES = r"(anthropic|openai|zai|amazon|meta|mistral|deepseek|qwen|moonshotai|xai)"
MODEL_ID = re.compile(
    rf"(?<![\w.-])(?:(?:global|us|eu|apac|jp|au|ca)\.{_FAMILIES}\.[a-z0-9][\w.:-]*"
    r"|(?:anthropic|openai)\.[a-z][\w.:-]*)"
)
# skip hosts / module paths that merely look like ids
_NOT_AN_ID = re.compile(r"amazon\.(com|dev)|amazonaws|openai\.(com|azure)|anthropic\.com")


def _allowlist() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for line in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        path, literal, reason = (part.strip() for part in line.split("|", 2))
        assert reason, f"allowlist entry without a reason: {line!r}"
        out.setdefault(path, set()).add(literal)
    return out


def _python_literals(path: Path):
    source = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.STRING or getattr(tokenize, "FSTRING_MIDDLE", -1) == tok.type:
            yield tok.start[0], tok.string


def _template_lines(path: Path):
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.lstrip().startswith("#"):  # template comments are documentation
            yield no, line


def _hits() -> list[tuple[str, int, str]]:
    hits = []
    for path in sorted(APP.rglob("*")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        if path.suffix == ".py":
            chunks = _python_literals(path)
        elif path.suffix in TEMPLATE_SUFFIXES or path.name == "Dockerfile":
            chunks = _template_lines(path)
        else:
            continue
        rel = path.relative_to(BACKEND).as_posix()
        for no, text in chunks:
            for match in MODEL_ID.finditer(text):
                if not _NOT_AN_ID.search(match.group(0)):
                    hits.append((rel, no, match.group(0).rstrip(".:")))
    return hits


def test_no_hard_coded_model_ids_outside_the_allowlist():
    allow = _allowlist()
    offending = [
        f"{rel}:{no}: {literal}"
        for rel, no, literal in _hits()
        if not ({"*", literal} & allow.get(rel, set()))
    ]
    assert not offending, (
        "hard-coded Bedrock model ids in runtime source — read them from settings "
        "(app/core/config.py + app/services/model_settings.ROLES) or allowlist them "
        "with a reason in tests/model_id_allowlist.txt:\n" + "\n".join(offending)
    )


def test_the_scanner_catches_a_literal():
    assert MODEL_ID.search('model = "global.anthropic.claude-sonnet-5"')
    assert MODEL_ID.search("'us.zai.glm-5.3'")
    assert MODEL_ID.search('"openai.gpt-oss-120b-1:0"')
    assert not MODEL_ID.search("https://bedrock.us-east-1.amazonaws.com")


def test_every_allowlist_entry_is_still_needed():
    hits = {(rel, literal) for rel, _, literal in _hits()}
    files = {rel for rel, _ in hits}
    stale = [
        f"{path} | {literal}"
        for path, literals in _allowlist().items()
        for literal in literals
        if (literal == "*" and path not in files)
        or (literal != "*" and (path, literal) not in hits)
    ]
    assert not stale, "allowlist entries that match nothing any more:\n" + "\n".join(stale)
