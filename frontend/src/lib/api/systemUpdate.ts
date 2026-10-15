/**
 * Online console update (online-update §2.6): the running version (every member, for the
 * footer) and the administrator's update flow — check the fixed source, prepare a stage,
 * schedule / cancel the restart, roll back, read a log. Hub-global: no workspace header.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

export type UpdateStatus =
  | "preparing"
  | "prepare_failed"
  | "ready"
  | "scheduled"
  | "applying"
  | "done"
  | "rolled_back"
  | "failed"
  | "superseded";

export interface SystemVersion {
  sha: string | null;
  short: string | null;
  committed_at: string | null;
  built_at: string | null;
  updater_installed: boolean;
  update_enabled: boolean;
  can_schedule: boolean;
}

export interface UpdateCommit {
  sha: string;
  title: string;
  author: string;
  time: string;
}

export interface RemoteCheck {
  repo?: string;
  branch?: string;
  checked_at: string | null;
  sha?: string;
  short?: string;
  committed_at?: string | null;
  running_sha?: string;
  behind?: number;
  fast_forward?: boolean;
  up_to_date?: boolean;
  commits?: UpdateCommit[];
  error?: { code: string; message: string };
}

export interface SystemUpdate {
  id: string;
  kind: "update" | "rollback";
  target_sha: string;
  short: string;
  target_time: string | null;
  base_sha: string | null;
  behind: number;
  commits: UpdateCommit[];
  status: UpdateStatus;
  staged_dir: string | null;
  progress: Record<string, "running" | "done">;
  steps: string[];
  scheduled_at: string | null;
  requested_by: string;
  announcement_id: string | null;
  error: string | null;
  previous_sha: string | null;
  finished_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface InflightEntry {
  kind: string;
  id: string;
  workspace_id: string | null;
  status: string;
  label: string;
  restart_outcome: string;
}

export interface UpdateState {
  version: SystemVersion;
  source: { repo: string; branch: string };
  remote: RemoteCheck | null;
  current: SystemUpdate | null;
  history: SystemUpdate[];
  inflight: { items: InflightEntry[]; interrupted: number; resumes: number } | null;
  manual_commands: string[] | null;
}

export interface UpdateLogEvent {
  t: string;
  step: string;
  level: "info" | "error";
  message: string;
}

/** Statuses while the page polls (something is moving on the server). */
export const LIVE_STATUSES: ReadonlySet<UpdateStatus> = new Set(["preparing", "scheduled", "applying"]);

/** The prepare steps in order, each `pending` / `running` / `done` / `failed`. */
export function stepStates(update: SystemUpdate): { step: string; state: "pending" | "running" | "done" | "failed" }[] {
  return update.steps.map((step) => {
    const mark = update.progress[step];
    if (mark === "done") return { step, state: "done" };
    if (mark === "running") return { step, state: update.status === "prepare_failed" ? "failed" : "running" };
    return { step, state: "pending" };
  });
}

/** `datetime-local` value (local time) → ISO with offset; empty = now. */
export function scheduleAt(local: string): string | null {
  if (!local) return null;
  const when = new Date(local);
  return Number.isNaN(when.getTime()) ? null : when.toISOString();
}

export const systemUpdateApi = {
  version: () => getJson<SystemVersion>("/api/system/version"),
  state: () => getJson<UpdateState>("/api/system/update"),
  check: () => sendJson<RemoteCheck>("/api/system/update/check", "POST"),
  prepare: () => sendJson<SystemUpdate>("/api/system/update/prepare", "POST"),
  rollback: () => sendJson<SystemUpdate>("/api/system/update/rollback", "POST"),
  schedule: (id: string, at: string | null, acknowledgeInflight: boolean) =>
    sendJson<SystemUpdate>(`/api/system/update/${enc(id)}/schedule`, "POST", { at, acknowledge_inflight: acknowledgeInflight }),
  cancel: (id: string) => sendJson<SystemUpdate>(`/api/system/update/${enc(id)}/cancel`, "POST"),
  log: (id: string) => getJson<{ id: string; status: UpdateStatus; events: UpdateLogEvent[] }>(`/api/system/update/${enc(id)}/log`),
};
