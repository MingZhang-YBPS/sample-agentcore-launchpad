import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { ApiError } from "./api";
import { type IngestionJob, jobFailedDocs, jobState, resourceState } from "./knowledgeBases";
import { KB_POLL_MS, kbPollDelay, kbUploadFailure } from "./useKbDetailPoll";

beforeAll(async () => {
  // i18n's languageChanged handler touches <html lang>; node has no document
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.stubGlobal("document", { documentElement: {} });
});

const job = (status: string, failed?: number): IngestionJob => ({
  job_id: "J1",
  status,
  statistics: failed === undefined ? {} : { numberOfDocumentsFailed: failed },
});

describe("ingestion job health", () => {
  it("a COMPLETE job that left documents failed is a warning, not green", () => {
    expect(jobState(job("COMPLETE", 0))).toBe("good");
    expect(jobState(job("COMPLETE", 2))).toBe("warn");
    expect(jobFailedDocs(job("COMPLETE", 2))).toBe(2);
  });

  it("other states keep their own health and a missing stat counts as zero", () => {
    expect(jobState(job("FAILED", 3))).toBe("crit");
    expect(jobState(job("IN_PROGRESS"))).toBe("warn");
    expect(jobFailedDocs(job("COMPLETE"))).toBe(0);
    expect(jobFailedDocs(undefined)).toBe(0);
  });

  it("DELETE_UNSUCCESSFUL reads as a failure", () => {
    expect(resourceState("DELETE_UNSUCCESSFUL")).toBe("crit");
  });
});

describe("upload failures from the proxy get KB-specific copy", () => {
  const err = (code: string) => new ApiError(code, "generic", null);

  it("413 says the file is over the upload limit, 504 that it timed out", () => {
    expect(kbUploadFailure(err("http.413"))).toBe(i18n.t("knowledge.upload.tooLarge"));
    expect(kbUploadFailure(err("http.504"))).toBe(i18n.t("knowledge.upload.timedOut"));
    expect(kbUploadFailure(err("http.413"))).toContain("client_max_body_size");
  });

  it("every other failure keeps its usual message", () => {
    expect(kbUploadFailure(err("kb.no_upload_target"))).toBeNull();
    expect(kbUploadFailure(new TypeError("Failed to fetch"))).toBeNull();
  });
});

describe("the detail poll rides out failed reads", () => {
  it("backs off with the failure streak, capped at four intervals", () => {
    expect(kbPollDelay(0)).toBe(KB_POLL_MS);
    expect(kbPollDelay(1)).toBe(2 * KB_POLL_MS);
    expect(kbPollDelay(10)).toBe(4 * KB_POLL_MS);
  });
});
