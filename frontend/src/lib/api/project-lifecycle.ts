/**
 * Typed client of the project lifecycle API (projects design §11.5;
 * `routers/projects.py` lifecycle block): the export bundle, the deletion request
 * and its cooling-off, and the `project_purge` job.
 *
 * Hub-global like the other project routes and administrator-only. The flow:
 * archive → `requestDeletion` (writes an export bundle, starts the cooling-off) →
 * `cancelDeletion` while it runs, or `purge` once `deletion.purge_ready` — call it
 * with `dryRun` first to show what goes. The default project cannot be deleted.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

/** `/api/projects/{id}/{...rest}`, each segment encoded. */
export function lifecyclePath(projectId: string, ...rest: string[]): string {
  return ["/api/projects", enc(projectId), ...rest.map(enc)].join("/");
}

export interface ProjectExport {
  id: string;
  created_at: string;
  created_by: string;
  bytes: number;
  sha256: string;
  /** rows per exported table, plus `audit_events` */
  tables: Record<string, number>;
}

export interface ProjectDeletion {
  requested_at: string;
  requested_by: string;
  requested_by_user_id: string | null;
  cooling_off_days: number;
  purge_after: string;
  export_id: string;
  job_id: string | null;
  purge_ready: boolean;
  seconds_left: number;
}

export interface PurgeJob {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string | null;
  agents_done: string[];
  rows: Record<string, number>;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ProjectLifecycle {
  project_id: string;
  status: "active" | "archived" | "deleting" | "deleted";
  is_default: boolean;
  archived_at: string | null;
  cooling_off_days: number;
  deletion: ProjectDeletion | null;
  deleted: {
    at: string;
    job_id: string;
    requested_by: string | null;
    export_id: string | null;
  } | null;
  exports: ProjectExport[];
  purge_job: PurgeJob | null;
}

export interface PurgePlan {
  dry_run: boolean;
  project_id: string;
  job_id?: string;
  agents: {
    id: string;
    name: string;
    method: string;
    workspace_id: string | null;
    status: string;
  }[];
  rows: Record<string, number>;
  /** journals the purge keeps */
  retained: string[];
}

export const projectLifecycleApi = {
  get: (projectId: string) =>
    getJson<ProjectLifecycle>(lifecyclePath(projectId, "lifecycle")),
  exportBundle: (projectId: string) =>
    sendJson<ProjectExport>(lifecyclePath(projectId, "export"), "POST"),
  /** a plain link target: the browser downloads the ZIP with the session cookie */
  exportUrl: (projectId: string, exportId: string) =>
    lifecyclePath(projectId, "exports", exportId),
  requestDeletion: (projectId: string) =>
    sendJson<ProjectLifecycle>(
      lifecyclePath(projectId, "delete-request"),
      "POST",
    ),
  cancelDeletion: (projectId: string) =>
    sendJson<ProjectLifecycle>(
      lifecyclePath(projectId, "delete-request", "cancel"),
      "POST",
    ),
  purge: (projectId: string, { dryRun = false }: { dryRun?: boolean } = {}) =>
    sendJson<PurgePlan>(
      `${lifecyclePath(projectId, "purge")}${dryRun ? "?dry_run=true" : ""}`,
      "POST",
    ),
};
