/**
 * The calibration run picker (calibration-run-picker §4): which runs a new
 * annotation task can be taken from, with the number of samples each would give.
 */
import { getJson } from "../api";

/** Why a run cannot be picked: it has not finished, or it holds no verdict for the criterion. */
export type CandidateReason = "not_completed" | "no_results";

export interface CandidateRun {
  id: string;
  name: string | null;
  dataset_name: string | null;
  status: string;
  split: string | null;
  created_at: string | null;
  finished_at: string | null;
  /** set when a release gate evaluated the run */
  release_id: string | null;
  /** samples a task taken from this run gets for the criterion */
  items: number;
  eligible: boolean;
  reason: CandidateReason | null;
}

export interface CandidateRunsQuery {
  agentId: string;
  criterionKey: string;
  q?: string;
  /** ISO timestamp: finished at or after */
  since?: string;
  source?: "gate" | "manual";
  /** default true: only runs with at least one sample */
  onlyEligible?: boolean;
  limit?: number;
  offset?: number;
}

/** How many runs the dropdown lists (design D-1); older ones are pasted by id or picked from the list. */
export const DROPDOWN_RUNS = 20;

export function candidateRunsPath(query: CandidateRunsQuery): string {
  const qs = new URLSearchParams({ agent_id: query.agentId, criterion_key: query.criterionKey });
  if (query.q?.trim()) qs.set("q", query.q.trim());
  if (query.since) qs.set("since", query.since);
  if (query.source) qs.set("source", query.source);
  if (query.onlyEligible === false) qs.set("only_eligible", "false");
  if (query.limit != null) qs.set("limit", String(query.limit));
  if (query.offset) qs.set("offset", String(query.offset));
  return `/api/calibration/candidate-runs?${qs.toString()}`;
}

/** The evaluator criteria a run holds verdicts for, with each one's sample count. */
export interface RunCalibrationCriteria {
  run_id: string;
  status: string;
  mode: string;
  agent_id: string | null;
  criteria: { key: string; items: number }[];
}

export const calibrationApi = {
  candidateRuns: (query: CandidateRunsQuery) =>
    getJson<{ total: number; runs: CandidateRun[] }>(candidateRunsPath(query)),
  runCriteria: (runId: string) =>
    getJson<RunCalibrationCriteria>(`/api/calibration/run-criteria/${encodeURIComponent(runId)}`),
};

/** A run's label in the picker: its name, else the dataset it replayed, else its id. */
export function candidateLabel(run: Pick<CandidateRun, "id" | "name" | "dataset_name">): string {
  return run.name?.trim() || run.dataset_name?.trim() || run.id;
}
