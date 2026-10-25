"""Hardening from the CodeQL review of PR #336 (console-v3 → main).

Each test pins the behaviour a finding was about, not the scanner's opinion:
the `/v1` stream keeps raw exception text out, the actor-id pattern matches the
same language in linear time, the cookie key is a slow derivation of the admin
password, a skill directory name cannot leave the bundle, and the SSO redirect
lands on a same-origin path whatever reaches it.
"""

import random
import re
import time

from app.deployer import zip_runtime
from app.routers import auth as auth_router
from app.routers.public_api import public_stream_event
from app.services import mcp_reviews, memory
from app.services.sso_oidc import safe_return_to

# ── /v1 SSE errors ──────────────────────────────────────────────────────────────


def test_a_raw_exception_in_the_v1_stream_becomes_a_generic_error():
    raw = {"event": "error", "data": {"message": (
        "ClientError: An error occurred (AccessDeniedException) when calling the "
        "InvokeAgentRuntime operation: User: arn:aws:sts::123456789012:assumed-role/x")}}
    out = public_stream_event(raw)
    assert out["event"] == "error"
    assert out["data"]["code"] == "agent.stream_failed"
    assert "arn:" not in str(out) and "123456789012" not in str(out)


def test_an_aws_error_envelope_gets_the_generic_aws_sentence():
    raw = {"event": "error", "data": {
        "code": "aws.access_denied", "message": "User arn:aws:iam::123456789012:role/r is not",
        "detail": {"operation": "InvokeAgentRuntime"}, "transient": False}}
    out = public_stream_event(raw)["data"]
    assert out == {"code": "aws.access_denied", "message": "AWS access denied", "transient": False}


def test_a_platform_error_keeps_its_code_and_message_but_not_upstream_text():
    raw = {"event": "error", "data": {
        "code": "agent.upstream_error", "message": "the agent's model failed",
        "detail": {"upstream_error": "internalServerException", "retryable": True,
                   "upstream_message": "arn:aws:bedrock:us-east-1:123456789012:..."},
        "next": [{"kind": "console"}]}}
    out = public_stream_event(raw)["data"]
    assert out["code"] == "agent.upstream_error"
    assert out["detail"] == {"upstream_error": "internalServerException", "retryable": True}
    assert "next" not in out


def test_other_events_pass_untouched():
    for event in ({"event": "delta", "data": {"text": "hi"}},
                  {"event": "done", "data": {"latency_ms": 3}}):
        assert public_stream_event(event) is event


# ── actorId pattern ─────────────────────────────────────────────────────────────

_AWS_ACTOR = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_/]*(?::[a-zA-Z0-9\-_/]+)*[a-zA-Z0-9\-_/]*$")


def test_the_actor_pattern_accepts_exactly_what_the_aws_pattern_does():
    rnd = random.Random(7)
    alphabet = "aZ09-_/:.@ "
    for _ in range(20000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
        assert bool(memory.ACTOR_ID_RE.match(s)) == bool(_AWS_ACTOR.match(s)), s


def test_the_actor_pattern_is_fast_on_a_hostile_actor_id():
    hostile = "0" + "-" * 50_000 + "!"
    started = time.monotonic()
    assert memory.ACTOR_ID_RE.match(hostile) is None
    assert time.monotonic() - started < 0.5


def test_tool_name_hints_are_bounded():
    started = time.monotonic()
    mcp_reviews._name_tokens("A" * 100_000 + "a")
    assert time.monotonic() - started < 0.5


# ── cookie signing key ──────────────────────────────────────────────────────────


def test_the_signing_key_is_pbkdf2_of_the_admin_password_and_rotates_with_it():
    import hashlib

    key = auth_router._derive_signing_key("admin", "pw-1")
    plain = hashlib.sha256(b"agentcore-launchpad-session:admin:pw-1").digest()
    assert key != plain and len(key) == 32
    assert key == auth_router._derive_signing_key("admin", "pw-1")
    assert key != auth_router._derive_signing_key("admin", "pw-2")
    assert auth_router._KDF_ITERATIONS >= 100_000


# ── skill bundle directory names ────────────────────────────────────────────────


def test_a_skill_name_that_is_not_a_skill_name_never_becomes_a_directory(tmp_path):
    victim = tmp_path / "keep"
    victim.mkdir()
    (victim / "f").write_text("x")
    logs: list[str] = []

    class _S3:
        def get_paginator(self, _):
            raise AssertionError("must not reach S3 for a rejected name")

    out = zip_runtime._download_named_skills(
        [("../../keep", "s3://b/skills/x/")], tmp_path / "pkg" / "a", logs.append, None,
        s3_client=_S3(),
    )
    assert out["bundled"] == []
    assert (victim / "f").exists()
    assert any("not a valid skill name" in line for line in logs)


# ── SSO return path ─────────────────────────────────────────────────────────────


def test_the_sso_landing_is_always_a_same_origin_path():
    for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "/api/x",
                "javascript:alert(1)", "/ok\r\nSet-Cookie: x"):
        assert safe_return_to(bad) == "/"
    assert safe_return_to("/v3/agents?id=1") == "/v3/agents?id=1"


# ── image attachments reach only their own decoder ──────────────────────────────


def test_an_image_attachment_reaches_only_the_decoder_its_extension_names():
    from io import BytesIO
    from unittest import mock

    import pytest
    from PIL import Image

    from app.core.errors import AppError
    from app.services import attachments

    buf = BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="TIFF")
    with mock.patch("PIL.TiffImagePlugin.TiffImageFile._open",
                    side_effect=AssertionError("TIFF decoder reached")), \
            pytest.raises(AppError):
        attachments._validate_image(buf.getvalue(), ".png", "x.png")
    png = BytesIO()
    Image.new("RGB", (4, 4)).save(png, format="PNG")
    attachments._validate_image(png.getvalue(), ".png", "ok.png")
