import { describe, expect, it } from "vitest";

import type { LadderRung } from "../../../lib/dlc";
import { anyRungCost, rungCostSteps } from "./rungCost";

const rung = (run_id: string, over: Partial<LadderRung> = {}) =>
  ({ run_id, agent_version: "v1", cost_actual_usd: null, layers_changed: [], ...over }) as LadderRung;

describe("the ladder's cost per rung", () => {
  it("uses the measured cost, else the estimate, and the change against the rung before", () => {
    const steps = rungCostSteps([
      rung("r1", { cost_actual_usd: 2 }),
      rung("r2", { cost: { total_usd: 2.5, estimate_usd: 2, source: "usage_events" } }),
      rung("r3", { cost: { total_usd: null, estimate_usd: 2, source: null } }),
      rung("r4"),
      rung("r5", { cost_actual_usd: 1 }),
    ]);
    expect(steps.map((s) => [s.usd, s.kind, s.delta_usd, s.delta_pct])).toEqual([
      [2, "actual", null, null],
      [2.5, "actual", 0.5, 25],
      [2, "estimate", -0.5, -20],
      [null, null, null, null],
      [1, "actual", null, null],
    ]);
    expect(anyRungCost(steps)).toBe(true);
    expect(anyRungCost(rungCostSteps([rung("r1")]))).toBe(false);
  });
});
