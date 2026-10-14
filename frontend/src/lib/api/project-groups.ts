/**
 * Typed client of the project-group and department API (projects design §7.7,
 * §7.10; `routers/project_groups.py`, `routers/departments.py`).
 *
 * Hub-global like the project routes: nothing here reads `X-Workspace` or
 * `X-Project`. Every group route and every department write is administrator-only;
 * the department list is open to any signed-in account (it labels the project
 * switcher's groups). Since §11.1 a department administrator also manages their
 * department's groups (listing them needs `department_id`) and may invite accounts
 * when the department's settings allow; appointing them and opening workspaces stay
 * the platform administrator's.
 */
import { getJson, sendJson } from "../api";
import { queryString } from "./projects";

const enc = encodeURIComponent;

/** `/api/project-groups[/{id}[/{...rest}]]`, each segment encoded. */
export function groupPath(groupId?: string, ...rest: string[]): string {
  if (groupId === undefined) return "/api/project-groups";
  return ["/api/project-groups", enc(groupId), ...rest.map(enc)].join("/");
}

/** `/api/departments[/{id}[/{...rest}]]`, each segment encoded. */
export function departmentPath(departmentId?: string, ...rest: string[]): string {
  if (departmentId === undefined) return "/api/departments";
  return ["/api/departments", enc(departmentId), ...rest.map(enc)].join("/");
}

/** list filter: `active` is the server default */
export type StructureStatus = "active" | "archived" | "all";

/** an SoD pair held on both sides, e.g. `["agents.deploy", "release.sign"]` */
export type SodPair = [string, string];

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
  group_count: number;
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
  /** 409 `department.in_use` while a project or a group names it */
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

/* ── project groups ───────────────────────────────────────────────────────── */

export interface GroupProject {
  id: string;
  name: string;
  status: string;
}

export interface ProjectGroup {
  id: string;
  name: string;
  description: string;
  department_id: string | null;
  archived: boolean;
  archived_at: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  projects: GroupProject[];
  member_count: number;
}

/** where a group member combines duties: the group's role ∪ their direct row there */
export interface GroupSodConflict {
  project_id: string;
  pairs: SodPair[];
  /** the project refuses such combinations (`settings.sod_strict`) */
  strict: boolean;
}

export interface GroupMember {
  user_id: string;
  username: string | null;
  email: string | null;
  account_role: string | null;
  status: string | null;
  /** a built-in template key */
  role_key: string;
  added_by: string | null;
  added_at: string | null;
  sod_conflicts: GroupSodConflict[];
}

export interface ProjectGroupDetail extends ProjectGroup {
  members: GroupMember[];
}

export interface GroupCreate {
  id: string;
  name: string;
  description?: string;
  department_id?: string | null;
}

export type GroupPatch = Partial<Pick<ProjectGroup, "name" | "description" | "department_id">>;

export interface GroupMembersPut {
  upsert?: { user_id: string; role_key: string }[];
  remove?: string[];
}

export interface GroupProjectsPut {
  add?: string[];
  remove?: string[];
}

export const projectGroupsApi = {
  list: (filters: { status?: StructureStatus; department_id?: string } = {}) =>
    getJson<{ groups: ProjectGroup[] }>(`${groupPath()}${queryString(filters)}`),
  get: (id: string) => getJson<ProjectGroupDetail>(groupPath(id)),
  create: (body: GroupCreate) => sendJson<ProjectGroupDetail>(groupPath(), "POST", body),
  update: (id: string, body: GroupPatch) => sendJson<ProjectGroupDetail>(groupPath(id), "PATCH", body),
  /** archives: the access the group gave stops; its projects keep their group */
  archive: (id: string) =>
    sendJson<ProjectGroupDetail & { keys_disabled: number }>(groupPath(id), "DELETE"),
  unarchive: (id: string) => sendJson<ProjectGroupDetail>(groupPath(id, "unarchive"), "POST"),
  /** 409 `project.sod_conflict` when a strict project would get a combined duty */
  putMembers: (id: string, body: GroupMembersPut) =>
    sendJson<ProjectGroupDetail & { added: string[]; updated: string[]; removed: string[] }>(
      groupPath(id, "members"), "PUT", body,
    ),
  /** 409 `group.project_in_other_group`: a project is in at most one group */
  putProjects: (id: string, body: GroupProjectsPut) =>
    sendJson<ProjectGroupDetail & { added: string[]; removed: string[] }>(
      groupPath(id, "projects"), "PUT", body,
    ),
};
