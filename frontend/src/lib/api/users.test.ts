import { afterEach, describe, expect, it, vi } from "vitest";

import { userPath, usersApi } from "./users";

type Call = [string, RequestInit | undefined];

function stubFetch(body: unknown = {}) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  return () => fetchMock.mock.calls.map((c) => c as unknown as Call);
}

describe("users API with projects", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("encodes paths and filters", async () => {
    expect(userPath("u/1", "access")).toBe("/api/users/u%2F1/access");
    const calls = stubFetch({ items: [], total: 0, limit: 50, offset: 0 });
    await usersApi.list({ status: "all", project: "alpha", group: "", role: "member" });
    await usersApi.list();
    expect(calls()[0][0]).toBe("/api/users?project=alpha&role=member");
    expect(calls()[1][0]).toBe("/api/users");
  });

  it("approves with projects and bulk-adds with the right bodies", async () => {
    const calls = stubFetch();
    await usersApi.approve("u1", { projects: [{ project_id: "alpha", role_key: "developer" }] });
    await usersApi.bulkAddToProject({ user_ids: ["a", "b"], project_id: "alpha", role_key: "viewer" });
    await usersApi.access("u1");
    const [approve, bulk, access] = calls();
    expect(approve[0]).toBe("/api/users/u1");
    expect(approve[1]?.method).toBe("PATCH");
    expect(JSON.parse(String(approve[1]?.body))).toEqual({
      projects: [{ project_id: "alpha", role_key: "developer" }],
      status: "active",
    });
    expect(bulk[0]).toBe("/api/users/bulk/projects");
    expect(bulk[1]?.method).toBe("POST");
    expect(access[0]).toBe("/api/users/u1/access");
  });
});
