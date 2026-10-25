// Fleet rows and governance findings mapped onto V3 signals (page-local).
import type { FleetRow, GovernanceFinding } from "../../../lib/api";
import type { Signal } from "../../ui";

/** prod is the tier that must never be mistaken: it carries the "needs you" colour. */
export const TIER_SIGNAL: Record<string, Signal | undefined> = { prod: "act", staging: "wait", dev: undefined };

export interface Problem {
  key: "agentsFailed" | "jobsFailed" | "alertsFiring" | "promotionsPending";
  count: number;
  s: Signal;
}

/** What is wrong in one readable environment, most serious first. */
export function rowProblems(row: FleetRow): Problem[] {
  const out: Problem[] = [];
  if ((row.agents_failed ?? 0) > 0) out.push({ key: "agentsFailed", count: row.agents_failed ?? 0, s: "act" });
  if ((row.alerts_firing ?? 0) > 0) out.push({ key: "alertsFiring", count: row.alerts_firing ?? 0, s: "act" });
  if ((row.jobs_failed ?? 0) > 0) out.push({ key: "jobsFailed", count: row.jobs_failed ?? 0, s: "wait" });
  if ((row.promotions_pending ?? 0) > 0) out.push({ key: "promotionsPending", count: row.promotions_pending ?? 0, s: "info" });
  return out;
}

/** An unreadable environment waits on its bootstrap; failures and firing alerts need someone. */
export function rowSignal(row: FleetRow): Signal {
  if (!row.readable) return "wait";
  const problems = rowProblems(row);
  if (problems.some((p) => p.s === "act")) return "act";
  if (problems.some((p) => p.s === "wait") || (row.agents_deploying ?? 0) > 0) return "wait";
  return "ok";
}

export function findingSignal(severity: GovernanceFinding["severity"]): Signal {
  return severity === "action" ? "act" : severity === "warn" ? "wait" : "info";
}
