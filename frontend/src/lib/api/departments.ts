/**
 * Typed client of the department API (projects design §7.10;
 * `routers/departments.py`). The organisation is department → project → member.
 *
 * Hub-global like the project routes: nothing here reads `X-Workspace` or
 * `X-Project`. Every department write is administrator-only; the department list is
 * open to any signed-in account (it labels the project switcher's groups). Since
 * §11.1 a department's administrators manage its projects and may invite accounts
 * when the department's settings allow; appointing them and opening workspaces stay
 * the platform administrator's.
 */
import { getJson, sendJson } from "../api";
import { queryString } from "./projects";

const enc = encodeURIComponent;

/** `/api/departments[/{id}[/{...rest}]]`, each segment encoded. */
export function departmentPath(departmentId?: string, ...rest: string[]): string {
  if (departmentId === undefined) return "/api/departments";
  return ["/api/departments", enc(departmentId), ...rest.map(enc)].join("/");
}

/** list filter: `active` is the server default */
export type StructureStatus = "active" | "archived" | "all";

/* ── departments ──────────────────────────────────────────────────────────── */

/** `departments.settings` (§11.1), defaults filled in by the server */
export interface DepartmentSettings {
  /** the department's administrators may invite new member accounts */
  can_create_users: boolean;
  /** cap on the department's projects; null = none */
  max_projects: number | null;
  /** an invited account's longest validity */
  user_validity_max_days: number;
}

export interface Department {
  id: string;
  name: string;
  description: string;
  settings: DepartmentSettings;
  archived: boolean;
  archived_at: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  project_count: number;
}

export interface DepartmentCreate {
  id: string;
  name: string;
  description?: string;
}

export type DepartmentPatch = Partial<Pick<Department, "name" | "description">> & {
  /** partial; a null entry restores that key's default */
  settings?: Partial<{ [K in keyof DepartmentSettings]: DepartmentSettings[K] | null }>;
};

export interface DepartmentAdmin {
  user_id: string;
  username: string | null;
  email: string | null;
  account_role: string | null;
  status: string | null;
  added_by: string | null;
  added_at: string | null;
}

export interface DepartmentWorkspace {
  workspace_id: string;
  name: string;
  region: string | null;
  added_by: string | null;
  added_at: string | null;
}

/** `{add, remove}` — the admins and open-workspaces PUT bodies */
export interface IdsPut {
  add?: string[];
  remove?: string[];
}

export interface InvitationCreate {
  username: string;
  email: string;
  /** one of the department's active projects */
  project_id: string;
  /** a project role (default `developer`) */
  role_key?: string;
  /** ≤ `settings.user_validity_max_days` (the default) */
  valid_days?: number;
}

export interface Invitation {
  user_id: string;
  username: string;
  email: string;
  expires_at: string | null;
  project_id: string;
  role_key: string;
  /** shown once */
  generated_password: string | null;
}

/** `GET /api/me/access` → `managed_departments`: what the caller administers (§11.1) */
export interface ManagedDepartment {
  id: string;
  name: string;
  /** where the caller may bind the department's projects */
  workspaces: string[];
}

export const departmentsApi = {
  list: (status?: StructureStatus) =>
    getJson<{ departments: Department[] }>(`${departmentPath()}${queryString({ status })}`),
  create: (body: DepartmentCreate) => sendJson<Department>(departmentPath(), "POST", body),
  update: (id: string, body: DepartmentPatch) => sendJson<Department>(departmentPath(id), "PATCH", body),
  archive: (id: string) => sendJson<Department>(departmentPath(id, "archive"), "POST"),
  unarchive: (id: string) => sendJson<Department>(departmentPath(id, "unarchive"), "POST"),
  /** 409 `department.in_use` while a project names it */
  remove: (id: string) => sendJson<void>(departmentPath(id), "DELETE"),

  /* §11.1 — platform administrators only */
  admins: (id: string) => getJson<{ admins: DepartmentAdmin[] }>(departmentPath(id, "admins")),
  putAdmins: (id: string, body: IdsPut) =>
    sendJson<{ admins: DepartmentAdmin[]; added: string[]; removed: string[] }>(departmentPath(id, "admins"), "PUT", body),
  workspaces: (id: string) => getJson<{ workspaces: DepartmentWorkspace[] }>(departmentPath(id, "workspaces")),
  /** closing a workspace stops new bindings; existing ones stay */
  putWorkspaces: (id: string, body: IdsPut) =>
    sendJson<{ workspaces: DepartmentWorkspace[]; added: string[]; removed: string[] }>(
      departmentPath(id, "workspaces"), "PUT", body,
    ),
  /** an administrator, or the department's own when `settings.can_create_users` */
  invite: (id: string, body: InvitationCreate) =>
    sendJson<Invitation>(departmentPath(id, "invitations"), "POST", body),
  /** the departments the caller administers (empty for a platform administrator) */
  mine: async () =>
    (await getJson<{ managed_departments?: ManagedDepartment[] }>("/api/me/access")).managed_departments ?? [],
};
