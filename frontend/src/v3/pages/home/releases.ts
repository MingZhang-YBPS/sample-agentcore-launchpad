import type { AgentInfo } from "../../../lib/api";
import type { ReleaseState } from "../../../lib/dlc";

export interface ReleaseSnapshot {
  states: Record<string, ReleaseState | null>;
  unavailable: number;
  unchecked: number;
}

/** Bound AWS reads while keeping incomplete coverage visible to the operator. */
export async function readReleases(
  agents: Pick<AgentInfo, "id" | "status">[],
  read: (id: string) => Promise<ReleaseState>,
): Promise<ReleaseSnapshot> {
  const active = agents.filter((a) => a.status === "active");
  const checked = active.slice(0, 24);
  const results = await Promise.allSettled(checked.map((a) => read(a.id)));
  return {
    states: Object.fromEntries(checked.map((a, i) => [a.id, results[i].status === "fulfilled" ? results[i].value : null])),
    unavailable: results.filter((r) => r.status === "rejected").length,
    unchecked: active.length - checked.length,
  };
}
