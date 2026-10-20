import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import type { AccessProject, ProjectDetail, ProjectMember } from "../../../lib/api/projects";
import { movableAgents } from "../../../lib/api/project-transfer";
import {
  applyOverrides,
  assignableRoles,
  bindHref,
  filterMembers,
  inUseRows,
  isProjectId,
  countdownParts,
  isProjectTab,
  visibleProjectTabs,
  keySlug,
  memberRowState,
  overridesFor,
  projectHref,
  projectOwners,
  roleKeyError,
  roleKeys,
  settingsDraft,
  settingsPatch,
  showsBindBanner,
  slugFromName,
  sodPairsIn,
  sodRefusal,
  SOD_PAIRS,
} from "./model";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p: string) => readFileSync(resolve(here, p), "utf-8");

function member(over: Partial<ProjectMember> = {}): ProjectMember {
  return {
    user_id: "u1", username: "zhang", email: "z***@corp.com", account_role: "member", status: "active",
    direct: { role_key: "developer", overrides: {}, added_by: null, added_at: null },
    sources: [{ source: "direct", role_key: "developer" }], bypass: false, permissions: [], sod_conflicts: [],
    ...over,
  };
}

function project(over: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    id: "risk", name: "Risk", description: "", department_id: null, status: "active",
    is_default: false, settings: {}, created_by: null, created_by_name: null, created_at: null, updated_at: null,
    archived_at: null, workspaces: [], member_count: 0, agent_count: 0, my_permissions: [], can_manage: false, ...over,
  };
}

const roles = {
  templates: [
    { key: "developer", permissions: ["agents.deploy", "project.use", "project.write"] },
    { key: "viewer", permissions: [] },
  ],
  custom: [{ key: "contractor", name: "Contractor", permissions: ["project.use"], based_on: "developer", members: 1, updated_at: null }],
  inherit: "inherit",
  inherit_available: false,
  agent_keys: ["agents.deploy"],
  project_keys: ["project.use", "project.write"],
};

describe("project ids and role keys", () => {
  it("mirrors the backend id pattern", () => {
    expect(isProjectId("risk-assistant")).toBe(true);
    expect(isProjectId("r")).toBe(false);
    expect(isProjectId("-risk")).toBe(false);
    expect(isProjectId("Risk")).toBe(false);
    expect(isProjectId("a".repeat(33))).toBe(false);
  });

  it("suggests a slug from a name, never an invalid one from ASCII", () => {
    expect(slugFromName("Risk Assistant v2")).toBe("risk-assistant-v2");
    expect(slugFromName("  --Loan  collection--  ")).toBe("loan-collection");
    expect(slugFromName("风控助手")).toBe("");
    expect(isProjectId(slugFromName("x".repeat(40)))).toBe(true);
  });

  it("refuses a custom role key that is malformed or a built-in role", () => {
    expect(roleKeyError("no_delete_dev", ["developer"])).toBeNull();
    expect(roleKeyError("Dev", [])).toBe("pattern");
    expect(roleKeyError("1dev", [])).toBe("pattern");
    expect(roleKeyError("developer", ["developer"])).toBe("builtin");
    expect(roleKeyError("inherit", [])).toBe("builtin");
  });

  it("reads tabs from the URL", () => {
    expect(isProjectTab("members")).toBe(true);
    expect(isProjectTab("audit")).toBe(true);
    expect(isProjectTab("lifecycle")).toBe(false);
    expect(visibleProjectTabs(false)).not.toContain("audit");
    expect(visibleProjectTabs(true)).toContain("audit");
    expect(countdownParts(90061)).toEqual({ d: 1, h: 1, m: 1, s: 1 });
    expect(countdownParts(-5)).toEqual({ d: 0, h: 0, m: 0, s: 0 });
    expect(isProjectTab(null)).toBe(false);
    expect(projectHref("risk")).toBe("/v3/projects?id=risk");
    expect(projectHref("risk", "members")).toBe("/v3/projects?id=risk&tab=members");
    expect(bindHref("prod")).toBe("/v3/projects?bind=prod");
  });
});

describe("roles and overrides", () => {
  it("resolves a role's keys: template, custom, inherit (null)", () => {
    expect(roleKeys(roles, "developer")).toEqual(["agents.deploy", "project.use", "project.write"]);
    expect(roleKeys(roles, "contractor")).toEqual(["project.use"]);
    expect(roleKeys(roles, "inherit")).toBeNull();
    expect(roleKeys(roles, "gone")).toEqual([]);
  });

  it("offers inherit only where the backend does (the default project)", () => {
    expect(assignableRoles(roles).map((r) => r.key)).toEqual(["developer", "viewer", "contractor"]);
    expect(assignableRoles({ ...roles, inherit_available: true }).map((r) => r.key)).toContain("inherit");
  });

  it("sends only what differs from the role, both ways", () => {
    const base = ["agents.deploy", "project.use", "project.write"];
    expect(overridesFor(base, base)).toBeNull();
    expect(overridesFor(base, ["project.use", "project.write", "criteria.sign"])).toEqual({
      "criteria.sign": true, "agents.deploy": false,
    });
  });

  it("round-trips a direct row's overrides", () => {
    const base = ["agents.deploy", "project.use"];
    const overrides = { "agents.deploy": false, "criteria.sign": true };
    const keys = applyOverrides(base, overrides);
    expect(keys).toEqual(["criteria.sign", "project.use"]);
    expect(overridesFor(base, keys)).toEqual(overrides);
  });

  it("flags combined duties like the backend", () => {
    expect(sodPairsIn(["agents.deploy", "release.sign", "eval.run"])).toEqual([["agents.deploy", "release.sign"]]);
    expect(sodPairsIn(["criteria.manage"])).toEqual([]);
    // the same pairs as services/projects.py::SOD_PAIRS
    const backend = read("../../../../../backend/app/services/projects.py");
    const start = backend.indexOf("SOD_PAIRS: tuple");
    const block = backend.slice(start, backend.indexOf("\n)", start));
    const pairs = [...block.matchAll(/\("([a-z.]+)", "([a-z.]+)"\)/g)].map((m) => [m[1], m[2]]);
    expect(pairs).toEqual(SOD_PAIRS.map((p) => [...p]));
  });
});

describe("member rows", () => {
  const opts = { canManage: true, isAdmin: false, me: "owner" };

  it("lets a manager edit a row, not their own", () => {
    expect(memberRowState(member(), opts)).toBe("editable");
    expect(memberRowState(member({ username: "owner" }), opts)).toBe("self");
    expect(memberRowState(member({ username: "owner" }), { ...opts, isAdmin: true })).toBe("editable");
    expect(memberRowState(member(), { ...opts, canManage: false })).toBe("readonly");
  });

  it("filters by role and text", () => {
    const rows = [
      member(),
      member({ user_id: "u2", username: "zhao" }),
      member({ user_id: "u3", username: "li", sources: [{ source: "direct", role_key: "biz_owner" }] }),
    ];
    expect(filterMembers(rows, { role: "developer", q: "" }).map((m) => m.user_id)).toEqual(["u1", "u2"]);
    expect(filterMembers(rows, { role: "all", q: "LI" }).map((m) => m.user_id)).toEqual(["u3"]);
  });

  it("lists the owners", () => {
    const rows = [member(), member({ user_id: "o", sources: [{ source: "direct", role_key: "project_owner" }] })];
    expect(projectOwners(rows).map((m) => m.user_id)).toEqual(["o"]);
  });
});

describe("settings patch", () => {
  const p = project({ name: "Risk", description: "x", settings: { resource_visibility: "shared", sod_strict: false } });

  it("sends nothing when nothing changed", () => {
    expect(settingsPatch(p, settingsDraft(p), true)).toBeNull();
  });

  it("sends an owner only the fields an owner may change", () => {
    const draft = { ...settingsDraft(p), name: "Renamed", description: "y", resource_visibility: "isolated" as const, sod_strict: true };
    expect(settingsPatch(p, draft, false)).toEqual({ description: "y", settings: { resource_visibility: "isolated" } });
  });

  it("sends an administrator structure and sod_strict, never a cleared department", () => {
    const withOrg = project({ department_id: "retail" });
    const draft = { ...settingsDraft(withOrg), name: " New ", department_id: "wealth", sod_strict: true };
    expect(settingsPatch(withOrg, draft, true)).toEqual({
      name: "New", department_id: "wealth", settings: { sod_strict: true },
    });
    expect(settingsPatch(withOrg, { ...settingsDraft(withOrg), department_id: "" }, true)).toBeNull();
  });
});

describe("refusals", () => {
  it("reads the rows a refused unbind left", () => {
    const err = new ApiError("project.workspace_in_use", "in use", { rows: { agents: 2, eval_runs: 5 } });
    expect(inUseRows(err)).toEqual({ agents: 2, eval_runs: 5 });
    expect(inUseRows(new ApiError("project.workspace_default", "x", null))).toBeNull();
    expect(inUseRows(new Error("x"))).toBeNull();
  });

  it("reads who a strict project refused", () => {
    const conflicts = [{ user_id: "u1", username: "zhang", pairs: [["agents.deploy", "release.sign"]] }];
    expect(sodRefusal(new ApiError("project.sod_conflict", "x", { conflicts }))).toEqual(conflicts);
    expect(sodRefusal(new ApiError("project.self_membership", "x", null))).toBeNull();
  });
});

describe("bind banner (§6.2)", () => {
  const none = { projects: [] as AccessProject[], current: null, boundProjects: 0 };

  it("shows for an administrator in a workspace with no bound project only", () => {
    expect(showsBindBanner(true, "prod", none)).toBe(true);
    expect(showsBindBanner(false, "prod", none)).toBe(false);
    expect(showsBindBanner(true, null, none)).toBe(false);
    expect(showsBindBanner(true, "prod", { ...none, boundProjects: 1 })).toBe(false);
  });
});

describe("transfer", () => {
  it("offers neither deleted agents nor platform presets", () => {
    const agents = [
      { id: "a", status: "active" }, { id: "b", status: "deleted" }, { id: "c", status: "active", system: { key: "x" } },
    ] as unknown as Parameters<typeof movableAgents>[0];
    expect(movableAgents(agents).map((a) => a.id)).toEqual(["a"]);
  });
});

describe("i18n", () => {
  const ns = (lang: string) => JSON.parse(read(`../../../locales/${lang}/common.json`)).v3.projects;
  const flat = (o: Record<string, unknown>, p = ""): string[] =>
    Object.entries(o).flatMap(([k, v]) => (v && typeof v === "object" ? flat(v as Record<string, unknown>, `${p}${k}.`) : [`${p}${k}`]));

  it("has the same keys in both languages", () => {
    expect(flat(ns("zh-CN")).sort()).toEqual(flat(ns("en")).sort());
  });

  it("labels every project key the backend defines, mcp.approve included", () => {
    // the two tuples: users.py::AGENT_PERMISSIONS and access.py::PROJECT_PERMISSIONS
    const tuple = (file: string, name: string) => {
      const text = read(`../../../../../backend/app/services/${file}`);
      const start = text.indexOf(`${name} = (`);
      return [...text.slice(start, text.indexOf("\n)", start)).matchAll(/^\s+"([a-z]+\.[a-z]+)",/gm)].map((m) => m[1]);
    };
    const keys = new Set([...tuple("users.py", "AGENT_PERMISSIONS"), ...tuple("access.py", "PROJECT_PERMISSIONS")]);
    expect(keys.size).toBeGreaterThanOrEqual(22);
    expect(keys).toContain("mcp.approve");
    for (const lang of ["en", "zh-CN"]) {
      for (const key of keys) expect(ns(lang).key[keySlug(key)], `${lang}: ${key}`).toBeTruthy();
    }
  });
});
