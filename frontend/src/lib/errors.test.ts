import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { ApiError, messageWithRaw } from "./api";
import { DETAIL_CODES, envelopeText, errorText, errorTitle } from "./errors";

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

describe("codes whose backend message carries the specifics (SG-5b)", () => {
  it("the copy, then the backend's message: the line number survives", async () => {
    const raw = "line 7: Expecting ',' delimiter: line 1 column 12 (char 11)";
    const text = messageWithRaw("dataset.invalid_jsonl", raw);
    expect(text).toBe(i18n.t("apiErrors.detail.withSpecifics", { title: i18n.t("apiErrors.dataset.invalid_jsonl"), raw }));
    expect(text.startsWith(i18n.t("apiErrors.dataset.invalid_jsonl"))).toBe(true);
    expect(text).toContain(raw);
    expect(text).not.toContain("AWS");
    await i18n.changeLanguage("zh-CN");
    const zh = messageWithRaw("dataset.invalid_jsonl", raw);
    expect(zh).toContain("详情：");
    expect(zh).toContain(raw);
  });

  it("the validator's list on an ApiError; the title stays the copy alone", () => {
    const raw = "skills: 'web-search' is not attachable; tools: at most 8";
    const err = new ApiError("assistant.preparation_invalid", raw, null, { rawMessage: raw });
    expect(errorText(err)).toContain(i18n.t("apiErrors.assistant.preparation_invalid"));
    expect(errorText(err)).toContain(raw);
    expect(errorTitle(err)).toBe(i18n.t("apiErrors.assistant.preparation_invalid"));
    expect(envelopeText("registry.skill_invalid", "SKILL.md: missing name")).toContain("SKILL.md: missing name");
  });

  it("no repetition: an empty message or one equal to the copy is the copy alone", () => {
    const copy = i18n.t("apiErrors.custom_tool.invalid");
    expect(messageWithRaw("custom_tool.invalid", "")).toBe(copy);
    expect(messageWithRaw("custom_tool.invalid", copy)).toBe(copy);
  });

  it("other translated codes keep the copy alone (their message is log text)", () => {
    expect(DETAIL_CODES.has("canary.already_running")).toBe(false);
    expect(messageWithRaw("canary.already_running", "row 42 is busy")).toBe(i18n.t("apiErrors.canary.already_running"));
  });

  it("every detail code has console copy in both languages", async () => {
    for (const code of DETAIL_CODES) expect(i18n.exists(`apiErrors.${code}`, { lng: "en" }), code).toBe(true);
    await i18n.changeLanguage("zh-CN");
    for (const code of DETAIL_CODES) expect(i18n.t(`apiErrors.${code}`), code).not.toBe(i18n.t(`apiErrors.${code}`, { lng: "en" }));
  });
});
