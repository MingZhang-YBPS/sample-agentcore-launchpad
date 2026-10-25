/**
 * The fix ladder's cost per rung (eval-cost §1.3, the V2 side of the V3 compare
 * tab): each run's cost — measured, else its estimate marked as one — and what it
 * changed against the rung before, so a fix that also moved the bill is visible.
 */
import type { LadderRung } from "../../../lib/dlc";

export interface RungCostStep {
  run_id: string;
  agent_version: string | null;
  usd: number | null;
  kind: "actual" | "estimate" | null;
  /** against the previous rung's cost (null when either side is unknown) */
  delta_usd: number | null;
  delta_pct: number | null;
}

/** A rung's cost: the measured total, else the estimate. */
export function rungCost(rung: LadderRung): { usd: number | null; kind: RungCostStep["kind"] } {
  const measured = rung.cost?.total_usd ?? rung.cost_actual_usd;
  if (measured !== null && measured !== undefined) return { usd: measured, kind: "actual" };
  const estimate = rung.cost?.estimate_usd ?? null;
  return estimate === null ? { usd: null, kind: null } : { usd: estimate, kind: "estimate" };
}

export function rungCostSteps(rungs: LadderRung[]): RungCostStep[] {
  let previous: number | null = null;
  return rungs.map((rung, index) => {
    const { usd, kind } = rungCost(rung);
    const delta = index > 0 && usd !== null && previous !== null ? usd - previous : null;
    const step: RungCostStep = {
      run_id: rung.run_id,
      agent_version: rung.agent_version,
      usd,
      kind,
      delta_usd: delta,
      delta_pct: delta !== null && previous ? Math.round((delta / previous) * 1000) / 10 : null,
    };
    previous = usd;
    return step;
  });
}

/** Whether any rung has a cost at all (the card is left out otherwise). */
export const anyRungCost = (steps: RungCostStep[]) => steps.some((s) => s.usd !== null);
