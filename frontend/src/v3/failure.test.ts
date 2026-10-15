import { describe, expect, it } from "vitest";

import { ApiError } from "../lib/api";
import { failedWith } from "./failure";

describe("failedWith (an action's failure under the page's headline)", () => {
  it("keeps the envelope — code, request id, next and needs — under the new title", () => {
    const err = new ApiError("release.no_criteria", "no criteria", null, {
      requestId: "rid-1", next: [{ action: "criteria.publish", params: { agent_id: "ag-1" } }], needs: "perm:criteria.manage",
    });
    const out = failedWith("Evaluate failed: no criteria", err);
    expect(out).toBeInstanceOf(ApiError);
    const api = out as ApiError;
    expect(api.title).toBe("Evaluate failed: no criteria");
    expect(api.code).toBe("release.no_criteria");
    expect(api.requestId).toBe("rid-1");
    expect(api.next).toEqual(err.next);
    expect(api.needs).toBe("perm:criteria.manage");
    expect(api.at).toBe(err.at);
  });

  it("anything else becomes the headline alone", () => {
    expect(failedWith("Copy failed")).toBe("Copy failed");
    expect(failedWith("Copy failed", new Error("denied"))).toBe("Copy failed");
  });
});
