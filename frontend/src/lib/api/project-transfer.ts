/**
 * The agents a project holds in one workspace, for the "move agent" action of the
 * project pages (projects design §7.10 `POST /api/projects/{id}/transfer`).
 *
 * `/api/agents` is workspace- and project-scoped: the request pins both headers
 * itself (the fetch wrapper keeps a header the caller set), so the list is the
 * managed project's, whatever the shared selection is. Only an administrator
 * reaches a project without a membership; the transfer itself is admin-only.
 */
import { type AgentInfo, responseMessage } from "../api";
import { PROJECT_HEADER, WORKSPACE_HEADER } from "../workspace-header";

export async function projectAgents(projectId: string, workspaceId: string): Promise<AgentInfo[]> {
  const res = await fetch("/api/agents", {
    headers: { "Content-Type": "application/json", [WORKSPACE_HEADER]: workspaceId, [PROJECT_HEADER]: projectId },
  });
  if (!res.ok) throw new Error(await responseMessage(res));
  const body = (await res.json()) as { agents?: AgentInfo[] };
  return body.agents ?? [];
}

/** What can move: not deleted, not a platform preset (the backend refuses both). */
export function movableAgents(agents: readonly AgentInfo[]): AgentInfo[] {
  return agents.filter((a) => a.status !== "deleted" && !a.system);
}
