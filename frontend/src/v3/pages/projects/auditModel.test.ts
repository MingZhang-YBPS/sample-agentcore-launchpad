import { describe, expect, it } from "vitest";

import { auditFilter, outcomeSignal, rangeFrom } from "./auditModel";

describe("audit tab helpers (§11.6)", () => {
  it("turns a range into the instant it starts", () => {
    const now = Date.parse("2026-10-15T12:00:00.000Z");
    expect(rangeFrom("1d", now)).toBe("2026-10-14T12:00:00.000Z");
    expect(rangeFrom("30d", now)).toBe("2026-09-15T12:00:00.000Z");
    expect(rangeFrom("all", now)).toBeUndefined();
  });

  it("drops the empty filters", () => {
    expect(auditFilter({ actor: "", action: "", outcome: "" })).toEqual({});
    expect(auditFilter({ actor: "li.na", action: "release.", outcome: "failed", from: "x" })).toEqual({
      actor: "li.na", action: "release.", outcome: "failed", from: "x",
    });
  });

  it("colours the outcomes", () => {
    expect(outcomeSignal("success")).toBe("ok");
    expect(outcomeSignal("refused")).toBe("wait");
    expect(outcomeSignal("failed")).toBe("act");
  });
});
