// V3 online evaluation — signal mapping for configs and reports. The rules
// (transient, editable, toggleable, report activity) are V2's, in v2/online.ts.
import type { OnlineEvalConfigRow } from "../../../lib/api";
import { isTransient, modeOf } from "../../../v2/online";
import type { Signal } from "../../ui";

const FAILED = new Set(["CREATE_FAILED", "UPDATE_FAILED", "ERROR"]);

/** A config: failed needs you; settling waits; enabled and active is fine; paused is off. */
export function configSignal(row: OnlineEvalConfigRow): Signal {
  if (FAILED.has(row.status ?? "")) return "act";
  if (isTransient(row)) return "wait";
  if (row.execution_status === "ENABLED") return row.duplicate_enabled ? "wait" : "ok";
  return "off";
}

/** Why a config is in the attention queue (null ⇒ it is not). */
export function attentionOf(row: OnlineEvalConfigRow): "failed" | "duplicate" | "settling" | null {
  if (FAILED.has(row.status ?? "")) return "failed";
  if (row.duplicate_enabled && row.execution_status === "ENABLED") return "duplicate";
  if (isTransient(row)) return "settling";
  return null;
}

export function reportSignal(status: string | null): Signal {
  if (status === "COMPLETED") return "ok";
  if (status === "COMPLETED_WITH_ERRORS") return "wait";
  if (status === "FAILED" || status === "STOPPED") return "act";
  return "info";
}

/** A mean score on the 0–1 normalized scale, as a signal. */
export function scoreSignal(normalized: number | null): Signal {
  if (normalized == null) return "off";
  return normalized >= 0.7 ? "ok" : normalized >= 0.4 ? "wait" : "act";
}

export { modeOf };
