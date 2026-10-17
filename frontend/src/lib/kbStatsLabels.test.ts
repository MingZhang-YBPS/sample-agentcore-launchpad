import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";

// BUG-11: the ingestion statistics were shown as raw humanized AWS keys
// ("Documents Scanned") even in the Chinese console.
const KEYS = [
  "numberOfDocumentsScanned", "numberOfMetadataDocumentsScanned", "numberOfNewDocumentsIndexed",
  "numberOfModifiedDocumentsIndexed", "numberOfMetadataDocumentsModified",
  "numberOfDocumentsDeleted", "numberOfDocumentsFailed", "numberOfDocumentsSkipped",
];

describe("knowledge.stats labels", () => {
  it("names every ingestion statistic AWS reports, in both languages", () => {
    for (const k of KEYS) {
      expect((en.knowledge.stats as Record<string, string>)[k]).toBeTruthy();
      expect((zh.knowledge.stats as Record<string, string>)[k]).toMatch(/[\u4e00-\u9fff]/);
    }
  });
});
