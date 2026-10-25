/**
 * The V2 task wizard's price for what is not a dataset replay (eval-cost §1.1): a
 * passive run — picked sessions, picked log streams or a look-back window — and a
 * continuous (online, per day) scores config. The replay keeps its pass^k card.
 * The same body the V3 wizard sends (`v3/pages/tasks/Wizard.tsx`).
 */
import type { EstimateBody } from "../../../lib/api/evalCost";

export interface PricedDraft {
  target: "agent" | "cloudwatch";
  agentId: string;
  mode: "evaluators" | "insights";
  source: "window" | "sessions" | "logs" | "dataset";
  strategy: "history" | "continuous";
  lookbackHours: number;
  sessionIds: string[];
  logSessionIds: string[];
  sampling: number;
  evaluators: string[];
}

/** The estimate body, or null when this draft is not priced here (insights, a dataset). */
export function passiveEstimateBody(draft: PricedDraft | null): EstimateBody | null {
  if (!draft || draft.mode !== "evaluators" || draft.evaluators.length === 0) return null;
  if (draft.target === "agent" && !draft.agentId) return null;
  const base = { ...(draft.target === "agent" ? { agent_id: draft.agentId } : {}), evaluators: draft.evaluators };
  if (draft.strategy === "continuous") return { ...base, scope: "online", sampling_percentage: draft.sampling };
  if (draft.source === "window") return { ...base, scope: "passive", lookback_hours: draft.lookbackHours };
  if (draft.source === "sessions") return { ...base, scope: "passive", sessions: draft.sessionIds.length };
  if (draft.source === "logs") return { ...base, scope: "passive", sessions: draft.logSessionIds.length };
  return null;
}

/** A passive run: its create body carries `session_ids` or `lookback_hours`. */
export const isPassiveSource = (source: PricedDraft["source"]) => source === "window" || source === "sessions" || source === "logs";
