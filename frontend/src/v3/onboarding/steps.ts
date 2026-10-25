import type { AgentInfo, Workspace } from "../../lib/api";
import type { ReleaseState } from "../../lib/dlc";

export interface LaunchStep {
  key: "workspace" | "agent" | "chat" | "evaluate" | "gate";
  done: boolean;
  to: string;
}

/**
 * The five steps from an empty workspace to a release that only moves when a
 * person signs a passing report — each read from what actually exists, never
 * from a click on the checklist. Pure, so it is unit-tested.
 */
export function launchSteps(input: {
  workspace: Pick<Workspace, "bootstrap_status"> | null;
  agents: Pick<AgentInfo, "id" | "status">[];
  conversed: boolean;
  evaluated: boolean;
  releases: Record<string, Pick<ReleaseState, "state"> | null | undefined>;
}): LaunchStep[] {
  const live = input.agents.filter((a) => a.status === "active");
  const first = live[0]?.id;
  const gated = Object.values(input.releases).some((r) => r?.state?.endpoint_mode === "live");
  return [
    { key: "workspace", done: input.workspace?.bootstrap_status === "ready", to: "/v3/workspaces" },
    { key: "agent", done: live.length > 0, to: "/v3/create" },
    { key: "chat", done: input.conversed, to: first ? `/v3/chat?agent=${first}` : "/v3/chat" },
    { key: "evaluate", done: input.evaluated, to: first ? `/v3/tasks?view=new&agent=${first}` : "/v3/tasks?view=new" },
    { key: "gate", done: gated, to: first ? `/v3/standards?agent=${first}&view=release` : "/v3/standards" },
  ];
}
