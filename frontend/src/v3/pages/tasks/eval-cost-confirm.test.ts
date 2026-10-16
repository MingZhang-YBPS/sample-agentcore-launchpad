import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { ApiError } from "../../../lib/api";
import type { CostSummary, EvalCostEstimate } from "../../../lib/api/evalCost";
import { costRefusal, refusalText } from "../../../lib/eval-cost-refusal";
import type { V2Task } from "../../../v2/tasks";
import { isPassiveScope, listCosts, runsWithoutCost } from "./model";

const estimate = (over: Partial<EvalCostEstimate> = {}) =>
  ({ total_usd: 12.5, max_usd: 10, period: "run", confirm_required: true, over_limit: false, ...over }) as EvalCostEstimate;

const task = (id: string, kind: "run" | "online", cost?: CostSummary) =>
  ({ id, kind, run: kind === "run" ? ({ id, cost } as unknown as V2Task["run"]) : undefined }) as Pick<V2Task, "kind" | "id" | "run">;

describe("the 409 cost refusals", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("reads the code and the server's estimate", () => {
    expect(costRefusal(new ApiError("run.cost_confirm_required", "m", { estimate: estimate() }))).toEqual({ kind: "confirm", estimate: estimate() });
    expect(costRefusal(new ApiError("run.cost_over_limit", "m", null))).toEqual({ kind: "over", estimate: null });
    expect(costRefusal(new ApiError("eval.cost_confirm_required", "m", null))).toBeNull();
    expect(costRefusal(new Error("x"))).toBeNull();
  });

  it("says what to do, per day for an online config", () => {
    expect(refusalText(i18n.t, { kind: "confirm", estimate: estimate() })).toContain("$12.50");
    expect(refusalText(i18n.t, { kind: "confirm", estimate: estimate() })).toContain("Tick the confirmation");
    const over = refusalText(i18n.t, { kind: "over", estimate: estimate({ period: "day", total_usd: 3 }) });
    expect(over).toContain("$3.00 / day");
    expect(over).toContain("$10.00");
  });

  it("names the project's quota when it is the binding limit (no administrator override)", () => {
    const over = refusalText(i18n.t, { kind: "over", estimate: estimate({ total_usd: 3, max_source: "project" }) });
    expect(over).toContain("this project's per-run evaluation limit");
    expect(over).toContain("raise the project's quota");
    expect(over).not.toContain("workspace");
  });
});

describe("passive runs and the cost column", () => {
  it("treats session ids and a look-back window as passive", () => {
    expect(isPassiveScope({ lookback_hours: 24 })).toBe(true);
    expect(isPassiveScope({ session_ids: ["s1"] })).toBe(true);
    expect(isPassiveScope({ session_ids: [] })).toBe(false);
    expect(isPassiveScope({})).toBe(false);
  });

  it("prefers the run payload's own cost and fetches only the rest", () => {
    const own: CostSummary = { usd: 1.2, kind: "actual", estimate_usd: 1 };
    const tasks = [task("r1", "run", own), task("r2", "run"), task("o1", "online")];
    expect(runsWithoutCost(tasks)).toEqual(["r2"]);
    const fetched: Record<string, CostSummary> = { r1: { usd: 9, kind: "estimate", estimate_usd: 9 }, r2: { usd: 0.4, kind: "live", estimate_usd: 1 } };
    expect(listCosts(tasks, fetched)).toEqual({ r1: own, r2: fetched.r2 });
    expect(listCosts(tasks, undefined)).toEqual({ r1: own });
  });
});
