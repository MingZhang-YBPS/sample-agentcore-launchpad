/** Which editor an agent opens in: the Studio canvas, the preset configure flow, or the re-publish wizard. */
export type EditKind = "canvas" | "preset" | "wizard";

type EditTarget = { id: string; method: string; system?: unknown };

export function editKind(agent: EditTarget): EditKind {
  if (agent.method === "studio") return "canvas";
  if (agent.system) return "preset";
  return "wizard";
}

/** Where editing an agent happens: the Studio canvas for a canvas-built agent,
 *  the preset configure flow (classic page) for a system preset, else the native
 *  V2 `?view=edit` re-publish wizard (hosted inside V3 when V3 is on). */
export function editPath(agent: EditTarget): string {
  switch (editKind(agent)) {
    case "canvas":
      return `/create/studio?agent=${agent.id}`;
    case "preset":
      return `/agents/${agent.id}/edit`;
    default:
      return `/v2/agents?view=edit&id=${encodeURIComponent(agent.id)}`;
  }
}

/** The V2 detail page itself, kept reachable from V3 (`full=1` stops the V3 hand-over). */
export function classicDetailPath(agentId: string): string {
  return `/v2/agents?view=detail&id=${encodeURIComponent(agentId)}&full=1`;
}

/** The V2 identity page (outbound auth, Connections) — V3 links to it until it has its own. */
export function identityPath(agentId: string): string {
  return `/v2/agents?view=identity&id=${encodeURIComponent(agentId)}`;
}

/**
 * The one verb for "change and re-publish" on every agent page, V2 and V3
 * (no "manage", no bare "edit"): the button label and its tooltip.
 */
export const EDIT_LABEL_KEY = "v3.agent.edit";
export const EDIT_HINT_KEY = "v3.agent.editHint";
