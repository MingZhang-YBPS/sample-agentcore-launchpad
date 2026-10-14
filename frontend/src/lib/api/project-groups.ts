/**
 * Typed client of the project-group and department API (projects design §7.7,
 * §7.10; `routers/project_groups.py`, `routers/departments.py`).
 *
 * Hub-global like the project routes: nothing here reads `X-Workspace` or
 * `X-Project`. Every group route and every department write is administrator-only;
 * the department list is open to any signed-in account (it labels the project
 * switcher's groups).
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

export interface Department {
  id: string;
  name: string;
  description: string;
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

export type DepartmentPatch = Partial<Pick<Department, "name" | "description">>;

export const departmentsApi = {
  list: (status?: StructureStatus) =>
    getJson<{ departments: Department[] }>(`${departmentPath()}${queryString({ status })}`),
  create: (body: DepartmentCreate) => sendJson<Department>(departmentPath(), "POST", body),
  update: (id: string, body: DepartmentPatch) => sendJson<Department>(departmentPath(id), "PATCH", body),
  archive: (id: string) => sendJson<Department>(departmentPath(id, "archive"), "POST"),
  unarchive: (id: string) => sendJson<Department>(departmentPath(id, "unarchive"), "POST"),
  /** 409 `department.in_use` while a project or a group names it */
  remove: (id: string) => sendJson<void>(departmentPath(id), "DELETE"),
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
