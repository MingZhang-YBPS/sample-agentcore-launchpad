import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { ApiError } from "../lib/api";
import { ApiErrorDetail } from "./ApiErrorDetail";

const html = (error: unknown) => renderToStaticMarkup(<ApiErrorDetail error={error} />);

const awsError = (masked: boolean) =>
  new ApiError(
    "aws.validation",
    "headline AWS said: raw",
    { aws_error_code: "ValidationException", operation: "CreateOnlineEvaluationConfig", aws_request_id: "aws-req-9", ...(masked ? { masked: true } : {}) },
    {
      rawMessage: masked ? "role arn:aws:iam::********9012:role/x is invalid" : "role arn:aws:iam::123456789012:role/x is invalid",
      title: "headline",
      requestId: "0123456789abcdef",
    },
  );

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("ApiErrorDetail (projects §19)", () => {
  it("renders nothing for no error", () => {
    expect(html(null)).toBe("");
    expect(html("")).toBe("");
  });

  it("renders a page's own validation string as plain text", () => {
    const out = html("Choose an agent");
    expect(out).toContain("Choose an agent");
    expect(out).not.toContain("<details");
  });

  it("an ApiError without AWS facts or request id shows its message only", () => {
    const out = html(new ApiError("agent.name_exists", "An agent with this name already exists", null));
    expect(out).toContain("An agent with this name already exists");
    expect(out).not.toContain("<details");
  });

  it("an aws.* error shows the headline once and the AWS details collapsed", () => {
    const out = html(awsError(false));
    expect(out).toContain("headline");
    expect(out).not.toContain("AWS said");
    expect(out).toMatch(/<details[^>]*>/);
    expect(out).not.toMatch(/<details[^>]*open/);
    expect(out).toContain(i18n.t("apiErrors.detail.summaryAws"));
    expect(out).toContain("CreateOnlineEvaluationConfig");
    expect(out).toContain("ValidationException");
    expect(out).toContain("aws-req-9");
    expect(out).toContain("role arn:aws:iam::123456789012:role/x is invalid");
    expect(out).toContain(i18n.t("apiErrors.detail.copy"));
  });

  it("shows the request id in the summary line, so it is visible without expanding", () => {
    const summary = /<summary[^>]*>(.*?)<\/summary>/.exec(html(awsError(false)))?.[1] ?? "";
    expect(summary).toContain("0123456789abcdef");
  });

  it("an admin's copy carries no masking note; a member's says the ids are masked", () => {
    const admin = html(awsError(false));
    const member = html(awsError(true));
    expect(admin).not.toContain('data-testid="api-error-masked"');
    expect(member).toContain('data-testid="api-error-masked"');
    expect(member).toContain(i18n.t("apiErrors.detail.masked"));
    expect(member).not.toContain("123456789012");
    expect(member).toContain("********9012");
  });

  it("a request id alone (no AWS code) still opens a generic detail", () => {
    const out = html(new ApiError("workspace.assume_role_failed", "diag", null, { requestId: "rid-7" }));
    expect(out).toContain(i18n.t("apiErrors.detail.summary"));
    expect(out).toContain("rid-7");
  });
});
