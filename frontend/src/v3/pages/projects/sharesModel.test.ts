import { describe, expect, it } from "vitest";

import type { ResourceShare } from "../../../lib/api/projects";
import { daysLeft, draftPayload, draftProblems, lifeLeft, lifeSignal, rowActions, type ShareDraft } from "./sharesModel";

const NOW = Date.parse("2026-10-15T00:00:00Z");
const DAY = 86_400_000;
const iso = (offsetDays: number) => new Date(NOW + offsetDays * DAY).toISOString();

function share(over: Partial<ResourceShare> = {}): ResourceShare {
  return {
    id: "s1", kind: "eval_dataset", resource_id: "d1", resource_name: "refunds", workspace_id: "default",
    owner_project_id: "alpha", owner_project_name: "Alpha", grantee_project_id: "beta",
    grantee_project_name: "Beta", access: "read", status: "active", note: "", granted_by: "olga",
    granted_at: iso(-10), approved_by: null, approved_at: null, revoked_by: null, revoked_at: null,
    expires_at: iso(80), ...over,
  };
}

describe("share life", () => {
  it("counts whole days left and never below zero", () => {
    expect(daysLeft(iso(80), NOW)).toBe(80);
    expect(daysLeft(iso(0.2), NOW)).toBe(1);
    expect(daysLeft(iso(-2), NOW)).toBe(0);
    expect(daysLeft(null, NOW)).toBeNull();
  });

  it("measures the remaining fraction of the granted span", () => {
    expect(lifeLeft(share(), NOW)).toBeCloseTo(80 / 90);
    expect(lifeLeft(share({ expires_at: null }), NOW)).toBe(1);
    expect(lifeLeft(share({ expires_at: iso(-1) }), NOW)).toBe(0);
  });

  it("turns amber inside two weeks and red inside three days", () => {
    expect(lifeSignal(share(), NOW)).toBe("ok");
    expect(lifeSignal(share({ expires_at: iso(10) }), NOW)).toBe("wait");
    expect(lifeSignal(share({ expires_at: iso(2) }), NOW)).toBe("act");
    expect(lifeSignal(share({ status: "requested" }), NOW)).toBe("wait");
    expect(lifeSignal(share({ status: "revoked" }), NOW)).toBe("off");
  });
});

describe("row actions", () => {
  it("lets the owner side revoke open shares only", () => {
    expect(rowActions(share(), "outgoing", { share: true, approve: false })).toEqual({ revoke: true, decide: false });
    expect(rowActions(share(), "outgoing", { share: false, approve: true }).revoke).toBe(false);
    expect(rowActions(share({ status: "expired" }), "outgoing", { share: true, approve: false }).revoke).toBe(false);
    expect(rowActions(share(), "incoming", { share: true, approve: true }).revoke).toBe(false);
  });

  it("lets the grantee's approver decide a request", () => {
    const req = share({ status: "requested" });
    expect(rowActions(req, "incoming", { share: false, approve: true }).decide).toBe(true);
    expect(rowActions(req, "incoming", { share: true, approve: false }).decide).toBe(false);
    expect(rowActions(share(), "incoming", { share: false, approve: true }).decide).toBe(false);
  });
});

describe("the grant form", () => {
  const draft: ShareDraft = {
    workspace_id: "default", kind: "knowledge_base", resource_id: " KB1 ", resource_name: "",
    grantee_project_id: "beta", access: "use", days: "90", note: " pilot ",
  };

  it("names what is missing", () => {
    expect(draftProblems(draft, 365)).toEqual([]);
    expect(draftProblems({ ...draft, resource_id: " ", grantee_project_id: "" }, 365)).toEqual(["resource", "grantee"]);
    expect(draftProblems({ ...draft, days: "0" }, 365)).toEqual(["days"]);
    expect(draftProblems({ ...draft, days: "400" }, 365)).toEqual(["days"]);
    expect(draftProblems({ ...draft, days: "1.5" }, 365)).toEqual(["days"]);
  });

  it("sends trimmed values and leaves empty optionals out", () => {
    expect(draftPayload(draft)).toEqual({
      workspace_id: "default", kind: "knowledge_base", resource_id: "KB1", grantee_project_id: "beta",
      access: "use", expires_in_days: 90, note: "pilot",
    });
  });
});
