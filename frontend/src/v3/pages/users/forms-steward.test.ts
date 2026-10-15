import { describe, expect, it } from "vitest";

import { createBody, emptyCreateForm, isPlatformRole, listFilterFrom, validateCreate } from "./forms";

const valid = { ...emptyCreateForm(), username: "org-admin-1", email: "org-admin-1@acme-corp.com" };

describe("creating and filtering stewards (projects §11.4)", () => {
  it("treats the steward as a platform role: no grant, no membership", () => {
    expect(isPlatformRole("steward")).toBe(true);
    expect(isPlatformRole("admin")).toBe(true);
    expect(isPlatformRole("operator")).toBe(false);
    const form = { ...valid, role: "steward" as const, projects: [{ id: "", role: "" }], groups: [{ id: "g", role: "viewer" }] };
    expect(validateCreate(form)).toEqual({});
    const body = createBody(form);
    expect(body.role).toBe("steward");
    expect(body.workspaces).toEqual([]);
    expect(body).not.toHaveProperty("projects");
    expect(body).not.toHaveProperty("groups");
  });

  it("filters the list by the steward role", () => {
    expect(listFilterFrom(new URLSearchParams("role=steward"), 25).role).toBe("steward");
  });
});
