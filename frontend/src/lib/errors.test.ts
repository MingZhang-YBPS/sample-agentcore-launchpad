import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { ApiError } from "./api";
import { envelopeText, errorText, errorTitle } from "./errors";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("one wording of an error for V2 and V3 (SG-5)", () => {
  const err = new ApiError("canary.already_running", "a canary is already running for this agent", null,
    { rawMessage: "a canary is already running for this agent" });

  it("a code with copy reads the copy, in the current language", async () => {
    expect(errorText(err)).toBe(i18n.t("apiErrors.canary.already_running"));
    await i18n.changeLanguage("zh-CN");
    expect(errorText(err)).toBe("该 Agent 已有金丝雀正在运行。");
    expect(errorTitle(err)).toBe("该 Agent 已有金丝雀正在运行。");
  });

  it("a code without copy keeps the backend's words", () => {
    const raw = new ApiError("nope.unknown_code", "something specific", null);
    expect(errorText(raw)).toBe("something specific");
  });

  it("aws.*: the copy, then AWS's own text; the title is the copy alone", () => {
    const aws = new ApiError("aws.validation", "x", null, { rawMessage: "Value at 'name' failed" });
    expect(errorText(aws)).toContain(i18n.t("apiErrors.aws.validation"));
    expect(errorText(aws)).toContain("Value at 'name' failed");
    expect(errorTitle(aws)).toBe(i18n.t("apiErrors.aws.validation"));
  });

  it("an SSE / job {code, message} pair, and errors that are not ApiError", () => {
    expect(envelopeText("assistant.turn_superseded", "log text")).toBe(i18n.t("apiErrors.assistant.turn_superseded"));
    expect(envelopeText(null, "plain")).toBe("plain");
    expect(errorText(new TypeError("Failed to fetch"))).toBe(i18n.t("apiErrors.network"));
    expect(errorText(new Error("boom"))).toBe("boom");
  });
});
