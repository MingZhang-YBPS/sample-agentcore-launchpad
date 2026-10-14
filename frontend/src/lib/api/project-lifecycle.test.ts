import { afterEach, describe, expect, it, vi } from "vitest";

import { lifecyclePath, projectLifecycleApi } from "./project-lifecycle";

type Call = [string, RequestInit | undefined];

function stubFetch(body: unknown = {}) {
  const fetchMock = vi.fn(
    async () => new Response(JSON.stringify(body), { status: 200 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return () => fetchMock.mock.calls.map((c) => c as unknown as Call);
}

describe("project lifecycle API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("encodes every path segment", () => {
    expect(lifecyclePath("a/b", "delete-request", "cancel")).toBe(
      "/api/projects/a%2Fb/delete-request/cancel",
    );
    expect(projectLifecycleApi.exportUrl("p1", "e1")).toBe(
      "/api/projects/p1/exports/e1",
    );
  });

  it("calls each route with the right method", async () => {
    const calls = stubFetch({});
    await projectLifecycleApi.get("p1");
    await projectLifecycleApi.exportBundle("p1");
    await projectLifecycleApi.requestDeletion("p1");
    await projectLifecycleApi.cancelDeletion("p1");
    await projectLifecycleApi.purge("p1", { dryRun: true });
    await projectLifecycleApi.purge("p1");
    expect(calls().map(([url, init]) => [init?.method ?? "GET", url])).toEqual([
      ["GET", "/api/projects/p1/lifecycle"],
      ["POST", "/api/projects/p1/export"],
      ["POST", "/api/projects/p1/delete-request"],
      ["POST", "/api/projects/p1/delete-request/cancel"],
      ["POST", "/api/projects/p1/purge?dry_run=true"],
      ["POST", "/api/projects/p1/purge"],
    ]);
  });
});
