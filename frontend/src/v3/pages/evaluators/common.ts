// Evaluator helpers shared by the list, the detail and the editor.
import type { EvaluatorRow } from "../../../lib/api";
import type { EvaluatorLevel } from "../../../lib/evaluators";
import type { Signal } from "../../ui";

export const LEVELS: EvaluatorLevel[] = ["SESSION", "TRACE", "TOOL_CALL"];

export const isManaged = (id: string) => id.startsWith("Builtin.") || id.startsWith("ThirdParty.");

/**
 * A custom evaluator's AgentCore status (managed ones carry none): active is in
 * service, a create/update in flight waits, a failed one needs the user.
 */
export function evaluatorSignal(row: Pick<EvaluatorRow, "status" | "source">): Signal {
  const s = String(row.status ?? "").toUpperCase();
  if (row.source !== "custom") return "info";
  if (s === "ACTIVE" || s === "") return "ok";
  if (s.includes("FAIL")) return "act";
  if (s.includes("CREATING") || s.includes("UPDATING") || s.includes("PENDING")) return "wait";
  return "off";
}
