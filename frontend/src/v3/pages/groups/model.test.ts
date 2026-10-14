import { describe, expect, it } from "vitest";

import type { ProjectSummary } from "../../../lib/api/projects";
import { deletable } from "../departments/model";
import { emptyStructureForm, projectChoices, projectSummary, slugify, validateStructure } from "./model";

const project = (id: string, over: Partial<ProjectSummary> = {}) =>
  ({ id, name: id.toUpperCase(), status: "active", group_id: null, ...over }) as ProjectSummary;

describe("project groups and departments (projects §7.7)", () => {
  it("checks the id on create only, and the name always", () => {
    const form = { ...emptyStructureForm(), id: "risk-platform", name: "Risk platform" };
    expect(validateStructure(form, true)).toEqual({});
    expect(validateStructure({ ...form, id: "Risk" }, true).id).toBe("v3.groups.form.err.id");
    expect(validateStructure({ ...form, id: "x" }, true).id).toBe("v3.groups.form.err.id");
    expect(validateStructure({ ...form, id: "Risk" }, false).id).toBeUndefined();
    expect(validateStructure({ ...form, name: "  " }, false).name).toBe("v3.groups.form.err.name");
  });

  it("suggests an id from the name", () => {
    expect(slugify("Risk Platform Group")).toBe("risk-platform-group");
    expect(slugify("  --A_b  ")).toBe("a-b");
    expect(slugify("风控")).toBe("");
    expect(slugify("x".repeat(40)).length).toBe(32);
  });

  it("offers active projects outside the group, flagging another group's", () => {
    const choices = projectChoices(
      [project("in", { group_id: "g1" }), project("free"), project("other", { group_id: "g2" }), project("old", { status: "archived" })],
      { id: "g1", projects: [{ id: "in", name: "IN", status: "active" }] },
    );
    expect(choices).toEqual([
      { id: "free", name: "FREE", otherGroup: null },
      { id: "other", name: "OTHER", otherGroup: "g2" },
    ]);
  });

  it("summarizes the projects of a row", () => {
    const ps = ["a", "b", "c", "d", "e"].map((id) => ({ id, name: id, status: "active" }));
    expect(projectSummary(ps)).toEqual({ names: "a, b, c", more: 2 });
    expect(projectSummary([])).toEqual({ names: "", more: 0 });
  });

  it("deletes a department only when nothing names it", () => {
    expect(deletable({ project_count: 0, group_count: 0 })).toBe(true);
    expect(deletable({ project_count: 1, group_count: 0 })).toBe(false);
    expect(deletable({ project_count: 0, group_count: 2 })).toBe(false);
  });
});
