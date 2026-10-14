import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { api, ApiError, apiErrorDetailText, apiErrorFacts, errorMessage, messageWithRaw } from "./api";

const RAW = "1 validation error detected: Value at 'evaluators.1.member.evaluatorId' failed to satisfy constraint";

function answer(status: number, body: unknown, headers: Record<string, string> = {}) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status, headers })));
}

async function failure(): Promise<ApiError> {
  const err = await api.listAgents().catch((e: unknown) => e);
  expect(err).toBeInstanceOf(ApiError);
  return err as ApiError;
}

beforeAll(async () => {
  // i18n's languageChanged handler touches <html lang>; node has no document
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.stubGlobal("document", { documentElement: {} });
});

describe("ApiError keeps the backend's own words (projects §19)", () => {
  it("aws.validation: the console headline and the AWS text both reach err.message", async () => {
    answer(400, {
      code: "aws.validation",
      message: RAW,
      detail: { aws_error_code: "ValidationException", operation: "CreateOnlineEvaluationConfig" },
      request_id: "0123456789abcdef",
    });
    const err = await failure();
    const title = i18n.t("apiErrors.aws.validation");
    expect(err.title).toBe(title);
    expect(err.rawMessage).toBe(RAW);
    expect(err.message).toContain(title);
    expect(err.message).toContain(RAW);
    expect(errorMessage(err)).toBe(err.message);
    expect(err.requestId).toBe("0123456789abcdef");
  });

  it("zh-CN puts the AWS text after the headline", async () => {
    await i18n.changeLanguage("zh-CN");
    try {
      const text = messageWithRaw("aws.validation", RAW);
      expect(text).toBe(`${i18n.t("apiErrors.aws.validation")}AWS 原文：${RAW}`);
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("a translated non-aws code keeps the console copy alone", async () => {
    answer(409, { code: "agent.name_exists", message: "agent 'x' already exists in ledger", detail: null });
    const err = await failure();
    expect(err.message).toBe(i18n.t("apiErrors.agent.name_exists"));
    expect(err.message).not.toContain("ledger");
    expect(err.rawMessage).toBe("agent 'x' already exists in ledger");
  });

  it("an untranslated code falls back to the backend message", async () => {
    answer(400, { code: "nobody.translated_this", message: "the backend's sentence", detail: null });
    const err = await failure();
    expect(err.message).toBe("the backend's sentence");
    expect(err.title).toBe("the backend's sentence");
  });

  it("an aws code without a message does not repeat the headline", () => {
    expect(messageWithRaw("aws.validation", "")).toBe(i18n.t("apiErrors.aws.validation"));
  });

  it("falls back to the X-Request-Id header when the body has no request_id", async () => {
    answer(502, { code: "workspace.assume_role_failed", message: "trust policy", detail: { aws_error_code: "AccessDenied" } }, { "X-Request-Id": "edge-1" });
    expect((await failure()).requestId).toBe("edge-1");
  });

  it("a public-style envelope (no request id, no aws code) has no detail facts", async () => {
    answer(400, { code: "agent.name_exists", message: "x", detail: null });
    expect(apiErrorFacts(await failure())).toBeNull();
  });
});

describe("apiErrorFacts / apiErrorDetailText", () => {
  const err = new ApiError("aws.access_denied", "headline: raw", {
    aws_error_code: "AccessDeniedException",
    operation: "CreateOnlineEvaluationConfig",
    aws_request_id: "aws-req-1",
    masked: true,
  }, { rawMessage: "arn:aws:iam::********9012:role/x not authorized", title: "headline", requestId: "rid-1" });

  it("reads the AWS facts out of detail", () => {
    expect(apiErrorFacts(err)).toEqual({
      operation: "CreateOnlineEvaluationConfig",
      awsErrorCode: "AccessDeniedException",
      awsRequestId: "aws-req-1",
      masked: true,
    });
  });

  it("copies one key: value per line, raw text last", () => {
    const lines = apiErrorDetailText(err).split("\n");
    expect(lines).toEqual([
      "code: aws.access_denied",
      "operation: CreateOnlineEvaluationConfig",
      "aws_error_code: AccessDeniedException",
      "request_id: rid-1",
      "aws_request_id: aws-req-1",
      `time: ${err.at.toISOString()}`,
      "message: arn:aws:iam::********9012:role/x not authorized",
    ]);
  });

  it("ignores a detail that is not an object", () => {
    expect(apiErrorFacts(new ApiError("aws.validation", "m", ["x"], { requestId: "r" }))).toEqual({
      operation: null, awsErrorCode: null, awsRequestId: null, masked: false,
    });
  });
});
