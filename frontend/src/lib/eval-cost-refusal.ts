/**
 * The 409 an evaluation entry answers when its estimate needs a confirmation or is
 * over the workspace's limit (eval-cost §1.1): `run.cost_confirm_required` /
 * `run.cost_over_limit`, from `POST /api/eval/runs` and `POST /api/eval/online`
 * alike, with the server's estimate in `detail.estimate`. Shared by the V3 and V2
 * task wizards and the V3 online evaluation editor.
 */
import type { TFunction } from "i18next";

import { ApiError } from "./api";
import { type EvalCostEstimate, usd } from "./api/evalCost";

export interface CostRefusal {
  /** `confirm`: above the confirm threshold — tick and resubmit; `over`: above the limit */
  kind: "confirm" | "over";
  estimate: EvalCostEstimate | null;
}

/** The refusal in `err`, or null for any other error. */
export function costRefusal(err: unknown): CostRefusal | null {
  if (!(err instanceof ApiError)) return null;
  const kind = err.code === "run.cost_confirm_required" ? "confirm" : err.code === "run.cost_over_limit" ? "over" : null;
  if (!kind) return null;
  const est = (err.detail as { estimate?: EvalCostEstimate } | null | undefined)?.estimate;
  return { kind, estimate: est && typeof est === "object" ? est : null };
}

/** What the refusal says: the server's own figure (the history may have moved since
 *  the card was priced) and the way out. */
export function refusalText(t: TFunction, refusal: CostRefusal): string {
  const est = refusal.estimate;
  const figure = `${usd(est?.total_usd)}${est?.period === "day" ? ` ${t("v3.evalCost.slashDay")}` : ""}`;
  if (refusal.kind === "confirm") return t("v3.evalCost.refusedConfirm", { usd: figure });
  // the project's quota binds an administrator too: name it, promise no override
  const key = est?.max_source === "project" ? "v3.evalCost.refusedOverProject" : "v3.evalCost.refusedOver";
  return t(key, { usd: figure, max: usd(est?.max_usd) });
}
