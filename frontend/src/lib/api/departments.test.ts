import { afterEach, describe, expect, it, vi } from "vitest";

import { departmentPath, departmentsApi } from "./departments";

type Call = [string, RequestInit | undefined];

function stubFetch(status = 200, body: unknown = {}) {
  const fetchMock = vi.fn(async () =>
    status === 204 ? new Response(null, { status }) : new Response(JSON.stringify(body), { status }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return () => fetchMock.mock.calls.map((c) => c as unknown as Call);
}

describe("departments API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("encodes every path segment", () => {
    expect(departmentPath()).toBe("/api/departments");
    expect(departmentPath("d 1", "archive")).toBe("/api/departments/d%201/archive");
  });

  it("lists with filters and writes with the right method and body", async () => {
    const calls = stubFetch(200, { departments: [] });
    await departmentsApi.list("all");
    await departmentsApi.update("d1", { name: "D" });
    const [departments, patch] = calls();
    expect(departments[0]).toBe("/api/departments?status=all");
    expect(patch[1]?.method).toBe("PATCH");
  });

  it("a department delete resolves undefined on 204", async () => {
    const calls = stubFetch(204);
    await expect(departmentsApi.remove("d1")).resolves.toBeUndefined();
    expect(calls()[0][1]?.method).toBe("DELETE");
  });

  it("department administrators, open workspaces and invitations (§11.1)", async () => {
    const calls = stubFetch(200, { managed_departments: [{ id: "retail", name: "Retail", workspaces: ["default"] }] });
    await departmentsApi.putAdmins("retail", { add: ["u1"] });
    await departmentsApi.putWorkspaces("retail", { remove: ["lab"] });
    await departmentsApi.invite("retail", { username: "n", email: "n@x.com", project_id: "shop" });
    await departmentsApi.update("retail", { settings: { can_create_users: true, max_projects: null } });
    const mine = await departmentsApi.mine();
    const [admins, workspaces, invite, settings, access] = calls();
    expect(admins[0]).toBe("/api/departments/retail/admins");
    expect(admins[1]?.method).toBe("PUT");
    expect(JSON.parse(String(admins[1]?.body))).toEqual({ add: ["u1"] });
    expect(workspaces[0]).toBe("/api/departments/retail/workspaces");
    expect(invite[0]).toBe("/api/departments/retail/invitations");
    expect(invite[1]?.method).toBe("POST");
    expect(JSON.parse(String(settings[1]?.body))).toEqual({ settings: { can_create_users: true, max_projects: null } });
    expect(access[0]).toBe("/api/me/access");
    expect(mine.map((d) => d.id)).toEqual(["retail"]);
  });
});
