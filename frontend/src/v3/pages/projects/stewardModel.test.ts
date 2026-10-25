import { describe, expect, it } from "vitest";

import type { StewardNotice, StewardPending } from "../../../lib/api/steward";
import { changeSummary, stewardTraceFor } from "./stewardModel";

const notice = (over: Partial<StewardNotice>): StewardNotice => ({
  id: "n", kind: "changed", project_id: "shop", project_name: "Shop", actor: "olga", actor_user_id: "u-olga",
  changes: [], pending_id: null, created_at: "2026-10-01T00:00:00Z", read: false, ...over,
});
const pending = (over: Partial<StewardPending>): StewardPending => ({
  id: "p", project_id: "shop", project_name: "Shop", changes: [], requested_by: "olga", requested_by_user_id: "u-olga",
  created_at: "2026-10-01T00:00:00Z", mine: false, can_confirm: true, ...over,
});

describe("a project's steward trace (projects §11.4)", () => {
  it("keeps this project's notices, unread and newest first, and its pending changes oldest first", () => {
    const trace = stewardTraceFor(
      "shop",
      [
        notice({ id: "old-read", read: true, created_at: "2026-10-03T00:00:00Z" }),
        notice({ id: "other", project_id: "risk" }),
        notice({ id: "old", created_at: "2026-10-01T00:00:00Z" }),
        notice({ id: "new", created_at: "2026-10-02T00:00:00Z" }),
      ],
      [pending({ id: "p2", created_at: "2026-10-05T00:00:00Z" }), pending({ id: "p1" }), pending({ id: "px", project_id: "risk" })],
    );
    expect(trace.notices.map((n) => n.id)).toEqual(["new", "old", "old-read"]);
    expect(trace.pending.map((p) => p.id)).toEqual(["p1", "p2"]);
  });

  it("summarizes who a change adds and removes", () => {
    expect(changeSummary([
      { op: "upsert", user_id: "u1", username: "lee", role_key: "developer" },
      { op: "upsert", user_id: "u2", username: null },
      { op: "remove", user_id: "u3", username: "kim" },
    ])).toEqual({ upserts: [{ name: "lee", role: "developer" }, { name: "u2", role: null }], removes: ["kim"] });
  });
});
