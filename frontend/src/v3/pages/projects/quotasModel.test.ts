import { describe, expect, it } from "vitest";

import type { ProjectQuotas, QuotaItem } from "../../../lib/api/projects";
import { formatQuota, parseQuota, quotaDraft, quotaErrors, quotaPatch, usageSignal } from "./quotasModel";

const view = (quotas: ProjectQuotas["quotas"] = {}, caps: ProjectQuotas["department_caps"] = {}): ProjectQuotas => ({
  project_id: "alpha",
  department_id: "retail",
  quotas,
  department_caps: caps,
  items: [],
  month_start: "2026-10-01T00:00:00+00:00",
});

const item = (over: Partial<QuotaItem>): QuotaItem => ({
  key: "max_agents",
  unit: "count",
  enforcement: "block",
  project_value: null,
  department_cap: null,
  limit: null,
  used: null,
  ...over,
});

describe("quota form", () => {
  it("drafts every key, empty when unset", () => {
    const draft = quotaDraft(view({ max_agents: 3, eval_run_max_usd: 1.5 }));
    expect(draft.max_agents).toBe("3");
    expect(draft.eval_run_max_usd).toBe("1.5");
    expect(draft.max_api_keys).toBe("");
  });

  it("parses within the backend's bounds", () => {
    expect(parseQuota("max_agents", "")).toBeNull();
    expect(parseQuota("max_agents", "0")).toBe(0);
    expect(parseQuota("max_agents", "1.5")).toBeNaN();
    expect(parseQuota("max_concurrent_eval_runs", "0")).toBeNaN();
    expect(parseQuota("eval_run_max_usd", "2.25")).toBe(2.25);
    expect(parseQuota("eval_run_max_usd", "-1")).toBeNaN();
    expect(parseQuota("v1_rate_per_minute", "abc")).toBeNaN();
  });

  it("sends changed keys only, an emptied field as null", () => {
    const current = view({ max_agents: 3, max_api_keys: 2 });
    const draft = { ...quotaDraft(current), max_agents: "5", max_api_keys: "" };
    expect(quotaPatch(current, draft)).toEqual({ max_agents: 5, max_api_keys: null });
    expect(quotaPatch(current, quotaDraft(current))).toBeNull();
  });

  it("refuses an invalid field or one above the department cap", () => {
    const current = view({}, { max_agents: 4 });
    const draft = { ...quotaDraft(current), max_agents: "5", eval_run_max_usd: "x" };
    expect(quotaErrors(current, draft)).toEqual(["max_agents", "eval_run_max_usd"]);
    expect(quotaPatch(current, draft)).toBeNull();
    expect(quotaPatch(current, { ...draft, max_agents: "4", eval_run_max_usd: "" })).toEqual({ max_agents: 4 });
  });
});

describe("usage", () => {
  it("signals by share of the limit", () => {
    expect(usageSignal(item({ limit: null, used: 3 }))).toBe("off");
    expect(usageSignal(item({ limit: 10, used: 3 }))).toBe("ok");
    expect(usageSignal(item({ limit: 10, used: 8 }))).toBe("wait");
    expect(usageSignal(item({ limit: 10, used: 10 }))).toBe("act");
    expect(usageSignal(item({ limit: 0, used: 0 }))).toBe("act");
    expect(usageSignal(item({ enforcement: "alert", unit: "usd", limit: 100, used: 20, state: "firing" }))).toBe("act");
  });

  it("formats dollars and counts", () => {
    expect(formatQuota({ unit: "usd" }, 12.5)).toBe("$12.50");
    expect(formatQuota({ unit: "count" }, 3)).toBe("3");
    expect(formatQuota({ unit: "count" }, null)).toBe("—");
  });
});
