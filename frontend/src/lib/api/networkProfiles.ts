/**
 * The "Configure → Network profiles" page's client (projects §12.4, §12.5, §12.7–§12.9,
 * §12.12): preflight, probe, the workspace's network switches and the condition-key
 * policy, on top of the profile CRUD and per-project allowed set (`networkProfilesApi`
 * in `./projects`, re-exported here). The pure helpers below read a profile's stored
 * `check_results`: the per-check rows, the endpoint matrix, the informational inbound
 * PrivateLink and cost entries, the probe, and the telemetry gaps.
 */
import { getJson, sendJson } from "../api";
import { type NetworkProfile, type NetworkProfileBody, networkProfilePath, networkProfilesApi, type NetworkReachable } from "./projects";

export { networkProfilesApi };
export type { NetworkProfile, NetworkProfileBody, NetworkReachable };

const enc = encodeURIComponent;

export type CheckSeverity = "ok" | "warn" | "block" | "info";

/** One row of a profile's `check_results` (backend `services/network_preflight`). */
export interface CheckResult {
  check: string;
  severity: CheckSeverity;
  message: string;
  details?: Record<string, unknown>;
  /** the probe entry only */
  status?: "running" | "done" | "failed";
  started_at?: string;
  finished_at?: string;
}

export interface NetworkSettings {
  network_enforcement: boolean;
  network_public_allowed: boolean;
}

export interface NetworkEnforcement {
  settings: NetworkSettings;
  approved_subnet_ids: string[];
  approved_security_group_ids: string[];
  stack_parameters: { ApprovedSubnetIds: string; ApprovedSecurityGroupIds: string };
  policy: Record<string, unknown>;
  actions: string[];
  not_enforceable: string[];
}

export const networkApi = {
  ...networkProfilesApi,
  check: (profileId: string) => sendJson<NetworkProfile>(`${networkProfilePath(profileId)}/check`, "POST"),
  /** 202: the probe runs in the background; the profile shows it as `running` */
  probe: (profileId: string) => sendJson<NetworkProfile>(`${networkProfilePath(profileId)}/probe`, "POST"),
  settings: () => getJson<NetworkSettings>(`${networkProfilePath()}/settings`),
  putSettings: (body: Partial<NetworkSettings>) => sendJson<NetworkSettings>(`${networkProfilePath()}/settings`, "PUT", body),
  enforcement: () => getJson<NetworkEnforcement>(`${networkProfilePath()}/enforcement`),
};

export const projectNetworkPath = (projectId: string) => `${networkProfilePath()}/projects/${enc(projectId)}`;

/* ── reading check_results ────────────────────────────────────────────────── */

export const PROBE_CHECK = "probe";

export function resultsOf(profile: Pick<NetworkProfile, "check_results">): CheckResult[] {
  return Array.isArray(profile.check_results) ? (profile.check_results as CheckResult[]) : [];
}

export interface ResultGroups {
  /** subnets / SGs / VPC / routes / ACL rows */
  checks: CheckResult[];
  /** `endpoint.<service>` rows: the endpoint requirement matrix */
  endpoints: CheckResult[];
  inbound: CheckResult | null;
  cost: CheckResult | null;
  probe: CheckResult | null;
}

export function groupResults(results: CheckResult[]): ResultGroups {
  const out: ResultGroups = { checks: [], endpoints: [], inbound: null, cost: null, probe: null };
  for (const r of results) {
    if (r.check === PROBE_CHECK) out.probe = r;
    else if (r.check === "inbound.endpoint") out.inbound = r;
    else if (r.check === "cost") out.cost = r;
    else if (r.check.startsWith("endpoint.")) out.endpoints.push(r);
    else out.checks.push(r);
  }
  return out;
}

/** Blocking / warning counts (the informational entries and a running probe excluded). */
export function countSeverities(results: CheckResult[]): { block: number; warn: number } {
  let block = 0;
  let warn = 0;
  for (const r of results) {
    if (r.check === PROBE_CHECK && r.status === "running") continue;
    if (r.severity === "block") block += 1;
    else if (r.severity === "warn") warn += 1;
  }
  return { block, warn };
}

export interface CostSummary {
  interfaceEndpoints: number;
  availabilityZones: number;
  endpointAzHours: number;
  natGateways: number;
  pricing: Record<string, string>;
}

export function costSummary(results: CheckResult[]): CostSummary | null {
  const cost = groupResults(results).cost;
  if (!cost?.details) return null;
  const d = cost.details;
  return {
    interfaceEndpoints: Array.isArray(d.interface_endpoints) ? d.interface_endpoints.length : 0,
    availabilityZones: Number(d.availability_zones ?? 0),
    endpointAzHours: Number(d.endpoint_az_hours ?? 0),
    natGateways: Array.isArray(d.nat_gateways) ? d.nat_gateways.length : 0,
    pricing: (d.pricing as Record<string, string>) ?? {},
  };
}

export interface InboundEndpoint {
  service: string;
  endpoint_id: string;
  private_dns: boolean;
  principals: string[];
  any_principal: boolean;
}

export function inboundEndpoints(results: CheckResult[]): InboundEndpoint[] {
  const inbound = groupResults(results).inbound;
  const list = inbound?.details?.endpoints;
  return Array.isArray(list) ? (list as InboundEndpoint[]) : [];
}

/** `logs` / `xray` whose endpoint row did not pass: VPC telemetry may be lost (§12.9). */
export function telemetryGaps(results: CheckResult[]): string[] {
  const by = new Map(results.map((r) => [r.check, r]));
  return ["logs", "xray"].filter((svc) => {
    const row = by.get(`endpoint.${svc}`);
    return row !== undefined && row.severity !== "ok";
  });
}

export interface ProbeTarget {
  target: string;
  port: number;
  dns?: string;
  tcp?: string;
  tls?: string;
  error?: string;
}

export function probeTargets(probe: CheckResult | null): ProbeTarget[] {
  const list = probe?.details?.targets;
  return Array.isArray(list) ? (list as ProbeTarget[]) : [];
}

/* ── the create / edit form ───────────────────────────────────────────────── */

export interface ProfileForm {
  name: string;
  vpc_id: string;
  /** one id per line or comma-separated */
  subnets: string;
  security_groups: string;
  purpose: NetworkProfile["purpose"];
  internet: NetworkProfile["internet"];
  /** one target per line: `10.1.0.0/16:5432 note` or `db.internal:5432 note` */
  reachable: string;
}

export function emptyProfileForm(): ProfileForm {
  return { name: "", vpc_id: "", subnets: "", security_groups: "", purpose: "agents", internet: "none", reachable: "" };
}

export function profileToForm(p: NetworkProfile): ProfileForm {
  return {
    name: p.name,
    vpc_id: p.vpc_id ?? "",
    subnets: p.subnet_ids.join("\n"),
    security_groups: p.security_group_ids.join("\n"),
    purpose: p.purpose,
    internet: p.internet,
    reachable: (p.reachable ?? []).map((r) => `${r.cidr ?? r.host}:${r.port}${r.note ? ` ${r.note}` : ""}`).join("\n"),
  };
}

export function parseIds(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean))];
}

const CIDR = /^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/;

/** Lines → targets; `null` for a line that is not `<cidr|host>:<port> [note]`. */
export function parseReachable(text: string): (NetworkReachable | null)[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const m = /^(\S+):(\d{1,5})(?:\s+(.*))?$/.exec(line);
      if (!m) return null;
      const port = Number(m[2]);
      if (port < 1 || port > 65535) return null;
      const target = m[1];
      const note = m[3]?.trim();
      return { ...(CIDR.test(target) ? { cidr: target } : { host: target }), port, ...(note ? { note } : {}) };
    });
}

export type ProfileErrors = Partial<Record<keyof ProfileForm, string>>;

const SUBNET = /^subnet-[0-9a-f]{8,17}$/;
const SG = /^sg-[0-9a-f]{8,17}$/;

/** i18n keys under `v3.network.form.err.*`, per field (the server re-checks). */
export function validateProfile(form: ProfileForm): ProfileErrors {
  const errors: ProfileErrors = {};
  if (!form.name.trim() || form.name.trim().length > 64) errors.name = "v3.network.form.err.name";
  if (!/^vpc-[0-9a-f]{8,17}$/.test(form.vpc_id.trim())) errors.vpc_id = "v3.network.form.err.vpc";
  const subnets = parseIds(form.subnets);
  if (subnets.length < 2 || subnets.length > 8 || !subnets.every((s) => SUBNET.test(s))) errors.subnets = "v3.network.form.err.subnets";
  const groups = parseIds(form.security_groups);
  if (groups.length < 1 || groups.length > 5 || !groups.every((g) => SG.test(g))) errors.security_groups = "v3.network.form.err.groups";
  if (parseReachable(form.reachable).some((r) => r === null)) errors.reachable = "v3.network.form.err.reachable";
  return errors;
}

export function formToBody(form: ProfileForm): NetworkProfileBody {
  return {
    name: form.name.trim(),
    vpc_id: form.vpc_id.trim(),
    subnet_ids: parseIds(form.subnets),
    security_group_ids: parseIds(form.security_groups),
    purpose: form.purpose,
    internet: form.internet,
    reachable: parseReachable(form.reachable).filter((r): r is NetworkReachable => r !== null),
  };
}

/** Projects allowed to use the profile (administrator view); > 1 = shared SGs (§12.3). */
export function profileProjects(p: NetworkProfile): { project_id: string; is_default: boolean }[] {
  return (p.projects ?? []) as { project_id: string; is_default: boolean }[];
}
