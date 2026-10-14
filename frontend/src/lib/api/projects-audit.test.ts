import { afterEach, describe, expect, it, vi } from "vitest";

import { projectAuditApi } from "./projects";

describe("project audit API (§11.6)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists with the filters and the page, dropping empty values", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ events: [], next_cursor: null })));
    vi.stubGlobal("fetch", fetchMock);
    await projectAuditApi.list("a/b", { actor: "li.na", outcome: "refused", action: "" }, { limit: 50, cursor: "c|1" });
    await projectAuditApi.list("alpha");
    const urls = fetchMock.mock.calls.map((c) => (c as unknown as [string])[0]);
    expect(urls[0]).toBe("/api/projects/a%2Fb/audit?actor=li.na&outcome=refused&limit=50&cursor=c%7C1");
    expect(urls[1]).toBe("/api/projects/alpha/audit");
  });

  it("builds the export URL and names the file from the response", async () => {
    expect(projectAuditApi.exportUrl("alpha", { from: "2026-10-01T00:00:00.000Z" }, "jsonl")).toBe(
      "/api/projects/alpha/audit/export?from=2026-10-01T00%3A00%3A00.000Z&format=jsonl",
    );
    vi.stubGlobal("fetch", vi.fn(async () => new Response("id,at\n", {
      headers: { "Content-Disposition": 'attachment; filename="audit-alpha-20261015.csv"' },
    })));
    const file = await projectAuditApi.export("alpha");
    expect(file.name).toBe("audit-alpha-20261015.csv");
    expect(await file.blob.text()).toBe("id,at\n");
  });

  it("turns a refusal into an error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ code: "auth.permission_required", message: "needs project.audit" }), { status: 403 },
    )));
    await expect(projectAuditApi.export("alpha")).rejects.toThrow();
  });
});
