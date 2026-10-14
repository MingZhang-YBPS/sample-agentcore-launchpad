import type { UiVersion } from "../../../lib/ui-version";

/**
 * Where the wizard lands once a publish starts (v3-agent-management §4): in V3
 * the V3 detail page of the agent, otherwise null — the V2 page switches to its
 * own `view=detail` as before.
 */
export function wizardDoneTarget(version: UiVersion, agentId: string): string | null {
  return version === "v3" ? `/v3/agents?id=${encodeURIComponent(agentId)}` : null;
}

/**
 * The wizard's back button in V3: the page that started it — one step back in
 * history when the wizard was opened from inside the app (the launch page, the
 * agent's detail page, a template) — or, opened straight from a link, the V3
 * page it would have come from: the agent's detail for an edit, the launch page
 * for a new agent. Null outside V3: the V2 page keeps its own back.
 */
export function wizardBackTarget(
  version: UiVersion,
  locationKey: string,
  editId: string | undefined,
): -1 | string | null {
  if (version !== "v3") return null;
  // react-router's key of the first entry of a session is "default"
  if (locationKey !== "default") return -1;
  return editId ? `/v3/agents?id=${encodeURIComponent(editId)}` : "/v3/create";
}
