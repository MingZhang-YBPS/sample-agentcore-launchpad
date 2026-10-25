// The observability agent drop-down (projects §17.3-3): every agent the viewer
// can see, merged with the agents the spans of the current range name. An agent
// with no span in the range stays listed but greyed, so it no longer just
// "disappears" — and a BYOC code zip deployed without the ADOT launcher is named
// as the reason. Pure, so the merge is unit-tested.
import type { AgentInfo } from "../../../lib/api";

export type NoSpanReason = "range" | "byocNoAdot";

export interface AgentOption {
  /** What a span row's `agent` carries: the ledger name, else the raw service. */
  value: string;
  label: string;
  /** Set when the agent has no span in the range; the option is disabled. */
  noSpans: NoSpanReason | null;
}

/** A BYOC code zip the deploy left without `opentelemetry-instrument` (the
 *  backend's `byoc_code_zip_without_adot`): rows deployed before the detection
 *  existed carry no `adot_launcher` and ran without the launcher too. */
export function byocCodeZipWithoutAdot(agent: Pick<AgentInfo, "method" | "spec">): boolean {
  if (agent.method !== "byoc") return false;
  const byoc = (agent.spec?.byoc ?? null) as { artifact_kind?: string; provenance?: { adot_launcher?: boolean } } | null;
  if (byoc?.artifact_kind !== "code_zip") return false;
  return !byoc.provenance?.adot_launcher;
}

/** Agents that can write spans at all: deployed runtimes, not MCP servers. */
function listable(agent: AgentInfo): boolean {
  if (agent.status === "deleted" || agent.status === "draft" || !agent.resource_id) return false;
  return (agent.spec?.protocol as string | undefined) !== "mcp";
}

/**
 * `spanAgents` are the `agent` values of the range's rows; `agents` the viewer's
 * agent list (already visibility-filtered server-side, so a hidden agent is in
 * neither). Agents with spans come first, then the greyed ones, each by label.
 */
export function agentOptions(spanAgents: Iterable<string>, agents: AgentInfo[] | null | undefined): AgentOption[] {
  const seen = new Set([...spanAgents].filter(Boolean));
  const out = new Map<string, AgentOption>();
  for (const agent of agents ?? []) {
    if (!listable(agent) && !seen.has(agent.name)) continue;
    const spans = seen.has(agent.name);
    out.set(agent.name, {
      value: agent.name,
      label: agent.display_name || agent.name,
      noSpans: spans ? null : byocCodeZipWithoutAdot(agent) ? "byocNoAdot" : "range",
    });
  }
  for (const name of seen) {
    if (!out.has(name)) out.set(name, { value: name, label: name, noSpans: null });
  }
  return [...out.values()].sort(
    (a, b) => Number(a.noSpans != null) - Number(b.noSpans != null) || a.label.localeCompare(b.label),
  );
}
