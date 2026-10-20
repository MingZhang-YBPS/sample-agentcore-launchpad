import { describe, expect, it } from "vitest";

import { deletable, emptyStructureForm, parseCount, settingsForm, settingsPatch, slugify, unopened, validateStructure } from "./model";

describe("departments (projects §7.10)", () => {
  it("checks the id on create only, and the name always", () => {
    const form = { ...emptyStructureForm(), id: "risk-platform", name: "Risk platform" };
    expect(validateStructure(form, true)).toEqual({});
    expect(validateStructure({ ...form, id: "Risk" }, true).id).toBe("v3.structure.form.err.id");
    expect(validateStructure({ ...form, id: "x" }, true).id).toBe("v3.structure.form.err.id");
    expect(validateStructure({ ...form, id: "Risk" }, false).id).toBeUndefined();
    expect(validateStructure({ ...form, name: "  " }, false).name).toBe("v3.structure.form.err.name");
  });

  it("suggests an id from the name", () => {
    expect(slugify("Risk Platform Group")).toBe("risk-platform-group");
    expect(slugify("  --A_b  ")).toBe("a-b");
    expect(slugify("风控")).toBe("");
    expect(slugify("x".repeat(40)).length).toBe(32);
  });

  it("deletes a department only when no project names it", () => {
    expect(deletable({ project_count: 0 })).toBe(true);
    expect(deletable({ project_count: 1 })).toBe(false);
  });
});

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
