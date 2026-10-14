import { describe, expect, it } from "vitest";

import {
  approveBody,
  createBody,
  directProjectRows,
  emptyApproveForm,
  emptyCreateForm,
  groupRows,
  listFilterFrom,
  oneTimePassword,
  sourceGroup,
  suggestedRole,
  validateApprove,
  validateCreate,
  validateMemberships,
  validatePasswordChange,
} from "./forms";

const valid = { ...emptyCreateForm(), username: "ws-attendee-01", email: "ws-attendee-01@acme-corp.com" };

describe("validateCreate", () => {
  it("accepts a filled form with a generated password", () => {
    expect(validateCreate(valid, "admin")).toEqual({});
  });

  it("flags the username, email and the reserved built-in name", () => {
    expect(validateCreate({ ...valid, username: "ab" }).username).toBe("v3.users.create.err.username");
    expect(validateCreate({ ...valid, username: "-bad" }).username).toBe("v3.users.create.err.username");
    expect(validateCreate({ ...valid, username: "Admin" }, "admin").username).toBe("v3.users.create.err.reserved");
    expect(validateCreate({ ...valid, email: "nope" }).email).toBe("v3.users.create.err.email");
  });

  it("checks a chosen password only when one is being set", () => {
    expect(validateCreate({ ...valid, passwordChoice: "set", password: "short" }).password).toBe("v3.users.create.err.password");
    expect(validateCreate({ ...valid, passwordChoice: "set", password: "long-enough" }).password).toBeUndefined();
    expect(validateCreate({ ...valid, passwordChoice: "generate", password: "x" }).password).toBeUndefined();
  });

  it("bounds custom validity days", () => {
    expect(validateCreate({ ...valid, expiry: "days", days: "0" }).days).toBe("v3.users.create.err.days");
    expect(validateCreate({ ...valid, expiry: "days", days: "3651" }).days).toBe("v3.users.create.err.days");
    expect(validateCreate({ ...valid, expiry: "days", days: "1.5" }).days).toBe("v3.users.create.err.days");
    expect(validateCreate({ ...valid, expiry: "days", days: "30" }).days).toBeUndefined();
  });
});

describe("createBody", () => {
  it("defaults: generated password, default workspace, default validity", () => {
    expect(createBody(valid)).toEqual({
      username: "ws-attendee-01",
      email: "ws-attendee-01@acme-corp.com",
      role: "member",
      workspaces: ["default"],
    });
  });

  it("maps the password, expiry and admin role", () => {
    const body = createBody({ ...valid, role: "admin", passwordChoice: "set", password: "long-enough", expiry: "never", workspaces: ["default", "default"] });
    expect(body).toMatchObject({ role: "admin", password: "long-enough", never_expires: true, workspaces: [] });
    expect(createBody({ ...valid, expiry: "days", days: "14" }).valid_days).toBe(14);
    expect(createBody({ ...valid, workspaces: ["default", "default", "b"] }).workspaces).toEqual(["default", "b"]);
  });
});

describe("validatePasswordChange", () => {
  it("needs the current password, policy length, a change and a matching confirmation", () => {
    expect(validatePasswordChange({ current: "", next: "new-pass-1", confirm: "new-pass-1" }).current).toBeDefined();
    expect(validatePasswordChange({ current: "old-pass-1", next: "short", confirm: "short" }).next).toBe("v3.users.create.err.password");
    expect(validatePasswordChange({ current: "same-pass-1", next: "same-pass-1", confirm: "same-pass-1" }).next).toBe("v3.account.password.err.same");
    expect(validatePasswordChange({ current: "old-pass-1", next: "new-pass-1", confirm: "new-pass-2" }).confirm).toBe("v3.account.password.err.mismatch");
    expect(validatePasswordChange({ current: "old-pass-1", next: "new-pass-1", confirm: "new-pass-1" })).toEqual({});
  });
});

describe("oneTimePassword", () => {
  it("shows a generated password once and nothing for a provided one", () => {
    expect(oneTimePassword({ username: "u", generated_password: "Xy7!abc" }, "created")).toEqual({ username: "u", password: "Xy7!abc", kind: "created" });
    expect(oneTimePassword({ username: "u" }, "created")).toBeNull();
  });
});

describe("memberships (projects §7.4, §7.5)", () => {
  it("flags incomplete and duplicate rows", () => {
    expect(validateMemberships([{ id: "alpha", role: "developer" }], "projects")).toBeNull();
    expect(validateMemberships([{ id: "alpha", role: "" }], "projects")).toBe("v3.users.memberships.err.projectsIncomplete");
    expect(validateMemberships([{ id: "g1", role: "viewer" }, { id: "g1", role: "developer" }], "groups")).toBe("v3.users.memberships.err.groupsDuplicate");
    expect(validateCreate({ ...valid, projects: [{ id: "", role: "developer" }] }).projects).toBe("v3.users.memberships.err.projectsIncomplete");
    // an administrator's rows are ignored (it holds none)
    expect(validateCreate({ ...valid, role: "admin", projects: [{ id: "", role: "" }] }).projects).toBeUndefined();
  });

  it("sends projects and groups only when chosen, never for an administrator", () => {
    expect(createBody(valid)).not.toHaveProperty("projects");
    const body = createBody({ ...valid, projects: [{ id: "alpha", role: "developer" }], groups: [{ id: "risk", role: "viewer" }] });
    expect(body.projects).toEqual([{ project_id: "alpha", role_key: "developer" }]);
    expect(body.groups).toEqual([{ group_id: "risk", role_key: "viewer" }]);
    expect(createBody({ ...valid, role: "admin", projects: [{ id: "alpha", role: "developer" }] })).not.toHaveProperty("projects");
  });

  it("approves as before without choices, with projects and a role change when made", () => {
    const pending = { role: "member" as const, workspaces: [] };
    const form = emptyApproveForm(pending);
    expect(form.workspaces).toEqual(["default"]);
    expect(approveBody(form, "member")).toEqual({ status: "active", workspaces: ["default"] });
    expect(approveBody({ ...form, role: "operator", projects: [{ id: "alpha", role: "release_ops" }] }, "member")).toEqual({
      status: "active",
      workspaces: ["default"],
      role: "operator",
      projects: [{ project_id: "alpha", role_key: "release_ops" }],
    });
    expect(validateApprove({ ...form, groups: [{ id: "", role: "" }] }).groups).toBe("v3.users.memberships.err.groupsIncomplete");
  });

  it("edits direct rows only, leaving the default project's inherit row to the grants", () => {
    const user = {
      projects: [
        { id: "default", name: "Default", status: "active" as const, is_default: true, role: "inherit", sources: [{ source: "direct", role_key: "inherit" }] },
        { id: "alpha", name: "Alpha", status: "active" as const, is_default: false, role: "developer", sources: [{ source: "direct", role_key: "release_ops" }, { source: "group:risk", role_key: "developer" }] },
        { id: "beta", name: "Beta", status: "active" as const, is_default: false, role: "viewer", sources: [{ source: "group:risk", role_key: "viewer" }] },
      ],
    };
    expect(directProjectRows(user)).toEqual([{ id: "alpha", role: "release_ops" }]);
    expect(groupRows({ groups: [{ id: "risk", name: "Risk", role: "developer", archived: false }] })).toEqual([{ id: "risk", role: "developer" }]);
    expect(sourceGroup("group:risk")).toBe("risk");
    expect(sourceGroup("direct")).toBeNull();
  });

  it("suggests the template of the global role", () => {
    const options = [{ key: "project_owner" }, { key: "developer" }, { key: "release_ops" }];
    expect(suggestedRole(options)).toBe("developer");
    expect(suggestedRole(options, "operator")).toBe("release_ops");
    expect(suggestedRole([{ key: "viewer" }])).toBe("viewer");
    expect(suggestedRole([])).toBe("");
  });
});

describe("listFilterFrom", () => {
  it("reads the URL filters and ignores unknown values", () => {
    expect(listFilterFrom(new URLSearchParams("status=pending&role=operator&project=alpha&group=risk&q=li&page=3"), 25)).toEqual({
      q: "li", status: "pending", role: "operator", project: "alpha", group: "risk", limit: 25, offset: 50, page: 3,
    });
    expect(listFilterFrom(new URLSearchParams("status=bogus&role=root"), 25)).toEqual({
      q: undefined, status: "all", role: undefined, project: undefined, group: undefined, limit: 25, offset: 0, page: 1,
    });
  });
});
