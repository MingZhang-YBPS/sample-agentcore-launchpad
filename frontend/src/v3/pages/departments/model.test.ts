import { describe, expect, it } from "vitest";

import { parseCount, settingsForm, settingsPatch, unopened } from "./model";

describe("department administrators (projects §11.1)", () => {
  it("round-trips the settings and fills the form from the defaults", () => {
    const form = settingsForm({ can_create_users: false, max_projects: null, user_validity_max_days: 90 });
    expect(form).toEqual({ canCreateUsers: false, maxProjects: "", validityDays: "90" });
    expect(settingsPatch({ ...form, canCreateUsers: true, maxProjects: "5" })).toEqual({
      settings: { can_create_users: true, max_projects: 5, user_validity_max_days: 90 },
    });
    expect(settingsPatch(form)).toEqual({ settings: { can_create_users: false, max_projects: null, user_validity_max_days: 90 } });
  });

  it("refuses counts that are not whole numbers in range", () => {
    expect(parseCount("", 10, true)).toBeNull();
    expect(parseCount("", 10)).toBeUndefined();
    expect(parseCount("0", 10)).toBeUndefined();
    expect(parseCount("2.5", 10)).toBeUndefined();
    expect(parseCount("11", 10)).toBeUndefined();
    expect(parseCount(" 7 ", 10)).toBe(7);
    const bad = settingsPatch({ canCreateUsers: false, maxProjects: "x", validityDays: "" });
    expect(bad).toEqual({ errors: { maxProjects: "v3.departments.settings.err.count", validityDays: "v3.departments.settings.err.days" } });
  });

  it("offers only the workspaces not opened yet, by name", () => {
    const all = [{ id: "b", name: "Beta" }, { id: "a", name: "Alpha" }, { id: "c", name: "Gamma" }];
    expect(unopened(all, [{ workspace_id: "c" }]).map((w) => w.id)).toEqual(["a", "b"]);
  });
});
