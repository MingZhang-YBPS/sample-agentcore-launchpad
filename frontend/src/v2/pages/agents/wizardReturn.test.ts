import { describe, expect, it } from "vitest";

import { wizardBackTarget, wizardDoneTarget } from "./wizardReturn";

describe("wizard return (v3-agent-management §4)", () => {
  it("a publish lands on the V3 detail in V3, on V2's own detail otherwise", () => {
    expect(wizardDoneTarget("v3", "a 1")).toBe("/v3/agents?id=a%201");
    expect(wizardDoneTarget("v2", "a1")).toBeNull();
    expect(wizardDoneTarget("v1", "a1")).toBeNull();
  });

  it("back in V3 returns to the page that opened the wizard", () => {
    expect(wizardBackTarget("v3", "k3x9", undefined)).toBe(-1);
    expect(wizardBackTarget("v3", "k3x9", "a1")).toBe(-1);
  });

  it("opened straight from a link, back goes to the V3 page it belongs to", () => {
    expect(wizardBackTarget("v3", "default", "a1")).toBe("/v3/agents?id=a1");
    expect(wizardBackTarget("v3", "default", undefined)).toBe("/v3/create");
  });

  it("V2 keeps its own back", () => {
    expect(wizardBackTarget("v2", "k3x9", "a1")).toBeNull();
    expect(wizardBackTarget("v2", "default", undefined)).toBeNull();
  });
});
