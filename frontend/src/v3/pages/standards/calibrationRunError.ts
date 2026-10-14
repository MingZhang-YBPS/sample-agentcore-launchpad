/**
 * Why a calibration task could not be taken from a run, in words a person can act on.
 *
 * People paste a run's *name* (what the task list shows) where its id belongs and got a
 * bare "run not found". The backend keeps its codes and carries what it looked up in
 * `detail`; this maps the three run refusals to copy. `lookupName` asks the caller to
 * look for a run carrying the typed text as its name, so it can offer that run's id.
 */
import { ApiError } from "../../../lib/api";

export interface RunRefusal {
  key: string;
  vars: Record<string, string>;
  lookupName?: string;
}

const text = (detail: unknown, field: string): string | null => {
  const value = detail && typeof detail === "object" ? (detail as Record<string, unknown>)[field] : null;
  return typeof value === "string" && value ? value : null;
};

/** null ⇒ not a run refusal; show the error as it is. */
export function runRefusal(err: unknown, typed: string, criterionKey: string): RunRefusal | null {
  if (!(err instanceof ApiError)) return null;
  const { code, detail } = err;
  if (code === "run.not_found") {
    const id = text(detail, "run_id") ?? typed;
    return { key: "v2.dlc.calibration.runNotFound", vars: { id }, lookupName: id };
  }
  if (code === "annotation.run_mismatch") {
    const agent = text(detail, "agent_name") ?? text(detail, "agent_id") ?? "—";
    return { key: "v2.dlc.calibration.runOtherAgent", vars: { agent } };
  }
  if (code === "annotation.no_items" && text(detail, "run_id")) {
    return { key: "v2.dlc.calibration.noResultsForCriterion", vars: { key: text(detail, "criterion_key") ?? criterionKey } };
  }
  return null;
}

/** The id of the newest run named exactly `name` (runs arrive newest-first), if any. */
export function runNamed(runs: { id: string; name?: string | null }[], name: string): string | null {
  const needle = name.trim();
  if (!needle) return null;
  return runs.find((run) => (run.name ?? "").trim() === needle)?.id ?? null;
}
