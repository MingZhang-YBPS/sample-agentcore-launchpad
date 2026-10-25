import type {
  ActivationStatus,
  BackfillCoverage,
  BackfillResource,
  BillingReport,
  ResourceTagStatus,
  TagStatus,
} from "../../../lib/api/costTags";
import type { Signal } from "../../ui";

export const TAG_SIGNAL: Record<TagStatus, Signal> = { active: "ok", inactive: "wait", not_found: "off", unknown: "act" };

/** Hours since `iso`, or null. */
export function hoursSince(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? (now - t) / 3_600_000 : null;
}

/**
 * Where the workspace stands on the way from a tagged resource to a billed project
 * line: activation read → `launchpad:project` active in Billing → its first day of
 * billed line items (Billing applies an activation about 24 h later).
 */
export type Stage = "unreadable" | "inactive" | "settling" | "billing";

export function stageOf(status: ActivationStatus, now = Date.now()): Stage {
  if (status.error) return "unreadable";
  const project = status.tags.find((t) => t.key === "launchpad:project");
  if (!project || project.status !== "active") return "inactive";
  const since = hoursSince(project.last_updated, now);
  return since !== null && since < status.activation_delay_hours ? "settling" : "billing";
}

/** The keys a click would activate: inactive ones (not-yet-seen keys cannot be). */
export const activatable = (status: ActivationStatus) => status.tags.filter((t) => t.status === "inactive").map((t) => t.key);

/** Money to two decimals, four under a cent; null is "—". */
export function usd(value: number | null | undefined): string {
  if (value == null) return "—";
  return value > 0 && value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

/** Each point's total (tagged + shared + untagged), for the strip chart. */
export function pointTotals(report: BillingReport): number[] {
  return report.series.map((p) => Object.values(p.by_project).reduce((a, b) => a + b, 0) + (p.shared_usd ?? 0) + (p.untagged_usd ?? 0));
}

/** Billed against estimated: the gap as a signed fraction of the estimate (null without one). */
export function gap(billed: number, estimate: number | null | undefined): number | null {
  if (estimate == null || estimate <= 0) return null;
  return (billed - estimate) / estimate;
}

/* ── back-fill coverage (§1.3) ─────────────────────────────────────────────── */

/** Each result's lamp: the ones an administrator can fix (`denied`, `failed`) are red. */
export const RESOURCE_SIGNAL: Record<ResourceTagStatus, Signal> = {
  tagged: "ok", pending: "info", missing: "wait", denied: "act", failed: "act", unsupported: "off",
};

/** The coverage bar's order: done, still to do, what needs a look, what cannot carry tags. */
export const COVERAGE_ORDER: ResourceTagStatus[] = ["tagged", "pending", "missing", "denied", "failed", "unsupported"];

export interface CoverageSegment {
  status: ResourceTagStatus;
  count: number;
  /** share of every resource found, 0..100 */
  pct: number;
}

/** The bar's segments (empty ones dropped), as shares of the whole inventory. */
export function coverageSegments(coverage: BackfillCoverage): CoverageSegment[] {
  const total = coverage.total;
  return COVERAGE_ORDER.map((status) => ({ status, count: coverage.by_status[status] ?? 0 }))
    .filter((s) => s.count > 0)
    .map((s) => ({ ...s, pct: total ? (s.count / total) * 100 : 0 }));
}

export interface KindRow {
  kind: string;
  total: number;
  tagged: number;
  /** resources of this kind not tagged that could be (pending, denied, missing, failed) */
  open: number;
  unsupported: number;
}

/** Per resource kind: what was found and how much of it is tagged — most open first. */
export function kindRows(coverage: BackfillCoverage): KindRow[] {
  return Object.entries(coverage.by_kind)
    .map(([kind, counts]) => {
      const total = Object.values(counts).reduce((a, b) => a + (b ?? 0), 0);
      const tagged = counts.tagged ?? 0;
      const unsupported = counts.unsupported ?? 0;
      return { kind, total, tagged, unsupported, open: total - tagged - unsupported };
    })
    .sort((a, b) => b.open - a.open || b.total - a.total || a.kind.localeCompare(b.kind));
}

/** The resources not tagged, the ones to act on first: denied, failed, missing, then pending. */
export function sortOpen(resources: BackfillResource[]): BackfillResource[] {
  const rank: Partial<Record<ResourceTagStatus, number>> = { denied: 0, failed: 1, missing: 2, pending: 3 };
  return [...resources].sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9) || a.kind.localeCompare(b.kind) || a.key.localeCompare(b.key));
}

/** What a resource is called on screen: its name, else its ARN's last part. */
export function resourceLabel(r: BackfillResource): string {
  if (r.name) return r.name;
  const tail = r.arn.split(/[:/]/).filter(Boolean).pop();
  return tail || r.key;
}
