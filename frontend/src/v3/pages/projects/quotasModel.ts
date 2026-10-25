/**
 * Pure helpers of the project quotas section (projects design §11.3): the form's
 * draft (one text field per quota), the partial PUT it sends (changed keys only, an
 * emptied field clearing the quota), the input checks the backend repeats
 * (`services/projects.py::QUOTA_COUNTS`), the signal a usage row shows, and the
 * monthly budget's progress.
 */
import { type ProjectBudget, type ProjectQuotas, QUOTA_KEYS, type QuotaItem, type QuotaKey } from "../../../lib/api/projects";
import type { Signal } from "../../ui";

export type QuotaDraft = Record<QuotaKey, string>;

/** whole-number quotas and their bounds; the rest (the monthly budget) are USD amounts ≥ 0 */
export const QUOTA_COUNT_BOUNDS: Partial<Record<QuotaKey, readonly [number, number]>> = {
  max_agents: [0, 10_000],
  max_api_keys: [0, 10_000],
  max_concurrent_eval_runs: [1, 100],
  v1_rate_per_minute: [1, 100_000],
};
export const MAX_QUOTA_USD = 10_000_000;

export function quotaDraft(view: ProjectQuotas): QuotaDraft {
  const out = {} as QuotaDraft;
  for (const key of QUOTA_KEYS) {
    const value = view.quotas[key];
    out[key] = value === undefined || value === null ? "" : String(value);
  }
  return out;
}

/** The value a field holds: null when empty, NaN when not a valid number for the key. */
export function parseQuota(key: QuotaKey, raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;
  const value = Number(text);
  const bounds = QUOTA_COUNT_BOUNDS[key];
  if (!Number.isFinite(value)) return Number.NaN;
  if (bounds) return Number.isInteger(value) && value >= bounds[0] && value <= bounds[1] ? value : Number.NaN;
  return value >= 0 && value <= MAX_QUOTA_USD ? value : Number.NaN;
}

/** Keys whose field is not a valid value. */
export function quotaErrors(_view: ProjectQuotas, draft: QuotaDraft): QuotaKey[] {
  return QUOTA_KEYS.filter((key) => Number.isNaN(parseQuota(key, draft[key]) ?? 0));
}

/** The PUT body: changed keys only (null clears); null when nothing changed or a field is invalid. */
export function quotaPatch(view: ProjectQuotas, draft: QuotaDraft): Partial<Record<QuotaKey, number | null>> | null {
  if (quotaErrors(view, draft).length) return null;
  const out: Partial<Record<QuotaKey, number | null>> = {};
  for (const key of QUOTA_KEYS) {
    const next = parseQuota(key, draft[key]);
    const current = view.quotas[key] ?? null;
    if (next !== current) out[key] = next;
  }
  return Object.keys(out).length ? out : null;
}

/** ok under 80 %, wait from 80 %, act at or over the limit; off without a limit or usage. */
export function usageSignal(item: QuotaItem): Signal {
  if (item.limit === null || item.used === null || item.used === undefined) return "off";
  if (item.used >= item.limit) return "act";
  return item.used / item.limit >= 0.8 ? "wait" : "ok";
}

/** `$12.50` for USD quotas, the plain number otherwise; `—` for none. */
export function formatQuota(item: Pick<QuotaItem, "unit">, value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return item.unit === "usd" ? `$${value.toFixed(2)}` : String(value);
}

/**
 * The overview's usage bars (§11.3): the quotas with both a limit and a usage to
 * draw, each with how full it is (0–1, clamped; a zero limit is full).
 */
export function usageBars(items: readonly QuotaItem[]): { item: QuotaItem; fraction: number }[] {
  return items
    .filter((item) => item.limit !== null && item.used !== null && item.used !== undefined)
    .map((item) => {
      const limit = item.limit as number;
      const used = item.used as number;
      const fraction = limit <= 0 ? 1 : Math.min(1, Math.max(0, used / limit));
      return { item, fraction };
    });
}

/**
 * The monthly budget's bar: how full (0–1, clamped; a zero budget is full), the
 * signal (wait from 80 % — where the alert fires — act once spent), and whether
 * billable work is refused. Null without a measured spend.
 */
export function budgetProgress(budget: ProjectBudget): { fraction: number; signal: Signal; blocked: boolean } | null {
  if (budget.spent_usd === null) return null;
  const raw = budget.budget_usd <= 0 ? 1 : budget.spent_usd / budget.budget_usd;
  const fraction = Math.min(1, Math.max(0, raw));
  // the block line: the budget, or 110 % of it with the overrun switch on
  const blocked = budget.spent_usd >= (budget.block_at_usd ?? budget.budget_usd);
  return { fraction, blocked, signal: blocked || raw >= 1 ? "act" : raw >= 0.8 ? "wait" : "ok" };
}
