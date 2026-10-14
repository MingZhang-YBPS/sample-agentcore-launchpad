"""Console AWS-error envelopes: request id, AWS request id, member masking (projects §19).

Hermetic: a throwaway FastAPI app with the platform's handlers; a tiny middleware
stands in for the auth gate by putting an identity on ``request.state``.
"""

import logging
import re
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.errors import mask_account_ids, register_error_handlers

RAW = (
    "User: arn:aws:sts::123456789012:assumed-role/LaunchpadExec/i-0abc is not authorized "
    "to perform: bedrock-agentcore:CreateOnlineEvaluationConfig"
)


def aws_error(code: str, message: str, operation: str, aws_request_id: str | None = None):
    response: dict = {"Error": {"Code": code, "Message": message}}
    if aws_request_id:
        response["ResponseMetadata"] = {"RequestId": aws_request_id}
    return ClientError(response, operation)


def make_client(role: str | None = None) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.middleware("http")
    async def fake_identity(request: Request, call_next):
        if role is not None:
            request.state.identity = SimpleNamespace(is_admin=role == "admin")
        return await call_next(request)

    @app.get("/api/aws/{code}")
    def console(code: str):
        raise aws_error(code, RAW, "CreateOnlineEvaluationConfig", "aws-req-1")

    @app.get("/api/no-meta")
    def no_meta():
        raise aws_error("ValidationException", "bad filter key", "CreateOnlineEvaluationConfig")

    @app.get("/v1/aws/{code}")
    def public(code: str):
        raise aws_error(code, RAW, "InvokeAgentRuntime", "aws-req-1")

    @app.get("/share/aws/{code}")
    def share(code: str):
        raise aws_error(code, RAW, "InvokeAgentRuntime", "aws-req-1")

    @app.get("/api/assume-role-denied")
    def assume_role_denied():
        raise aws_error("AccessDenied", "not authorized to perform sts:AssumeRole", "AssumeRole")

    return TestClient(app, raise_server_exceptions=False)


def test_console_envelope_carries_request_id_header_and_aws_request_id():
    res = make_client().get("/api/aws/ValidationException")
    assert res.status_code == 400
    body = res.json()
    assert re.fullmatch(r"[0-9a-f]{16}", body["request_id"])
    assert res.headers["X-Request-Id"] == body["request_id"]
    assert body["code"] == "aws.validation"
    assert body["message"] == RAW
    assert body["detail"] == {
        "aws_error_code": "ValidationException",
        "operation": "CreateOnlineEvaluationConfig",
        "aws_request_id": "aws-req-1",
    }


def test_each_request_gets_its_own_id():
    client = make_client()
    first = client.get("/api/aws/ValidationException").json()["request_id"]
    second = client.get("/api/aws/ValidationException").json()["request_id"]
    assert first != second


def test_no_response_metadata_means_no_aws_request_id():
    body = make_client().get("/api/no-meta").json()
    assert "aws_request_id" not in body["detail"]
    assert body["request_id"]


def test_inbound_request_id_is_reused():
    res = make_client().get("/api/aws/ValidationException", headers={"X-Request-Id": "edge-42.a"})
    assert res.json()["request_id"] == "edge-42.a"
    assert res.headers["X-Request-Id"] == "edge-42.a"


@pytest.mark.parametrize("inbound", ["has space", "x" * 129, "line\\nbreak", "<script>"])
def test_inbound_request_id_of_the_wrong_shape_is_replaced(inbound):
    rid = make_client().get(
        "/api/aws/ValidationException", headers={"X-Request-Id": inbound}
    ).json()["request_id"]
    assert rid != inbound
    assert re.fullmatch(r"[0-9a-f]{16}", rid)


def test_request_id_is_in_the_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="launchpad.errors"):
        rid = make_client().get("/api/aws/ValidationException").json()["request_id"]
    lines = [r.getMessage() for r in caplog.records if r.name == "launchpad.errors"]
    assert any(
        "ValidationException on CreateOnlineEvaluationConfig" in line and f"[request {rid}]" in line
        for line in lines
    )


def test_log_keeps_the_full_text_even_when_the_member_copy_is_masked(caplog):
    with caplog.at_level(logging.INFO, logger="launchpad.errors"):
        make_client("member").get("/api/aws/AccessDeniedException")
    assert any("123456789012" in r.getMessage() for r in caplog.records)


def test_admin_sees_the_aws_text_verbatim():
    body = make_client("admin").get("/api/aws/AccessDeniedException").json()
    assert body["message"] == RAW
    assert "masked" not in body["detail"]


def test_open_console_operator_counts_as_admin():
    # no identity on the request: the auth gate is off, the local operator is admin
    body = make_client(None).get("/api/aws/AccessDeniedException").json()
    assert "123456789012" in body["message"]


def test_member_gets_account_ids_masked():
    body = make_client("member").get("/api/aws/AccessDeniedException").json()
    assert "123456789012" not in body["message"]
    assert "arn:aws:sts::********9012:assumed-role/LaunchpadExec/i-0abc" in body["message"]
    assert body["detail"]["masked"] is True
    assert body["request_id"]


def test_member_text_without_an_account_id_is_not_flagged():
    body = make_client("member").get("/api/no-meta").json()
    assert body["message"] == "bad filter key"
    assert "masked" not in body["detail"]


@pytest.mark.parametrize("prefix", ["/v1", "/share"])
def test_public_envelopes_are_unchanged(prefix):
    """The §19 additions stop at the trust boundary: same snapshot as before."""
    res = make_client("member").get(f"{prefix}/aws/AccessDeniedException")
    assert res.status_code == 403
    assert res.json() == {
        "code": "aws.access_denied",
        "message": "AWS access denied",
        "detail": {"aws_error_code": "AccessDeniedException"},
    }
    assert "X-Request-Id" not in res.headers
    assert "aws-req-1" not in res.text


def test_assume_role_diagnostic_carries_a_request_id():
    res = make_client().get("/api/assume-role-denied")
    assert res.status_code == 502
    body = res.json()
    assert body["code"] == "workspace.assume_role_failed"
    assert body["detail"] == {"aws_error_code": "AccessDenied"}
    assert body["request_id"] == res.headers["X-Request-Id"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("account 123456789012 denied", "account ********9012 denied"),
        ("arn:aws:iam::210987654321:role/x", "arn:aws:iam::********4321:role/x"),
        ("1234567890123 is thirteen digits", "1234567890123 is thirteen digits"),
        ("12345678901 is eleven", "12345678901 is eleven"),
        ("no ids here", "no ids here"),
    ],
)
def test_mask_account_ids(text, expected):
    assert mask_account_ids(text) == expected
