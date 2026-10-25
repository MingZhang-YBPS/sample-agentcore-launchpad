// The compare page's cost change (eval-cost §1.3): the per-part rows and the signal.
import type { CostDelta, RunCostPartKey } from "../../../lib/dlc";
import type { Signal } from "../../ui";

/** The ladder's parts, in the order the run cost card lists them; `part` is its colour. */
export const COST_PARTS: { key: RunCostPartKey; part: "agent" | "runtime" | "judge" | "managed" }[] = [
  { key: "agent_usd", part: "agent" },
  { key: "runtime_usd", part: "runtime" },
  { key: "judge_usd", part: "judge" },
  { key: "managed_evaluations_usd", part: "managed" },
];

export interface CostDeltaRow {
  key: RunCostPartKey;
  part: (typeof COST_PARTS)[number]["part"];
  usd: number | null;
  /** |difference| against the largest part's, 0..1 (the bar's length on its side) */
  reach: number;
}

/** The per-part rows: every part either run measured, scaled to the biggest swing. */
export function costDeltaRows(delta: CostDelta): CostDeltaRow[] {
  const rows = COST_PARTS.map(({ key, part }) => ({ key, part, usd: delta.parts[key] ?? null }));
  const widest = Math.max(0, ...rows.map((r) => Math.abs(r.usd ?? 0)));
  return rows
    .filter((r) => r.usd !== null)
    .map((r) => ({ ...r, reach: widest > 0 ? Math.abs(r.usd ?? 0) / widest : 0 }));
}

/** A dearer run waits for a look; a cheaper one is good news; no change is neither. */
export function costDeltaSignal(deltaUsd: number | null): Signal | undefined {
  if (deltaUsd === null || Math.abs(deltaUsd) < 0.00005) return undefined;
  return deltaUsd > 0 ? "wait" : "ok";
}
