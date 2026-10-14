import { parseUtc } from "../../../v2/pages/agents/deployStatus";
import { ago } from "../../format";

/** A ledger timestamp as local date and time, with "how long ago" after it. */
export function when(iso: string | null | undefined): string {
  const at = parseUtc(iso);
  if (at == null) return "—";
  return `${new Date(at).toLocaleString()} · ${ago(iso)}`;
}
