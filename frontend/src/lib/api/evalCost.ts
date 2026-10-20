/**
 * Evaluation cost (eval-cost §1): the estimate every evaluation entry shows before
 * it is submitted — dataset replay, past sessions / look-back window, online
 * evaluation (per day), the release gate — and what a run really cost
 * (`cost_actual`, written at every terminal state; a live spend while it replays).
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

export type CostScope = "replay" | "passive" | "online";
export type CostPartKey = "agent" | "runtime" | "judge" | "managed";

export interface CostPart {
  key: CostPartKey;
  usd: number | null;
  low: number | null;
  high: number | null;
  /** history_7d | eval_runs | rough | unpriced | not_invoked | judges | agentcore_evaluations */
  basis: string;
  model?: string | null;
  seconds?: number;
  samples?: number;
}

export interface CostJudge {
  evaluator_id: string;
  level?: string;
  billed_by?: "agentcore_evaluations" | "your_account" | string;
  model?: string | null;
  usd?: number | null;
  judgements?: number;
  tokens_per_item?: number;
  samples?: number;
  note?: string;
}

export interface EvalCostEstimate {
  scope?: CostScope;
  period?: "run" | "day";
  items: number;
  repeats: number;
  sessions: number;
  sessions_basis?: "given" | "history_7d";
  agent_usd: number | null;
  agent_basis: string;
  runtime_usd?: number;
  runtime_seconds?: number;
  judge_usd: number | null;
  managed_usd?: number | null;
  judges: CostJudge[];
  components?: CostPart[];
  total_usd: number | null;
  range?: { low: number; mid: number; high: number } | null;
  basis?: {
    agent_samples: number;
    duration_samples: number;
    judge_samples: Record<string, number>;
    prices: { source: string | null; updated_at: string | null };
    model_prices_updated_at?: string | null;
  };
  unpriced_models?: string[];
  duration_minutes: number | null;
  confirm_required: boolean;
  confirm_usd: number | null;
  over_limit: boolean;
  max_usd: number | null;
  /** the per-run limit is the workspace release policy's (a project's money limit is its monthly budget) */
  max_source?: "workspace" | null;
  unpriced: boolean;
  gate_unavailable?: string;
}

export interface EstimateBody {
  /** absent for a CloudWatch log source (no platform agent: nothing invoked) */
  agent_id?: string;
  scope?: CostScope;
  dataset_id?: string;
  evaluators?: string[];
  repeats?: number;
  sessions?: number;
  lookback_hours?: number;
  sampling_percentage?: number;
  release?: boolean;
}

export interface CostActual {
  agent_usd: number | null;
  agent_tokens?: { input?: number; output?: number; total?: number };
  runtime_usd: number;
  runtime_seconds: number;
  judge_usd: number;
  managed_evaluations_usd: number;
  judge_tokens: Record<string, number>;
  judgements: Record<string, number>;
  total_usd: number;
  unpriced_models: string[];
  sessions: number;
  invoked: boolean;
  source: "usage_events" | "spans" | "estimate_fallback" | string;
  partial: boolean;
  status: string;
  computed_at: string;
  estimate_total_usd?: number;
  delta_usd?: number;
  delta_pct?: number | null;
  notes?: string[];
}

export interface CostLive {
  sessions: number;
  invokes: number;
  usage_invokes: number;
  seconds: number;
  agent_usd: number | null;
  runtime_usd: number;
  total_usd: number;
  updated_at: string;
}

export interface CostSummary {
  usd: number | null;
  kind: "actual" | "live" | "estimate";
  estimate_usd: number | null;
}

export interface RunCostCard {
  run_id: string;
  status: string;
  final: boolean;
  running: boolean;
  actual: CostActual | null;
  live: CostLive | null;
  estimate: EvalCostEstimate | null;
  estimate_total_usd: number | null;
  estimate_range: { low: number; mid: number; high: number } | null;
  summary: CostSummary;
  prices: { source: string | null; updated_at: string | null };
}

export interface OnlineCostDay {
  day: string;
  judgements: number;
  judge_usd: number;
  reports_usd: number;
  usd: number;
}

export interface OnlineDailyCost {
  config_id: string;
  days: OnlineCostDay[];
  range_days: number;
  total_usd: number;
  judge_model: string;
  unpriced_models: string[];
  basis: { prices: { source: string | null; updated_at: string | null } };
}

export const evalCostApi = {
  estimate: (body: EstimateBody) => sendJson<EvalCostEstimate>("/api/eval/runs/estimate", "POST", body),
  runCost: (runId: string) => getJson<RunCostCard>(`/api/eval/runs/${enc(runId)}/cost`),
  runCosts: (runIds: string[]) =>
    runIds.length
      ? getJson<{ costs: Record<string, CostSummary> }>(`/api/eval/run-costs?runs=${enc(runIds.join(","))}`)
      : Promise.resolve({ costs: {} as Record<string, CostSummary> }),
  onlineDaily: (configId: string, days = 7) =>
    getJson<OnlineDailyCost>(`/api/eval/online/${enc(configId)}/cost?days=${days}`),
};

/** `$1.93`, finer below a cent so a cheap run does not read as free. */
export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value === 0) return "$0.00";
  if (Math.abs(value) < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

/** The parts of an estimate, in reading order (an older server sends none). */
export function estimateParts(est: EvalCostEstimate | null | undefined): CostPart[] {
  if (!est) return [];
  if (est.components?.length) return est.components;
  return [
    { key: "agent", usd: est.agent_usd, low: est.agent_usd, high: est.agent_usd, basis: est.agent_basis },
    { key: "judge", usd: est.judge_usd, low: est.judge_usd, high: est.judge_usd, basis: "judges" },
  ];
}

/** Each part's share of the bar: what it contributes to the mid total. */
export function partShares(parts: { key: string; usd: number | null }[]): { key: string; share: number }[] {
  const total = parts.reduce((sum, p) => sum + (p.usd ?? 0), 0);
  return parts.map((p) => ({ key: p.key, share: total > 0 ? (p.usd ?? 0) / total : 0 }));
}

/** Actual against the estimate: `+7%`, or null with nothing to compare. */
export function deltaPct(actual: number | null | undefined, estimate: number | null | undefined): number | null {
  if (actual === null || actual === undefined || !estimate) return null;
  return Math.round(((actual - estimate) / estimate) * 1000) / 10;
}

/** Where the mid value sits between low and high, 0..1 (the range bar's marker). */
export function rangePosition(range: { low: number; mid: number; high: number } | null | undefined): number {
  if (!range || range.high <= range.low) return 0.5;
  return Math.min(1, Math.max(0, (range.mid - range.low) / (range.high - range.low)));
}
