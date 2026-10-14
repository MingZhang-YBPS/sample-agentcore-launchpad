/**
 * The live facts behind the detail pages' "Tools & capabilities" card (projects
 * §18, §17.3-4): the Registry status of each Gateway record, each gateway's policy
 * mode and policies, the live version's spec in a gated workspace, and the agent's
 * telemetry status. Every read is best-effort — a governance or registry read the
 * caller cannot make leaves its warning out instead of failing the card.
 */
import { api, ApiError, type AgentInfo, getJson } from "../api";
import { type GatewayFacts, toolsModel } from "../agent-tools";
import type { ReleaseState } from "../dlc";

export type TelemetryStatus = "receiving" | "none" | "unknown" | "unsupported" | "not_deployed";

/** `GET /api/agents/{id}/telemetry` (backend `routers/agent_telemetry.py`). */
export interface AgentTelemetry {
  agent_id: string;
  endpoint: string;
  service_name: string | null;
  status: TelemetryStatus;
  /** the narrowest probed window holding a span */
  last_span_within: "1h" | "24h" | "7d" | "14d" | null;
  lookback_days: number;
  /** byoc_code_zip_no_adot | no_log_group | probe_failed | method_unsupported | not_deployed | … */
  reason: string | null;
  byoc_no_adot: boolean;
  /** a VPC agent with no spans: its network profile lacks a usable logs / X-Ray
   * endpoint or was never checked (`network_profiles.telemetry_hint`); else null */
  hint?: { code: string; profile_id: string; missing: string[]; message: string } | null;
  checked_at: string;
  cached: boolean;
}

export const agentTelemetry = (agentId: string, refresh = false) =>
  getJson<AgentTelemetry>(`/api/agents/${encodeURIComponent(agentId)}/telemetry${refresh ? "?refresh=true" : ""}`);

/** The candidate and live specs when a gated agent's candidate is not live yet. */
export interface VersionPair {
  candidateVersion: string;
  liveVersion: string;
  candidateSpec: Record<string, unknown>;
  liveSpec: Record<string, unknown>;
}

export interface ToolsFacts {
  /** record id → Registry status (`DELETED` when the record is gone) */
  records: Record<string, string>;
  /** gateway id → its policy mode, name and policies */
  gateways: Record<string, GatewayFacts>;
  /** remote MCP name → its outbound auth type in the Registry */
  mcpAuth: Record<string, string | null>;
}

async function recordStatus(recordId: string): Promise<string | null> {
  try {
    return (await api.registryRecord(recordId)).status;
  } catch (err) {
    return err instanceof ApiError && err.code.endsWith("not_found") ? "DELETED" : null;
  }
}

async function gatewayFacts(gatewayId: string): Promise<GatewayFacts | null> {
  try {
    const res = await api.listGovernancePolicies(gatewayId);
    return {
      name: res.gateway.name,
      mode: res.gateway.policy_engine_configuration?.mode ?? res.engine?.mode ?? null,
      policies: res.policies,
    };
  } catch {
    return null;
  }
}

const specOfSnapshot = (agentId: string, seq: number) =>
  getJson<{ spec?: Record<string, unknown> }>(`/api/agents/${encodeURIComponent(agentId)}/snapshots/${seq}`).then(
    (snap) => snap.spec ?? {},
  );

/**
 * The live version's spec, from the ledger snapshot its deploy recorded — only in a
 * gated workspace whose candidate differs from live (projects §18.3-2).
 */
export async function loadVersionPair(agent: AgentInfo, release: ReleaseState | null): Promise<VersionPair | null> {
  if (!release || release.release_mode !== "gated") return null;
  const live = release.state.live_version ?? null;
  const candidate = release.state.candidate_version ?? agent.version ?? null;
  if (!live || !candidate || live === candidate) return null;
  const { snapshots } = await api.agentSnapshots(agent.id);
  const newest = (version: string) =>
    snapshots.filter((snap) => snap.aws_version === version).sort((a, b) => b.seq - a.seq)[0];
  const liveSnap = newest(live);
  if (!liveSnap) return null;
  const candidateSnap = candidate === agent.version ? null : newest(candidate);
  const [liveSpec, candidateSpec] = await Promise.all([
    specOfSnapshot(agent.id, liveSnap.seq),
    candidateSnap ? specOfSnapshot(agent.id, candidateSnap.seq) : Promise.resolve((agent.spec ?? {}) as Record<string, unknown>),
  ]);
  return { candidateVersion: candidate, liveVersion: live, candidateSpec, liveSpec };
}

/** Everything the card reads beyond the spec and the versions, in parallel. */
export async function loadToolsFacts(agent: AgentInfo): Promise<ToolsFacts> {
  const model = toolsModel(agent, agent.spec);
  const recordIds = [...new Set(model.gateway.map((gw) => gw.recordId).filter((id): id is string => !!id))];
  const gatewayIds = [...new Set(model.gateway.map((gw) => gw.gatewayId).filter((id): id is string => !!id))];
  const [statuses, gateways, attachables] = await Promise.all([
    Promise.all(recordIds.map(recordStatus)),
    Promise.all(gatewayIds.map(gatewayFacts)),
    model.mcp.length ? api.registryAttachables().catch(() => null) : Promise.resolve(null),
  ]);
  const records: Record<string, string> = {};
  recordIds.forEach((id, i) => {
    if (statuses[i]) records[id] = statuses[i]!;
  });
  const gatewayMap: Record<string, GatewayFacts> = {};
  gatewayIds.forEach((id, i) => {
    if (gateways[i]) gatewayMap[id] = gateways[i]!;
  });
  const mcpAuth: Record<string, string | null> = {};
  for (const server of attachables?.mcp_servers ?? []) {
    if (!server.gateway) mcpAuth[server.name] = server.auth_type;
  }
  return { records, gateways: gatewayMap, mcpAuth };
}
