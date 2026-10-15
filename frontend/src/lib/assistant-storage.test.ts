import { describe, expect, it } from "vitest";

import { proposalContentFromDraft, proposalDraftFrom } from "./assistant";

describe("a member's proposal edit keeps the base's storage (storage-mounts §8.3)", () => {
  const base = {
    version: 1 as const, name: "orders", system_prompt: "You help.", model_id: "m", tools: [], knowledge_bases: [],
    storage: [{ storage_id: "state", mode: "rw" as const }],
  };

  it("carries the storage list through an edit", () => {
    const next = proposalContentFromDraft(proposalDraftFrom(base), base);
    expect(next.storage).toEqual([{ storage_id: "state", mode: "rw" }]);
  });

  it("adds nothing when the base mounts no storage", () => {
    const next = proposalContentFromDraft(proposalDraftFrom({ ...base, storage: [] }), { ...base, storage: [] });
    expect(next).not.toHaveProperty("storage");
  });
});
