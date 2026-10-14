import { describe, expect, it } from "vitest";

import type { ConsoleUser } from "../../../lib/api";
import { adminCreateBody, demoteBlock, emptyAdminForm, promotable, validateAdminForm } from "./model";

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
