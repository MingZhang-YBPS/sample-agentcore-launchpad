/**
 * "Build an annotation task from this run" (calibration-run-picker §3, §5): the jump
 * from an evaluation task or a gate report into the criteria page's new-task dialog,
 * prefilled. The link carries `new=1&run=<id>[&criterion=<key>]`; the criteria page
 * reads it once and drops it again, so a refresh does not reopen the dialog.
 */
import type { V2Task } from "../../../v2/tasks";

export const PREFILL_PARAMS = ["new", "run", "criterion"] as const;

export function calibrateUrl(agentId: string, runId: string, criterion?: string | null): string {
  const qs = new URLSearchParams({ view: "calibration", agent: agentId, new: "1", run: runId });
  if (criterion) qs.set("criterion", criterion);
  return `/v3/standards?${qs.toString()}`;
}

/** What the criteria page was asked to prefill; null when the link asks for nothing. */
export function readPrefill(params: URLSearchParams): { runId: string; criterion: string | null } | null {
  if (params.get("new") !== "1") return null;
  return { runId: (params.get("run") ?? "").trim(), criterion: params.get("criterion") || null };
}

/** The same params without the prefill, for a `replace` once the dialog has it. */
export function withoutPrefill(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of PREFILL_PARAMS) next.delete(key);
  return next;
}

export type CalibrateBlocker = "notBatch" | "notCompleted" | "noAgent" | "noEvaluatorResults";

/**
 * Why this task cannot seed an annotation task (§3.1), or null when it can. Without
 * `criteria` (the task list has no verdict detail) a run counts as usable when it ran
 * evaluators; the dialog then checks the count for the criterion.
 */
export function calibrateBlocker(
  task: Pick<V2Task, "kind" | "mode" | "status" | "agentId" | "evaluators">,
  criteria?: string[],
): CalibrateBlocker | null {
  if (task.kind !== "run" || task.mode !== "evaluators") return "notBatch";
  if (task.status !== "completed") return "notCompleted";
  if (!task.agentId) return "noAgent";
  if (criteria ? criteria.length === 0 : task.evaluators.length === 0) return "noEvaluatorResults";
  return null;
}

/** The run's evaluator criteria that hold at least one sample — the ones worth calibrating. */
export function calibratableCriteria(criteria: { key: string; items: number }[] | null | undefined): string[] {
  return (criteria ?? []).filter((c) => c.items > 0).map((c) => c.key).sort();
}

/** One calibratable criterion goes into the link; with several the user picks in the dialog. */
export function soleCriterion(keys: string[]): string | null {
  return keys.length === 1 ? keys[0] : null;
}
