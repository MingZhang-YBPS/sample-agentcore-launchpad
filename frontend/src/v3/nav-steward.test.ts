import { describe, expect, it } from "vitest";

import { showsStewardEmptyState } from "../workspace/steward-paths";
import { hostedGroups, STEWARD_ENTRIES } from "./nav";

describe("a steward's rail (projects §11.4)", () => {
  const groups = hostedGroups(false, [], false, true);
  const tos = groups.flatMap((g) => g.items.map((i) => i.to));

  it("is Configure's structure entries alone — no business group", () => {
    expect(groups.map((g) => g.key)).toEqual(["admin"]);
    expect(tos).toEqual(expect.arrayContaining([
      "/v3/users", "/v3/admins", "/v3/projects", "/v3/project-groups", "/v3/departments",
      "/v3/workspaces", "/v3/models", "/v3/network", "/v3/storage",
    ]));
    expect(tos).not.toContain("/v3/fleet");
    expect(tos).not.toContain("/v3/system-update");
    expect(tos).not.toContain("/v3/project-settings");
    expect(tos).toHaveLength(STEWARD_ENTRIES.size);
  });

  it("opens no entry onto the steward's empty state", () => {
    expect(tos.filter((to) => showsStewardEmptyState("steward", to.split("?")[0]))).toEqual([]);
  });

  it("leaves everyone else's rail as it was", () => {
    expect(hostedGroups(true, [], false, false)).toEqual(hostedGroups(true));
    expect(hostedGroups(false, ["project.members"], true, false)).toEqual(hostedGroups(false, ["project.members"], true));
  });
});
