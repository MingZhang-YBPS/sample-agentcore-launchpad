import { describe, expect, it } from "vitest";

import { projectCan } from "../auth/auth-context";
import type { AccessProject, AccessWorkspace } from "../lib/api/projects";
import {
  groupByDepartment,
  isSwitchable,
  projectRole,
  readProjectLink,
  resolveProject,
  scopeKey,
  showsProjectChip,
  splitByAccess,
} from "./project-context";

const project = (id: string, over: Partial<AccessProject> = {}): AccessProject => ({
  id,
  name: id.toUpperCase(),
  status: "active",
  is_default: false,
  is_workspace_default: false,
  permissions: [],
  role_sources: [{ source: "direct", role_key: "developer" }],
  bypass: false,
  department_id: null,
  department_name: null,
  ...over,
});

const ws = (projects: AccessProject[], defaultId: string | null = null): AccessWorkspace => ({
  workspace_id: "dev",
  name: "Dev",
  default_project_id: defaultId,
  bound_projects: projects.length,
  projects,
});

describe("resolveProject", () => {
  const entry = ws([project("a"), project("b"), project("c")], "b");

  it("takes the first reachable preference (deep link, then stored)", () => {
    expect(resolveProject(entry, ["c", "a"])?.id).toBe("c");
    expect(resolveProject(entry, [null, "a"])?.id).toBe("a");
    expect(resolveProject(entry, ["zzz", "a"])?.id).toBe("a");
  });

  it("falls back to the workspace default, then the first", () => {
    expect(resolveProject(entry, ["gone", null])?.id).toBe("b");
    expect(resolveProject(ws([project("a"), project("b")]), [])?.id).toBe("a");
  });

  it("skips a refused id until the list is re-read", () => {
    expect(resolveProject(entry, ["c"], "c")?.id).toBe("b");
    expect(resolveProject(entry, [], "b")?.id).toBe("a");
  });

  it("is null without a list or projects", () => {
    expect(resolveProject(undefined, ["a"])).toBeNull();
    expect(resolveProject(ws([]), ["a"])).toBeNull();
    expect(resolveProject(ws([project("a")]), [], "a")).toBeNull();
  });
});

describe("readProjectLink", () => {
  it("consumes ?project= and keeps the other parameters", () => {
    expect(readProjectLink("?id=agent-1&project=risk-bot")).toEqual({
      present: true, projectId: "risk-bot", rest: "?id=agent-1",
    });
    expect(readProjectLink("?project=risk-bot")).toEqual({ present: true, projectId: "risk-bot", rest: "" });
  });

  it("drops a malformed id but still consumes it", () => {
    expect(readProjectLink("?project=Not%20Valid&tab=x")).toEqual({ present: true, projectId: null, rest: "?tab=x" });
  });

  it("leaves a search without the parameter untouched", () => {
    expect(readProjectLink("?id=a")).toEqual({ present: false, projectId: null, rest: "?id=a" });
    expect(readProjectLink("")).toEqual({ present: false, projectId: null, rest: "" });
  });
});

describe("switcher helpers", () => {
  it("shows the switcher only with a choice", () => {
    expect(isSwitchable([])).toBe(false);
    expect(isSwitchable([project("a")])).toBe(false);
    expect(isSwitchable([project("a"), project("b")])).toBe(true);
  });

  it("prefers the direct role, null for admin access", () => {
    expect(projectRole(project("a"))).toBe("developer");
    expect(projectRole(project("a", {
      role_sources: [{ source: "group:g1", role_key: "viewer" }, { source: "direct", role_key: "project_owner" }],
    }))).toBe("project_owner");
    expect(projectRole(project("a", { role_sources: [{ source: "group:g1", role_key: "sme" }] }))).toBe("sme");
    expect(projectRole(project("a", { bypass: true, role_sources: [] }))).toBeNull();
    expect(projectRole(project("a", { role_sources: [] }))).toBeNull();
  });

  it("splits member projects from admin-only access", () => {
    const { member, adminAccess } = splitByAccess([project("a"), project("b", { bypass: true }), project("c")]);
    expect(member.map((p) => p.id)).toEqual(["a", "c"]);
    expect(adminAccess.map((p) => p.id)).toEqual(["b"]);
  });

  it("keys the routed subtree by workspace and project", () => {
    expect(scopeKey("dev", "a")).toBe("dev:a");
    expect(scopeKey("dev", null)).toBe("dev:-");
    expect(scopeKey(null, undefined)).toBe("none:-");
  });
});

describe("projectCan", () => {
  const globalCan = (p: string) => p === "agents.deploy";

  it("lets an administrator pass everything", () => {
    expect(projectCan(true, [], globalCan)("eval.run")).toBe(true);
  });

  it("reads the project's keys, replacing the global grants", () => {
    const can = projectCan(false, ["eval.run"], globalCan);
    expect(can("eval.run")).toBe(true);
    // granted globally, not in this project: refused (§2.5)
    expect(can("agents.deploy")).toBe(false);
  });

  it("keeps the global check outside a project", () => {
    expect(projectCan(false, null, globalCan)).toBe(globalCan);
  });
});

describe("groupByDepartment", () => {
  it("groups in first-seen order, projects without a department last", () => {
    const groups = groupByDepartment([
      project("a", { department_id: "risk", department_name: "Risk" }),
      project("b"),
      project("c", { department_id: "ops", department_name: null }),
      project("d", { department_id: "risk", department_name: "Risk" }),
    ]);
    expect(groups.map((g) => [g.id, g.label, g.projects.map((p) => p.id)])).toEqual([
      ["risk", "Risk", ["a", "d"]],
      ["ops", "ops", ["c"]],
      [null, null, ["b"]],
    ]);
  });

  it("is one unlabeled group when no project has a department", () => {
    const groups = groupByDepartment([project("a"), project("b")]);
    expect(groups).toHaveLength(1);
    expect(groups[0].id).toBeNull();
  });
});

describe("showsProjectChip", () => {
  it("shows only for a hidden switcher in a workspace binding several projects", () => {
    expect(showsProjectChip(false, 3, project("a"))).toBe(true);
    expect(showsProjectChip(false, 1, project("a"))).toBe(false);
    expect(showsProjectChip(true, 3, project("a"))).toBe(false);
    expect(showsProjectChip(false, 3, null)).toBe(false);
  });
});
