import { describe, expect, it } from "vitest";

import { AGENT_PERMISSIONS, togglePermission, type AgentPermission } from "./api";

// Effective map as GET /api/users returns it for a member with one extra grant.
const member = Object.fromEntries(AGENT_PERMISSIONS.map((k) => [k, true])) as Record<AgentPermission, boolean>;
for (const k of ["promotion.approve", "criteria.sign", "golden.admit", "judge.calibrate", "waiver.approve", "release.sign"] as const) {
  member[k] = false;
}

describe("togglePermission", () => {
  it("sends every key, so earlier overrides survive the next click", () => {
    const afterFirst = togglePermission(member, "criteria.sign", true);
    const afterSecond = togglePermission(afterFirst, "golden.admit", true);
    expect(Object.keys(afterSecond).sort()).toEqual([...AGENT_PERMISSIONS].sort());
    expect(afterSecond["criteria.sign"]).toBe(true);
    expect(afterSecond["golden.admit"]).toBe(true);
  });

  it("keeps a revoked default revoked when another chip is granted", () => {
    const noDeploy = togglePermission(member, "agents.deploy", false);
    const next = togglePermission(noDeploy, "judge.calibrate", true);
    expect(next["agents.deploy"]).toBe(false);
    expect(next["judge.calibrate"]).toBe(true);
  });

  it("treats a missing key the way the chips display it (granted)", () => {
    expect(togglePermission({}, "eval.run", false)["agents.deploy"]).toBe(true);
  });
});
