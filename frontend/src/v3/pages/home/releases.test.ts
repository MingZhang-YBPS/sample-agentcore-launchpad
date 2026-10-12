import { describe, expect, it } from "vitest";

import type { ReleaseState } from "../../../lib/dlc";
import { readReleases } from "./releases";

const state = { state: { endpoint_mode: "live" }, pending: null } as ReleaseState;

describe("release status coverage", () => {
  it("keeps successful states and exposes a failed read rather than calling it ungated", async () => {
    const result = await readReleases([
      { id: "ok", status: "active" }, { id: "failed-read", status: "active" }, { id: "deploying", status: "deploying" },
    ], async (id) => {
      if (id === "failed-read") throw new Error("AWS unavailable");
      return state;
    });
    expect(result.states).toEqual({ ok: state, "failed-read": null });
    expect(result.unavailable).toBe(1);
    expect(result.unchecked).toBe(0);
  });

  it("reports agents beyond the read budget as unchecked", async () => {
    const agents = Array.from({ length: 27 }, (_, i) => ({ id: `a${i}`, status: "active" as const }));
    const result = await readReleases(agents, async () => state);
    expect(Object.keys(result.states)).toHaveLength(24);
    expect(result.unchecked).toBe(3);
    expect(result.states.a24).toBeUndefined();
  });

  it("does not call AWS for an empty or inactive fleet", async () => {
    let calls = 0;
    const result = await readReleases([{ id: "draft", status: "draft" }], async () => { calls++; return state; });
    expect(calls).toBe(0);
    expect(result).toEqual({ states: {}, unavailable: 0, unchecked: 0 });
  });
});
