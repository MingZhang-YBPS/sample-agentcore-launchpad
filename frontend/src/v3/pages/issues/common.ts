import type { TFunction } from "i18next";

import { api, type AgentInfo, type IssueStatus } from "../../../lib/api";
import { useLoad } from "../../hooks";
import type { Signal } from "../../ui";

/** An issue: open needs the owner; fixed is done; won't-fix is set aside. */
export function issueSignal(status: IssueStatus): Signal {
  return status === "open" ? "act" : status === "fixed" ? "ok" : "off";
}

/** Hours as "5.2 h" under two days, then whole days — the same rule as V2. */
export function hours(value: number | null | undefined, t: TFunction): string {
  if (value == null) return "—";
  return value < 48
    ? t("selfService.issues.hours", { count: Math.round(value * 10) / 10 })
    : t("selfService.issues.days", { count: Math.round(value / 24) });
}

/** The workspace's ordinary agents (platform presets carry no curated answers or
 *  review links, and a deleted agent has nothing to fix). */
export function useAgents(ws: string) {
  const load = useLoad(() => api.listAgents(), `v3-iss-agents:${ws}`);
  const agents: AgentInfo[] = (load.data?.agents ?? []).filter((a) => a.status !== "deleted" && !a.system);
  const label = (id: string) => {
    const a = agents.find((x) => x.id === id);
    return a ? a.display_name || a.name : id;
  };
  return { agents, label, loading: load.loading };
}
