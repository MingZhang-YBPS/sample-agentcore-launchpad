import type { ActivationStatus, BillingReport, TagStatus } from "../../../lib/api/costTags";
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
