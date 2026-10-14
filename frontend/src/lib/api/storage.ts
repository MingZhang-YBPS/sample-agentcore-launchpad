/**
 * The storage catalog's client (storage-mounts §4, §5, §9, §10): S3 Files / EFS access
 * points an administrator registers per workspace, the per-project allowed set, the
 * check, the agent-time preflight, and an agent's storage card. The pure helpers below
 * turn the wizard's catalog picks into `filesystem.mounts` and back, and read an entry's
 * stored `check_results`.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

export type StorageKind = "s3files" | "efs";
export type StorageMode = "ro" | "rw";
export type StorageDataClass = "reference" | "shared_state" | "scratch";
export type StorageStatus = "unchecked" | "ok" | "warn" | "blocked";
export type StorageSeverity = "ok" | "warn" | "block" | "info";

export const STORAGE_MODES: readonly StorageMode[] = ["ro", "rw"];
export const STORAGE_DATA_CLASSES: readonly StorageDataClass[] = ["reference", "shared_state", "scratch"];
/** `models/storage.STORAGE_ID_PATTERN` */
export const STORAGE_ID_RE = /^[a-z0-9][a-z0-9-]{1,31}$/;
/** S3 Files + EFS mounts per agent (catalog + inline), session storage aside */
export const MAX_BYO_MOUNTS = 4;

export interface StorageCheck {
  check: string;
  severity: StorageSeverity;
  message: string;
  details?: Record<string, unknown>;
  storage_id?: string;
  profile_id?: string;
}

export interface StorageMountTarget {
  id: string;
  subnet_id: string;
  availability_zone_id: string;
  vpc_id: string;
  ip?: string | null;
  security_groups: string[];
  status: string;
}

export interface StorageUser {
  agent_id: string;
  name: string;
  project_id: string | null;
  version: string | null;
  status: string;
  endpoint_mode: string;
  mode: StorageMode;
  mount_path: string;
}

export interface StorageEntry {
  id: string;
  workspace_id: string;
  name: string;
  kind: StorageKind;
  access_point_arn: string;
  file_system_arn: string | null;
  bucket_arn: string | null;
  posix_uid: number | null;
  posix_gid: number | null;
  root_directory: string | null;
  mount_target_azs: string[];
  mount_targets: StorageMountTarget[];
  vpc_id: string | null;
  network_profile_id: string | null;
  max_mode: StorageMode;
  data_class: StorageDataClass;
  origin: "registered" | "platform";
  status: StorageStatus;
  check_results: StorageCheck[] | null;
  checked_at: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  archived_at: string | null;
  allowed: boolean;
  /** administrator view only (the shared view, §4.5) */
  projects?: string[];
  agents?: StorageUser[];
  writers?: number;
  multi_writer?: boolean;
  /** register only: the check could not run (the entry stays unchecked) */
  check_error?: { code: string; message: string };
}

export interface StorageRegisterBody {
  id: string;
  name?: string;
  access_point_arn: string;
  max_mode?: StorageMode;
  data_class?: StorageDataClass;
  network_profile_id?: string | null;
  check?: boolean;
}

export interface StoragePatch {
  name?: string;
  max_mode?: StorageMode;
  data_class?: StorageDataClass;
  /** "" clears the default profile */
  network_profile_id?: string;
}

export interface StorageMountRef {
  storage_id: string;
  mount_path?: string;
  mode?: StorageMode;
}

export interface StoragePreflight {
  status: "ok" | "warn" | "blocked";
  results: StorageCheck[];
}

export interface AgentStorage {
  agent_id: string;
  version: string | null;
  session_storage: string | null;
  mounts: {
    storage_id: string;
    mount_path: string;
    mode: StorageMode;
    missing: boolean;
    entry: {
      name: string;
      kind: StorageKind;
      status: StorageStatus;
      max_mode: StorageMode;
      data_class: StorageDataClass;
      archived: boolean;
      checked_at: string | null;
      blocking: number;
      warnings: number;
    } | null;
  }[];
  inline: { kind: StorageKind; access_point_arn: string | null; mount_path: string | null; mode: StorageMode }[];
}

export const storagePath = (storageId?: string) =>
  storageId === undefined ? "/api/storage-mounts" : `/api/storage-mounts/${enc(storageId)}`;

export const storageApi = {
  list: (includeArchived = false) =>
    getJson<{ storage: StorageEntry[] }>(`${storagePath()}${includeArchived ? "?include_archived=true" : ""}`),
  get: (storageId: string) => getJson<StorageEntry>(storagePath(storageId)),
  register: (body: StorageRegisterBody) => sendJson<StorageEntry>(storagePath(), "POST", body),
  update: (storageId: string, patch: StoragePatch) => sendJson<StorageEntry>(storagePath(storageId), "PATCH", patch),
  /** archives; refused while an agent mounts it */
  archive: (storageId: string) => sendJson<void>(storagePath(storageId), "DELETE"),
  check: (storageId: string) => sendJson<StorageEntry>(`${storagePath(storageId)}/check`, "POST"),
  usage: (storageId: string) =>
    getJson<{ storage_id: string; agents: StorageUser[]; writers: number; multi_writer: boolean }>(`${storagePath(storageId)}/usage`),
  forProject: (projectId: string) =>
    getJson<{ project_id: string; workspace_id: string; storage: StorageEntry[] }>(`${storagePath()}/projects/${enc(projectId)}`),
  setForProject: (projectId: string, storageIds: string[]) =>
    sendJson<{ project_id: string; workspace_id: string; storage: StorageEntry[] }>(
      `${storagePath()}/projects/${enc(projectId)}`, "PUT", { storage_ids: storageIds }),
  preflight: (body: { mounts: StorageMountRef[]; network_profile_id?: string | null; method?: string; byoc_artifact_kind?: string | null }) =>
    sendJson<StoragePreflight>(`${storagePath()}/preflight`, "POST", body),
  forAgent: (agentId: string) => getJson<AgentStorage>(`${storagePath()}/agents/${enc(agentId)}`),
};

/* ── the wizard's catalog picks ───────────────────────────────────────────── */

/** One catalog mount in the wizard form. */
export interface CatalogMountRow {
  storageId: string;
  path: string;
  mode: StorageMode;
}

export const defaultMountPath = (storageId: string) => `/mnt/${storageId}`;

/** A new pick: under `/mnt/<id>`, read-only (D-1). */
export const newCatalogRow = (storageId: string): CatalogMountRow => ({ storageId, path: defaultMountPath(storageId), mode: "ro" });

/** `filesystem.mounts` for the spec (the server stamps kind and ARN). */
export function catalogMountsSpec(rows: readonly CatalogMountRow[]): StorageMountRef[] {
  return rows.map((r) => ({ storage_id: r.storageId, mount_path: r.path || defaultMountPath(r.storageId), mode: r.mode }));
}

/** A stored spec's `filesystem.mounts` back into rows. */
export function catalogRowsFromSpec(fs: { mounts?: { storage_id?: string; mount_path?: string; mode?: string }[] } | null | undefined): CatalogMountRow[] {
  return (fs?.mounts ?? [])
    .filter((m) => typeof m.storage_id === "string" && m.storage_id)
    .map((m) => ({
      storageId: m.storage_id as string,
      path: m.mount_path || defaultMountPath(m.storage_id as string),
      mode: m.mode === "rw" ? "rw" : "ro",
    }));
}

/** The entries still pickable: live, not blocked, not already mounted. */
export function pickable(entries: readonly StorageEntry[], rows: readonly CatalogMountRow[]): StorageEntry[] {
  const taken = new Set(rows.map((r) => r.storageId));
  return entries.filter((e) => !e.archived_at && e.status !== "blocked" && !taken.has(e.id));
}

/** A row's mode may not exceed its entry's `max_mode`. */
export function modeAllowed(row: CatalogMountRow, entry: Pick<StorageEntry, "max_mode"> | undefined): boolean {
  return row.mode === "ro" || entry?.max_mode === "rw";
}

/* ── reading check_results ────────────────────────────────────────────────── */

export function checksOf(entry: Pick<StorageEntry, "check_results">): StorageCheck[] {
  return Array.isArray(entry.check_results) ? entry.check_results : [];
}

/** Blocking / warning counts (`info` rows excluded). */
export function countChecks(results: readonly StorageCheck[]): { block: number; warn: number } {
  let block = 0;
  let warn = 0;
  for (const r of results) {
    if (r.severity === "block") block += 1;
    else if (r.severity === "warn") warn += 1;
  }
  return { block, warn };
}

/** The register form, its validation and its body. */
export interface StorageForm {
  id: string;
  name: string;
  access_point_arn: string;
  max_mode: StorageMode;
  data_class: StorageDataClass;
  network_profile_id: string;
}

export const emptyStorageForm = (): StorageForm => ({
  id: "", name: "", access_point_arn: "", max_mode: "ro", data_class: "reference", network_profile_id: "",
});

export function storageKindOfArn(arn: string): StorageKind | null {
  const parts = arn.trim().split(":");
  if (parts.length < 6 || parts[0] !== "arn") return null;
  if (parts[2] === "s3files" && parts[5].includes("/access-point/")) return "s3files";
  if (parts[2] === "elasticfilesystem" && parts[5].startsWith("access-point/")) return "efs";
  return null;
}

/** field → i18n key of its error */
export function validateStorage(form: StorageForm, creating: boolean): Partial<Record<keyof StorageForm, string>> {
  const out: Partial<Record<keyof StorageForm, string>> = {};
  if (creating && !STORAGE_ID_RE.test(form.id)) out.id = "v3.storage.form.errId";
  if (creating && storageKindOfArn(form.access_point_arn) === null) out.access_point_arn = "v3.storage.form.errArn";
  if (form.name.length > 64) out.name = "v3.storage.form.errName";
  return out;
}

export function storageFormToBody(form: StorageForm): StorageRegisterBody {
  return {
    id: form.id.trim(),
    name: form.name.trim() || undefined,
    access_point_arn: form.access_point_arn.trim(),
    max_mode: form.max_mode,
    data_class: form.data_class,
    network_profile_id: form.network_profile_id || null,
  };
}
