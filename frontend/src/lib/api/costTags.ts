/**
 * Cost allocation tags and the billing-basis cost view (cost-tags design §1.4, §1.6):
 * the workspace's tag set with each key's activation state and the one-click
 * activation, what AWS billed per `launchpad:project` (Cost Explorer, cached 6 hours,
 * the shared row allocated by invocations), and a project's custom cost tags and
 * budget basis, and the workspace-wide back-fill run (§1.3) with its coverage and the
 * resources it could not tag. The pure helpers validate the custom tag editor the way
 * `services/billing_costs.check_custom_tags` does, so a bad row is named before save.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

export type TagStatus = "active" | "inactive" | "not_found" | "unknown";
export type AccountType = "standalone" | "management" | "member" | "unknown";
export type BillingGranularity = "daily" | "monthly";
export type BudgetBasis = "estimate" | "billing";

export interface TagSetEntry {
  key: string;
  value: string;
  role: "primary" | "dimension";
}

export interface TagActivation {
  key: string;
  custom: boolean;
  status: TagStatus;
  last_updated: string | null;
  last_used: string | null;
}

export interface ActivationStatus {
  account_id: string;
  account_type: AccountType;
  can_activate: boolean;
  management_account_id: string | null;
  project_tag_active: boolean;
  tags: TagActivation[];
  error: { code: string; message: string } | null;
  checked_at: string;
  activation_delay_hours: number;
  cache?: { hit: boolean; age_seconds: number; ttl_seconds: number };
}

export interface CostTagsView {
  tag_set: TagSetEntry[];
  shared_value: string;
  custom_max: number;
  reserved_prefixes: string[];
  activation: ActivationStatus;
}

export interface ActivationResult {
  activated: string[];
  already_active: string[];
  not_found: string[];
  errors: { key: string; code: string; message: string }[];
  status: ActivationStatus;
}

export interface ServiceCost {
  service: string;
  usd: number;
}

export interface BillingProjectRow {
  project_id: string;
  tagged_usd: number;
  allocated_shared_usd: number;
  total_usd: number;
  invocations: number;
  services: ServiceCost[];
}

export interface BillingPoint {
  start: string;
  end: string;
  estimated: boolean;
  by_project: Record<string, number>;
  shared_usd: number | null;
  untagged_usd: number | null;
}

export interface BillingReport {
  basis: "billing";
  granularity: BillingGranularity;
  start: string;
  end: string;
  region: string;
  currency: string;
  generated_at: string;
  estimated: boolean;
  by_project: BillingProjectRow[];
  shared: {
    usd: number;
    allocation: "invocations";
    allocated: boolean;
    weights: Record<string, number>;
    weights_error: string | null;
    services: ServiceCost[];
  };
  untagged: { usd: number; services: ServiceCost[] } | null;
  series: BillingPoint[];
  total_usd: number;
  project?: string;
  project_tag_active: boolean | null;
  activation_checked: boolean;
  cache?: { hit: boolean; age_seconds: number; ttl_seconds: number };
}

export interface ProjectCostTags {
  project_id: string;
  cost_tags: Record<string, string>;
  max: number;
  reserved_prefixes: string[];
  budget_basis: BudgetBasis;
  budget_bases: BudgetBasis[];
}

/* ── back-fill (§1.3, the resumable job `cost_tags_backfill`) ─────────────── */

export type BackfillStatus = "queued" | "running" | "succeeded" | "failed";
export type BackfillStageName = "plan" | "tag";
export type BackfillStageStatus = "pending" | "running" | "succeeded" | "failed";
/** A resource's result; `pending` until the tag stage reaches it. */
export type ResourceTagStatus = "pending" | "tagged" | "unsupported" | "denied" | "missing" | "failed";

export interface BackfillStage {
  name: BackfillStageName;
  status: BackfillStageStatus;
  detail: string;
  started_at?: string;
  ended_at?: string;
}

export interface BackfillCoverage {
  total: number;
  /** total less `unsupported` (what can carry tags at all) */
  taggable: number;
  tagged: number;
  /** tagged share of taggable, one decimal; null with nothing taggable */
  percent: number | null;
  by_status: Partial<Record<ResourceTagStatus, number>>;
  by_kind: Record<string, Partial<Record<ResourceTagStatus, number>>>;
}

export interface BackfillResource {
  key: string;
  kind: string;
  arn: string;
  name: string;
  project_id: string | null;
  agent_id: string | null;
  status: ResourceTagStatus;
  detail: string;
  tagged_at?: string;
}

export interface BackfillRun {
  id: string;
  status: BackfillStatus;
  error: string | null;
  workspace_id: string;
  project_id: string | null;
  scope: { project_id?: string | null; agent_ids?: string[] | null };
  /** `admin` for this page's run; `project_update` / `transfer` start re-tag runs */
  reason: string | null;
  actor: string | null;
  created_at: string | null;
  stages: BackfillStage[];
  coverage: BackfillCoverage;
  /** the resources not tagged (neither `tagged` nor `unsupported`) */
  resources: BackfillResource[];
}

export interface BackfillView {
  tag_keys: string[];
  shared_project: string;
  max_cost_tags: number;
  run: BackfillRun | null;
}

export const costTagsApi = {
  get: (force = false) => getJson<CostTagsView>(`/api/cost-tags${force ? "?force=true" : ""}`),
  activate: (keys?: string[]) => sendJson<ActivationResult>("/api/cost-tags/activate", "POST", keys ? { keys } : {}),
  billing: (granularity: BillingGranularity, force = false) =>
    getJson<BillingReport>(`/api/costs/billing?granularity=${granularity}${force ? "&force=true" : ""}`),
  project: (projectId: string) => getJson<ProjectCostTags>(`/api/projects/${enc(projectId)}/cost-tags`),
  putProject: (projectId: string, costTags: Record<string, string>) =>
    sendJson<ProjectCostTags>(`/api/projects/${enc(projectId)}/cost-tags`, "PUT", { cost_tags: costTags }),
  putBudgetBasis: (projectId: string, basis: BudgetBasis) =>
    sendJson<ProjectCostTags>(`/api/projects/${enc(projectId)}/budget-basis`, "PUT", { basis }),
  backfill: () => getJson<BackfillView>("/api/cost-tags/backfill"),
  startBackfill: () => sendJson<BackfillRun>("/api/cost-tags/backfill", "POST", {}),
};

/** A run that will still change: poll it. */
export const backfillActive = (run: BackfillRun | null | undefined): boolean =>
  !!run && (run.status === "queued" || run.status === "running");

/* ── the custom tag editor ─────────────────────────────────────────────────── */

export const MAX_CUSTOM_TAGS = 10;
const RESERVED = ["aws:", "launchpad:"];
// the AWS tag rule: letters, digits, spaces and _ . : / = + - @
const TAG_CHARS = /^[\p{L}\p{N}\s_.:/=+\-@]*$/u;

export interface TagRow {
  key: string;
  value: string;
}

export type TagRowError = "empty" | "long" | "reserved" | "chars" | "duplicate";

export const rowsOf = (tags: Record<string, string>): TagRow[] =>
  Object.entries(tags).map(([key, value]) => ({ key, value }));

/** Each row's problem, or null — the backend's rules, case-insensitive duplicates. */
export function rowErrors(rows: TagRow[]): (TagRowError | null)[] {
  const seen = new Map<string, number>();
  rows.forEach((r) => {
    const k = r.key.trim().toLowerCase();
    if (k) seen.set(k, (seen.get(k) ?? 0) + 1);
  });
  return rows.map((r) => {
    const key = r.key.trim();
    const value = r.value.trim();
    if (!key) return "empty";
    if (key.length > 128 || value.length > 256) return "long";
    if (RESERVED.some((p) => key.toLowerCase().startsWith(p))) return "reserved";
    if (!TAG_CHARS.test(key) || !TAG_CHARS.test(value)) return "chars";
    if ((seen.get(key.toLowerCase()) ?? 0) > 1) return "duplicate";
    return null;
  });
}

/** The rows as the PUT body (trimmed), or null when a row is invalid or over the limit. */
export function tagsOf(rows: TagRow[]): Record<string, string> | null {
  if (rows.length > MAX_CUSTOM_TAGS || rowErrors(rows).some((e) => e !== null)) return null;
  return Object.fromEntries(rows.map((r) => [r.key.trim(), r.value.trim()]));
}

export function sameTags(a: Record<string, string>, b: Record<string, string>): boolean {
  const ka = Object.keys(a);
  return ka.length === Object.keys(b).length && ka.every((k) => b[k] === a[k]);
}

/** How far each key of the activation list has come: active / waiting for Billing / to do. */
export function activationSummary(status: ActivationStatus) {
  const count = (s: TagStatus) => status.tags.filter((t) => t.status === s).length;
  return { total: status.tags.length, active: count("active"), inactive: count("inactive"), notFound: count("not_found") };
}
