import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { ProjectUser, UserAccess } from "../../../lib/api/users";
import { AccessPanel } from "./AccessPanel";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const user = (over: Partial<ProjectUser> = {}): ProjectUser =>
  ({
    id: "u1",
    username: "zhang.san",
    role: "member",
    projects: [
      { id: "risk", name: "Risk assistant", status: "active", is_default: false, role: "release_ops",
        sources: [{ source: "direct", role_key: "release_ops" }, { source: "group:risk-platform", role_key: "developer" }] },
      { id: "default", name: "Default project", status: "active", is_default: true, role: "inherit",
        sources: [{ source: "direct", role_key: "inherit" }] },
    ],
    groups: [{ id: "risk-platform", name: "Risk platform", role: "developer", archived: false }],
    ...over,
  }) as ProjectUser;

const access: UserAccess = {
  username: "zhang.san",
  user_id: "u1",
  role: "member",
  is_admin: false,
  can_manage_structure: false,
  state: "active",
  workspaces: [
    {
      workspace_id: "dev",
      name: "dev",
      bound_projects: 1,
      default_project_id: "risk",
      projects: [{ id: "risk", name: "Risk assistant", status: "active", is_default: false, is_workspace_default: true,
        permissions: ["project.use", "agents.deploy", "release.sign"], role_sources: [], bypass: false, department_id: null, department_name: null }],
    },
  ],
};

describe("user detail access view (projects §7.4)", () => {
  it("renders each project's role, its sources and the reachable workspaces", () => {
    const html = renderToStaticMarkup(<AccessPanel user={user()} access={access} loading={false} error={null} onEdit={() => undefined} />);
    expect(html).toContain('data-testid="v3-user-projects-table"');
    expect(html).toContain("Risk assistant");
    expect(html).toContain("Direct (Release ops)");
    expect(html).toContain("Group “Risk platform” (Developer)");
    expect(html).toContain("Inherits global role");
    // the effective key count comes from /access
    expect(html).toMatch(/>3</);
    expect(html).toContain("Risk platform · Developer");
    expect(html).toContain("via Risk assistant");
    expect(html).toContain("Edit projects and groups");
  });

  it("an administrator reaches every project by role and has no group panel", () => {
    const html = renderToStaticMarkup(<AccessPanel user={user({ role: "admin", projects: [], groups: [] })} access={null} loading error={null} />);
    expect(html).toContain("All (administrator)");
    expect(html).not.toContain('data-testid="v3-user-projects-table"');
    expect(html).not.toContain("Project groups");
  });

  it("an account without projects is told so", () => {
    const html = renderToStaticMarkup(<AccessPanel user={user({ projects: [], groups: [] })} access={{ ...access, workspaces: [] }} loading={false} error={null} />);
    expect(html).toContain("Not a member of any project");
    expect(html).toContain("Not in any project group.");
  });
});
