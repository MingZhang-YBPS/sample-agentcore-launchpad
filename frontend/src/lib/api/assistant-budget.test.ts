import { describe, expect, it, vi } from "vitest";

const status = vi.fn();
vi.mock("../api", () => ({ api: { assistantStatus: () => status() } }));

import { architectReplayBudget, budgetCapped, type ReplayBudget } from "./assistant-budget";

const budget = (over: Partial<ReplayBudget> = {}): ReplayBudget => ({
  unit: "tokens", limit: 100_000, max_turns: 12, source: "model", model_id: "m",
  context_window_tokens: 200_000, window_share: 0.5, ...over,
});

describe("architect replay budget", () => {
  it("reads the budget off the assistant status, null from an older backend", async () => {
    status.mockResolvedValueOnce({ replay_budget: budget() });
    expect(await architectReplayBudget()).toEqual(budget());
    status.mockResolvedValueOnce({});
    expect(await architectReplayBudget()).toBeNull();
  });

  it("flags a budget cut below half the window by the token cap", () => {
    expect(budgetCapped(budget())).toBe(false);
    expect(budgetCapped(budget({ limit: 200_000, context_window_tokens: 1_000_000 }))).toBe(true);
    expect(budgetCapped(budget({ unit: "chars", limit: 160_000, context_window_tokens: null, source: "fallback" }))).toBe(false);
  });
});
