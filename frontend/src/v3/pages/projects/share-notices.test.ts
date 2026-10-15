import { describe, expect, it } from "vitest";

import { type ShareNotice, unreadNoticesFor } from "../../../lib/api/sharedFrom";

const notice = (id: string, patch: Partial<ShareNotice> = {}): ShareNotice => ({
  id, kind: "revoked", share_id: `s-${id}`, project_id: "p-1", project_name: "Support",
  owner_project_id: "p-2", owner_project_name: "Platform", resource_kind: "knowledge_base",
  resource_id: "kb-1", resource_name: "Handbook", access: "use", workspace_id: "ws-1",
  pinned_by: [], actor: "alice", created_at: "2026-10-01T00:00:00Z", read: false, ...patch,
});

describe("unreadNoticesFor", () => {
  it("keeps this project's unread notices, newest first", () => {
    const rows = unreadNoticesFor([
      notice("a", { created_at: "2026-10-01T00:00:00Z" }),
      notice("b", { created_at: "2026-10-03T00:00:00Z" }),
      notice("c", { read: true }),
      notice("d", { project_id: "p-9" }),
    ], "p-1");
    expect(rows.map((n) => n.id)).toEqual(["b", "a"]);
  });
});
