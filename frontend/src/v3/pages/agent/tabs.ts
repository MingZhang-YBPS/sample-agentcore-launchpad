import type { AgentInfo } from "../../../lib/api";
import { releasable } from "../../../lib/agent-admin/release";
import { inboundFacts } from "../../../lib/agent-admin/inbound";

/** The detail page's tabs, in order; the first is the default (kept off the URL). */
export const AGENT_TABS = ["overview", "deploy", "access", "release"] as const;
export type AgentTab = (typeof AGENT_TABS)[number];

export function parseTab(value: string | null): AgentTab {
  return AGENT_TABS.find((tab) => tab === value) ?? "overview";
}

/** The search for a tab: the other params ride along, the default tab drops `tab=`. */
export function tabSearch(params: URLSearchParams, tab: AgentTab): URLSearchParams {
  const next = new URLSearchParams(params);
  if (tab === "overview") next.delete("tab");
  else next.set("tab", tab);
  return next;
}

/** An MCP server is a tool behind a Gateway target, not an agent you chat with or gate. */
export const isMcpServer = (agent: Pick<AgentInfo, "spec">) =>
  (agent.spec as { protocol?: string } | undefined)?.protocol === "mcp";

/**
 * Which cards an agent's detail page shows, by tab. One place for the
 * per-method rules (Harness, zip, container, BYOC, Studio, imported runtime,
 * MCP server, system preset), so the page and the tests read the same answer.
 */
export function detailSections(agent: AgentInfo) {
  const spec = (agent.spec ?? {}) as Record<string, unknown>;
  const mcp = isMcpServer(agent);
  const imported = agent.method === "discovered_runtime";
  const ownRuntime = !!agent.arn && !imported;
  const kbs = (spec.knowledge_bases as unknown[] | undefined) ?? [];
  return {
    // overview
    lifecycle: !mcp,
    mcpPanel: mcp,
    systemNote: !!agent.system,
    byoc: agent.method === "byoc" && !!spec.byoc,
    converted: !!(spec.source_harness as { agent_name?: string } | undefined)?.agent_name,
    knowledgeBases: kbs.length > 0,
    tryIt: !mcp,
    // deploy & versions
    deployProgress: !!(agent.deployments?.[0] ?? agent.deployment),
    versions: ownRuntime && agent.status !== "deploying",
    snapshots: !imported,
    // access & integration
    inbound: inboundFacts(agent).shown,
    integration: !!agent.invoke_capability?.eligible,
    identity: !imported,
    // release
    gate: !mcp,
    promotion: releasable(agent),
  };
}

export type DetailSections = ReturnType<typeof detailSections>;
