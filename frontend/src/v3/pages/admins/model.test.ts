import { describe, expect, it } from "vitest";

import type { ConsoleUser } from "../../../lib/api";
import { adminCreateBody, demoteBlock, emptyAdminForm, platformType, promotable, validateAdminForm } from "./model";

const user = (over: Partial<ConsoleUser>) => ({ id: "u", username: "u", role: "member", ...over }) as ConsoleUser;

describe("administrators page (projects §7.3)", () => {
  it("explains why demote is unavailable", () => {
    expect(demoteBlock({ builtin: true, can_demote: false })).toBe("builtin");
    expect(demoteBlock({ builtin: false, can_demote: false, last_signable: true })).toBe("last");
    expect(demoteBlock({ builtin: false, can_demote: false, last_signable: false })).toBe("self");
    expect(demoteBlock({ builtin: false, can_demote: true, last_signable: false })).toBeNull();
  });

  it("promotes only registered non-admin accounts", () => {
    const list = [user({ id: "a", role: "admin" }), user({ id: "b" }), user({ id: "c", builtin: true, role: "admin" })];
    expect(promotable(list).map((u) => u.id)).toEqual(["b"]);
  });

  it("creates an administrator without grants or memberships", () => {
    const form = { ...emptyAdminForm(), username: "ops-admin", email: "ops-admin@acme-corp.com", projects: [{ id: "x", role: "viewer" }] };
    expect(validateAdminForm(form, "admin")).toEqual({});
    expect(validateAdminForm({ ...form, username: "admin" }, "admin").username).toBe("v3.users.create.err.reserved");
    expect(adminCreateBody(form)).toEqual({
      username: "ops-admin", email: "ops-admin@acme-corp.com", role: "admin", workspaces: [], never_expires: true,
    });
  });
});

describe("stewards on the administrators page (projects §11.4)", () => {
  it("types a row and keeps a viewing steward's hands off", () => {
    expect(platformType({ role: "steward" as ConsoleUser["role"] })).toBe("steward");
    expect(platformType({ role: "admin" })).toBe("admin");
    expect(demoteBlock({ builtin: false, can_demote: false, last_signable: false }, false)).toBe("manage");
  });

  it("never offers an administrator or a steward for promotion", () => {
    const list = [user({ id: "s", role: "steward" as ConsoleUser["role"] }), user({ id: "m" })];
    expect(promotable(list).map((u) => u.id)).toEqual(["m"]);
  });

  it("creates a steward without grants or memberships", () => {
    const form = { ...emptyAdminForm(), username: "ops-steward", email: "ops-steward@acme-corp.com" };
    expect(adminCreateBody(form, "steward")).toEqual({
      username: "ops-steward", email: "ops-steward@acme-corp.com", role: "steward", workspaces: [], never_expires: true,
    });
  });
});
