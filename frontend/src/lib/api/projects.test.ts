import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import {
  adminsApi,
  networkProfilePath,
  networkProfilesApi,
  projectPath,
  projectsApi,
  queryString,
} from "./projects";

type Call = [string, RequestInit | undefined];

function stubFetch(status = 200, body: unknown = {}) {
  const fetchMock = vi.fn(async () =>
    status === 204 ? new Response(null, { status }) : new Response(JSON.stringify(body), { status }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return () => fetchMock.mock.calls.map((c) => c as unknown as Call);
}

describe("projects API paths", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("encodes every path segment", () => {
    expect(projectPath()).toBe("/api/projects");
    expect(projectPath("alpha")).toBe("/api/projects/alpha");
    expect(projectPath("a/b", "members", "u 1")).toBe("/api/projects/a%2Fb/members/u%201");
    expect(networkProfilePath()).toBe("/api/network-profiles");
    expect(networkProfilePath("np/1")).toBe("/api/network-profiles/np%2F1");
  });

  it("drops empty query values", () => {
    expect(queryString({})).toBe("");
    expect(queryString({ q: "", status: undefined, group_id: null })).toBe("");
    expect(queryString({ status: "archived", q: "pay ments" })).toBe("?status=archived&q=pay+ments");
  });

  it("lists with filters and writes with the right method and body", async () => {
    const calls = stubFetch(200, { projects: [] });
    await projectsApi.list({ status: "active", q: "x" });
    await projectsApi.update("p1", { name: "P" });
    await projectsApi.putMembers("p1", { upsert: [{ user_id: "u1", role_key: "member" }] });
    await projectsApi.transfer("p1", { resource_id: "a1", to_project_id: "p2" });
    const [list, patch, members, transfer] = calls();
    expect(list[0]).toBe("/api/projects?status=active&q=x");
    expect(patch[0]).toBe("/api/projects/p1");
    expect(patch[1]?.method).toBe("PATCH");
    expect(JSON.parse(String(patch[1]?.body))).toEqual({ name: "P" });
    expect(members[1]?.method).toBe("PUT");
    expect(transfer[0]).toBe("/api/projects/p1/transfer");
    expect(JSON.parse(String(transfer[1]?.body))).toEqual({ kind: "agent", resource_id: "a1", to_project_id: "p2" });
  });

  it("a body-less POST sends no body; a 204 resolves undefined", async () => {
    const calls = stubFetch(204);
    await expect(projectsApi.removeMember("p1", "u1")).resolves.toBeUndefined();
    await expect(networkProfilesApi.archive("np1")).resolves.toBeUndefined();
    stubFetch(200, { ok: true, user_id: "u1", session_epoch: 2 });
    await adminsApi.revokeSessions("u1");
    const [remove, archive] = calls();
    expect(remove[0]).toBe("/api/projects/p1/members/u1");
    expect(remove[1]?.method).toBe("DELETE");
    expect(archive[0]).toBe("/api/network-profiles/np1");
    const revoke = (vi.mocked(fetch).mock.calls[0] as unknown as Call);
    expect(revoke[0]).toBe("/api/users/u1/sessions/revoke");
    expect(revoke[1]?.body).toBeUndefined();
  });

  it("network profiles of a project, archived listing", async () => {
    const calls = stubFetch(200, { profiles: [] });
    await networkProfilesApi.list(true);
    await networkProfilesApi.list();
    await networkProfilesApi.setForProject("p1", { profile_ids: ["np1"], default_profile_id: "np1" });
    const [archived, live, put] = calls();
    expect(archived[0]).toBe("/api/network-profiles?include_archived=true");
    expect(live[0]).toBe("/api/network-profiles");
    expect(put[0]).toBe("/api/network-profiles/projects/p1");
    expect(put[1]?.method).toBe("PUT");
  });

  it("surfaces a refusal as an ApiError with its code", async () => {
    stubFetch(409, { code: "project.role_in_use", message: "2 member(s) still hold role 'x'" });
    const err = await projectsApi.deleteRole("p1", "x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("project.role_in_use");
  });
});
