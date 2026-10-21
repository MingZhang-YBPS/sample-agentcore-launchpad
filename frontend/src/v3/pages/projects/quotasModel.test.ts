import { describe, expect, it } from "vitest";

import type { ProjectBudget, ProjectQuotas, QuotaItem } from "../../../lib/api/projects";
import { budgetProgress, formatQuota, parseQuota, quotaDraft, quotaErrors, quotaPatch, usageSignal } from "./quotasModel";

const view = (quotas: ProjectQuotas["quotas"] = {}): ProjectQuotas => ({
  project_id: "alpha",
  department_id: "retail",
  quotas,
  items: [],
  budget: null,
  budget_basis: "estimate",
  month_start: "2026-10-01T00:00:00+00:00",
  reset_at: "2026-11-01T00:00:00+00:00",
});

const item = (over: Partial<QuotaItem>): QuotaItem => ({
  key: "max_agents",
  unit: "count",
  enforcement: "block",
  project_value: null,
  limit: null,
  used: null,
  ...over,
});

const budget = (over: Partial<ProjectBudget>): ProjectBudget => ({
  project_id: "alpha",
  budget_usd: 10,
  spent_usd: 4,
  remaining_usd: 6,
  fraction: 0.4,
  exhausted: false,
  month_start: "2026-10-01T00:00:00+00:00",
  reset_at: "2026-11-01T00:00:00+00:00",
  measured_at: null,
  detail: "",
  basis: "estimate",
  ...over,
});

describe("quota form", () => {
  it("drafts every key, empty when unset", () => {
    const draft = quotaDraft(view({ max_agents: 3, monthly_budget_usd: 1.5 }));
    expect(draft.max_agents).toBe("3");
    expect(draft.monthly_budget_usd).toBe("1.5");
    expect(draft.max_api_keys).toBe("");
  });

  it("parses within the backend's bounds", () => {
    expect(parseQuota("max_agents", "")).toBeNull();
    expect(parseQuota("max_agents", "0")).toBe(0);
    expect(parseQuota("max_agents", "1.5")).toBeNaN();
    expect(parseQuota("max_concurrent_eval_runs", "0")).toBeNaN();
    expect(parseQuota("monthly_budget_usd", "2.25")).toBe(2.25);
    expect(parseQuota("monthly_budget_usd", "-1")).toBeNaN();
    expect(parseQuota("v1_rate_per_minute", "abc")).toBeNaN();
  });

  it("sends changed keys only, an emptied field as null", () => {
    const current = view({ max_agents: 3, max_api_keys: 2 });
    const draft = { ...quotaDraft(current), max_agents: "5", max_api_keys: "" };
    expect(quotaPatch(current, draft)).toEqual({ max_agents: 5, max_api_keys: null });
    expect(quotaPatch(current, quotaDraft(current))).toBeNull();
  });

  it("refuses an invalid field", () => {
    const current = view();
    const draft = { ...quotaDraft(current), max_agents: "5", monthly_budget_usd: "x" };
    expect(quotaErrors(current, draft)).toEqual(["monthly_budget_usd"]);
    expect(quotaPatch(current, draft)).toBeNull();
    expect(quotaPatch(current, { ...draft, monthly_budget_usd: "" })).toEqual({ max_agents: 5 });
  });
});

describe("usage", () => {
  it("signals by share of the limit", () => {
    expect(usageSignal(item({ limit: null, used: 3 }))).toBe("off");
    expect(usageSignal(item({ limit: 10, used: 3 }))).toBe("ok");
    expect(usageSignal(item({ limit: 10, used: 8 }))).toBe("wait");
    expect(usageSignal(item({ limit: 10, used: 10 }))).toBe("act");
    expect(usageSignal(item({ limit: 0, used: 0 }))).toBe("act");
  });

  it("formats dollars and counts", () => {
    expect(formatQuota({ unit: "usd" }, 12.5)).toBe("$12.50");
    expect(formatQuota({ unit: "count" }, 3)).toBe("3");
    expect(formatQuota({ unit: "count" }, null)).toBe("—");
  });
});

describe("the monthly budget's progress", () => {
  it("is ok under 80 %, wait from 80 %, act and blocked once spent", () => {
    expect(budgetProgress(budget({ spent_usd: 4 }))).toEqual({ fraction: 0.4, signal: "ok", blocked: false });
    expect(budgetProgress(budget({ spent_usd: 8 }))).toEqual({ fraction: 0.8, signal: "wait", blocked: false });
    expect(budgetProgress(budget({ spent_usd: 10 }))).toEqual({ fraction: 1, signal: "act", blocked: true });
    // an overshoot (the spend lags) is clamped to a full bar
    expect(budgetProgress(budget({ spent_usd: 12 }))?.fraction).toBe(1);
    expect(budgetProgress(budget({ budget_usd: 0, spent_usd: 0 }))).toEqual({ fraction: 1, signal: "act", blocked: true });
  });

  it("blocks at the block line with the overrun switch on (110 %)", () => {
    const over = { allow_overrun: true, block_at_usd: 11 };
    expect(budgetProgress(budget({ spent_usd: 10.5, ...over }))).toEqual({ fraction: 1, signal: "act", blocked: false });
    expect(budgetProgress(budget({ spent_usd: 11, ...over }))?.blocked).toBe(true);
  });

  it("is null while the spend is not measured", () => {
    expect(budgetProgress(budget({ spent_usd: null }))).toBeNull();
  });
});
