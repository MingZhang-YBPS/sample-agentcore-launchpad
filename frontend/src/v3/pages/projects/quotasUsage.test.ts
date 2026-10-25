import { describe, expect, it } from "vitest";

import type { QuotaItem } from "../../../lib/api/projects";
import { usageBars } from "./quotasModel";

const item = (over: Partial<QuotaItem>): QuotaItem => ({
  key: "max_agents", unit: "count", enforcement: "block", project_value: null,
  limit: 10, used: 4, ...over,
});

describe("the overview's quota usage bars (projects §11.3)", () => {
  it("draws only quotas with a limit and a usage, clamped to full", () => {
    const bars = usageBars([
      item({ key: "max_agents", used: 4 }),
      item({ key: "max_api_keys", used: 12 }),
      item({ key: "monthly_budget_usd", unit: "usd", used: null }),
      item({ key: "max_concurrent_eval_runs", limit: null }),
      item({ key: "v1_rate_per_minute", limit: 0, used: 0 }),
    ]);
    expect(bars.map((b) => [b.item.key, b.fraction])).toEqual([
      ["max_agents", 0.4],
      ["max_api_keys", 1],
      ["v1_rate_per_minute", 1],
    ]);
  });
});
