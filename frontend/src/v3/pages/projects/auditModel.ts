/** Pure helpers of the project audit tab (projects design §11.6). */
import type { AuditOutcome, ProjectAuditFilter } from "../../../lib/api/projects";
import type { Signal } from "../../ui";

export const AUDIT_RANGES = ["1d", "7d", "30d", "90d", "all"] as const;
export type AuditRange = (typeof AUDIT_RANGES)[number];
const RANGE_DAYS: Record<Exclude<AuditRange, "all">, number> = { "1d": 1, "7d": 7, "30d": 30, "90d": 90 };

/** The `from` instant of a range ending now; undefined for "all". */
export function rangeFrom(range: AuditRange, now = Date.now()): string | undefined {
  if (range === "all") return undefined;
  return new Date(now - RANGE_DAYS[range] * 86_400_000).toISOString();
}

export function outcomeSignal(outcome: AuditOutcome): Signal {
  return outcome === "success" ? "ok" : outcome === "refused" ? "wait" : "act";
}

/** The view's filter as the API takes it (empty fields dropped). */
export function auditFilter(f: { actor: string; action: string; outcome: AuditOutcome | ""; from?: string }): ProjectAuditFilter {
  return {
    ...(f.actor ? { actor: f.actor } : {}),
    ...(f.action ? { action: f.action } : {}),
    ...(f.outcome ? { outcome: f.outcome } : {}),
    ...(f.from ? { from: f.from } : {}),
  };
}
