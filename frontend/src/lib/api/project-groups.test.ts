import { afterEach, describe, expect, it, vi } from "vitest";

import { departmentPath, departmentsApi, groupPath, projectGroupsApi } from "./project-groups";

type Call = [string, RequestInit | undefined];

function stubFetch(status = 200, body: unknown = {}) {
  const fetchMock = vi.fn(async () =>
    status === 204 ? new Response(null, { status }) : new Response(JSON.stringify(body), { status }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return () => fetchMock.mock.calls.map((c) => c as unknown as Call);
}

describe("project groups and departments API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("encodes every path segment", () => {
    expect(groupPath()).toBe("/api/project-groups");
    expect(groupPath("a/b", "members")).toBe("/api/project-groups/a%2Fb/members");
    expect(departmentPath()).toBe("/api/departments");
    expect(departmentPath("d 1", "archive")).toBe("/api/departments/d%201/archive");
  });

  it("lists with filters and writes with the right method and body", async () => {
    const calls = stubFetch(200, { groups: [] });
    await projectGroupsApi.list({ status: "archived" });
    await projectGroupsApi.putMembers("g1", { upsert: [{ user_id: "u1", role_key: "developer" }] });
    await projectGroupsApi.putProjects("g1", { add: ["p1"] });
    await projectGroupsApi.archive("g1");
    await departmentsApi.list("all");
    await departmentsApi.update("d1", { name: "D" });
    const [list, members, projects, archive, departments, patch] = calls();
    expect(list[0]).toBe("/api/project-groups?status=archived");
    expect(members[0]).toBe("/api/project-groups/g1/members");
    expect(members[1]?.method).toBe("PUT");
    expect(JSON.parse(String(projects[1]?.body))).toEqual({ add: ["p1"] });
    expect(archive[0]).toBe("/api/project-groups/g1");
    expect(archive[1]?.method).toBe("DELETE");
    expect(departments[0]).toBe("/api/departments?status=all");
    expect(patch[1]?.method).toBe("PATCH");
  });

  it("a department delete resolves undefined on 204", async () => {
    const calls = stubFetch(204);
    await expect(departmentsApi.remove("d1")).resolves.toBeUndefined();
    expect(calls()[0][1]?.method).toBe("DELETE");
  });
});
