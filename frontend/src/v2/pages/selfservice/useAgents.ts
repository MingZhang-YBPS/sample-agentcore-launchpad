import { api, type AgentInfo } from "../../../lib/api";
import { useLoad } from "../../hooks";

/** The workspace's ordinary agents (platform presets carry no curated answers or
 *  review links, and a deleted agent has nothing to fix). */
export function useAgents() {
  const load = useLoad(() => api.listAgents(), "selfservice:agents");
  const agents: AgentInfo[] = (load.data?.agents ?? []).filter(
    (a) => a.status !== "deleted" && !a.system,
  );
  const label = (id: string) => {
    const a = agents.find((x) => x.id === id);
    return a ? a.display_name || a.name : id;
  };
  return { agents, label, loading: load.loading, error: load.error };
}
