import { describe, expect, it } from "vitest";

import { createBody, emptyCreateForm, oneTimePassword, validateCreate, validatePasswordChange } from "./forms";

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
