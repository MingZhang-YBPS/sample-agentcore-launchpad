import { type NetworkProfile, resultsOf } from "../../../lib/api/networkProfiles";
import type { Signal } from "../../ui";

/** A check row's lamp: block → act, warn → wait; `info` rows are informational. */
export const SEVERITY_SIGNAL: Record<string, Signal> = { ok: "ok", warn: "wait", block: "act", info: "info" };
/** A profile's preflight status chip. */
export const STATUS_SIGNAL: Record<string, Signal> = { ok: "ok", warn: "wait", blocked: "act", unchecked: "off" };

/** The Availability Zones the last preflight found the profile's subnets in. */
export function azsOf(p: Pick<NetworkProfile, "check_results">): string[] {
  const row = resultsOf(p).find((r) => r.check === "subnets.azs");
  const azs = row?.details?.azs;
  return Array.isArray(azs) ? (azs as string[]) : [];
}
