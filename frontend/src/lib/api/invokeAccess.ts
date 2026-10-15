/**
 * Typed client of "call it from your app" (design invoke-access §0,
 * `routers/invoke_access.py`) plus the small pure helpers the V3 cards share.
 *
 * `GET /api/agents/{id}/invoke-access` is workspace- and project-scoped like every
 * agent read; `GET /api/projects/{id}/invoke-access` is hub-global (the project is
 * the path's). The backend assembles every ARN and policy — the console never does.
 */
import { type ApiKeyInfo, getJson, responseMessage } from "../api";
import { queryString } from "./projects";

const enc = encodeURIComponent;

export type InvokeQualifier = "DEFAULT" | "live" | "candidate";
export type InvokeMode = "iam" | "jwt";
export type InvokeInclude = "stop_session" | "memory";
export const INVOKE_INCLUDES: readonly InvokeInclude[] = ["stop_session", "memory"];
export type SampleLang = "cli" | "python" | "ts";
export const SAMPLE_LANGS: readonly SampleLang[] = ["cli", "python", "ts"];

export interface IamStatement {
  Sid: string;
  Effect: "Allow";
  Action: string[];
  Resource: string[];
}

export interface IamPolicy {
  Version: string;
  Statement: IamStatement[];
}

export interface InvokeResource {
  kind: "runtime" | "harness" | "runtime_endpoint" | "harness_endpoint" | "memory";
  arn: string | null;
  qualifier?: InvokeQualifier;
  production?: boolean;
}

export interface JwtInfo {
  discovery_url: string | null;
  issuer: string | null;
  allowed_clients: string[];
  allowed_audience: string[];
  allowed_scopes: string[];
  /** an imported runtime with an external authorizer: Launchpad holds no config */
  external: boolean;
}

/** `GET /api/agents/{id}/invoke-access`. */
export interface AgentInvokeAccess {
  agent_id: string;
  name: string;
  method: string;
  kind: "runtime" | "harness";
  mode: InvokeMode;
  account: string;
  region: string;
  partition: string;
  arn: string;
  qualifiers: InvokeQualifier[];
  available_qualifiers: InvokeQualifier[];
  production_qualifier: InvokeQualifier;
  sample_qualifier: InvokeQualifier;
  include: InvokeInclude[];
  resources: InvokeResource[];
  /** null for a JWT agent: IAM permissions do not apply to it */
  policy: IamPolicy | null;
  jwt: JwtInfo | null;
  samples: Record<SampleLang, string>;
  /** machine-readable caveats: jwt_no_iam, cross_account, harness_action_unverified, … */
  notes: string[];
  actions: { runtime: string; harness: string; harness_verified: boolean };
}

export interface InventoryRow {
  agent_id: string;
  name: string;
  method: string;
  kind: "runtime" | "harness";
  mode: InvokeMode;
  account: string;
  region: string;
  arn: string;
  qualifier: InvokeQualifier;
  endpoint_arn: string | null;
  production: boolean;
}

/** `GET /api/projects/{id}/invoke-access`. */
export interface ProjectInvokeAccess {
  project_id: string;
  inventory: InventoryRow[];
  policy: IamPolicy | null;
  jwt_agents: string[];
  skipped: { agent_id: string; name: string; reason: string }[];
  qualifier: InvokeQualifier | null;
  include: InvokeInclude[];
}

export interface InvokeAccessQuery {
  qualifier?: InvokeQualifier | null;
  include?: readonly InvokeInclude[];
}

export interface ProjectInvokeAccessQuery extends InvokeAccessQuery {
  agents?: readonly string[];
}

function projectQuery(q: ProjectInvokeAccessQuery, format?: "csv" | "json") {
  return queryString({
    agents: q.agents?.length ? q.agents.join(",") : undefined,
    qualifier: q.qualifier ?? undefined,
    include: q.include?.length ? q.include.join(",") : undefined,
    format,
  });
}

export const invokeAccessApi = {
  agent: (agentId: string, q: InvokeAccessQuery = {}) =>
    getJson<AgentInvokeAccess>(
      `/api/agents/${enc(agentId)}/invoke-access${queryString({
        qualifier: q.qualifier ?? undefined,
        include: q.include?.length ? q.include.join(",") : undefined,
      })}`,
    ),
  project: (projectId: string, q: ProjectInvokeAccessQuery = {}) =>
    getJson<ProjectInvokeAccess>(`/api/projects/${enc(projectId)}/invoke-access${projectQuery(q)}`),
  /** The inventory as a file (fetched, so a refusal shows as console copy). */
  export: async (projectId: string, format: "csv" | "json", q: ProjectInvokeAccessQuery = {}) => {
    const res = await fetch(`/api/projects/${enc(projectId)}/invoke-access${projectQuery(q, format)}`);
    if (!res.ok) throw new Error(await responseMessage(res));
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `${projectId}-agent-arns.${format}`;
    return { blob: await res.blob(), name };
  },
};

/* ── pure helpers (unit-tested) ─────────────────────────────────────────────── */

/** The policy exactly as copied / downloaded: two-space JSON, like the backend export. */
export function policyText(policy: IamPolicy): string {
  return JSON.stringify(policy, null, 2);
}

/** A file name safe on every OS for a downloaded policy. */
export function policyFileName(name: string, qualifier: string | null): string {
  const base = name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "agent";
  return `${base}${qualifier ? `-${qualifier}` : ""}-invoke-policy.json`;
}

/** Keys that may call this agent: an empty `agent_ids` list means every agent. */
export function keysForAgent(keys: readonly ApiKeyInfo[], agentId: string): ApiKeyInfo[] {
  return keys.filter((k) => !k.agent_ids || k.agent_ids.length === 0 || k.agent_ids.includes(agentId));
}

export type KeyState = "enabled" | "disabled" | "expired" | "offboarded";

/** One state per key, the most blocking first: an offboarded creator's key cannot be re-enabled. */
export function keyState(key: ApiKeyInfo): KeyState {
  if (key.creator_offboarded) return "offboarded";
  if (key.expired) return "expired";
  return key.enabled ? "enabled" : "disabled";
}

/** The `/v1` call a freshly minted key is shown with (the real key filled in once). */
export function v1Sample(origin: string, agentId: string, key: string): string {
  return `curl -N -X POST \\
  ${origin}/v1/agents/${agentId}/invoke-stream \\
  -H "x-api-key: ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{"prompt":"Hello","session_id":null}'`;
}

export function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

/** Creating, disabling and enabling keys is `project.write` (route policy); an
 *  administrator holds every key, and with no project resolved (a single-project
 *  workspace before access loads) the backend's answer decides. */
export function canWriteKeys(isAdmin: boolean, project: { permissions: string[]; bypass?: boolean } | null): boolean {
  if (isAdmin || project == null) return true;
  return project.bypass === true || project.permissions.includes("project.write");
}
