import { api } from "../api";

/**
 * What one architect-assistant turn may replay of its conversation
 * (assistant-context C3, `replay_budget` on `GET /api/assistant/architect`): half the
 * preset model's context window in estimated tokens, or a fixed character budget when
 * the window is unknown.
 */
export interface ReplayBudget {
  unit: "tokens" | "chars";
  limit: number;
  max_turns: number;
  source: "config" | "model" | "fallback";
  model_id: string | null;
  context_window_tokens: number | null;
  window_share: number;
}

/** The architect preset's replay budget; null from a backend that predates it. */
export async function architectReplayBudget(): Promise<ReplayBudget | null> {
  const status = await api.assistantStatus();
  return status.replay_budget ?? null;
}

/** Whether `assistant_replay_max_tokens` cut the budget below the window share. */
export function budgetCapped(budget: ReplayBudget): boolean {
  return (
    budget.unit === "tokens" &&
    budget.context_window_tokens != null &&
    budget.limit < Math.floor(budget.context_window_tokens * budget.window_share)
  );
}
