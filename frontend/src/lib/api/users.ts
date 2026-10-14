/**
 * Typed client of user management with projects (projects design §7.4–§7.6, §7.10,
 * `routers/users.py`): the account rows' `projects` / `groups`, the list's
 * `role` / `project` / `group` filters, an account's detail and effective access,
 * memberships on create / approve / edit, and the bulk "add to project".
 *
 * Hub-global and admin-only like the rest of `/api/users`. The base account calls
 * (create without memberships, stats, delete) stay in `lib/api.ts`.
 */
import { type ConsoleUser, type UserCreateBody, type UserListResult, getJson, sendJson } from "../api";
import { type MyAccess, type ProjectRoleSource, type ProjectStatus, queryString } from "./projects";

const enc = encodeURIComponent;

/** One project an account belongs to: directly and/or through its group. */
export interface UserProject {
  id: string;
  name: string;
  status: ProjectStatus;
  is_default: boolean;
  /** the direct role, else the group's; `inherit` = the global role's keys */
  role: string;
  sources: ProjectRoleSource[];
}

export interface UserGroup {
  id: string;
  name: string;
  role: string;
  archived: boolean;
}

/** A `ConsoleUser` row as `/api/users` returns it since P1F-users. */
export interface ProjectUser extends ConsoleUser {
  projects: UserProject[];
  groups: UserGroup[];
}

export interface ProjectUserList extends Omit<UserListResult, "items"> {
  items: ProjectUser[];
}

export interface UserListFilter {
  q?: string;
  status?: "all" | "pending" | "active" | "expired" | "disabled";
  role?: "admin" | "operator" | "member";
  /** members of the project (direct or through its group) */
  project?: string;
  group?: string;
  limit?: number;
  offset?: number;
}

export interface ProjectMembershipInput {
  project_id: string;
  role_key: string;
  /** omitted ⇒ an existing row keeps its overrides; null clears them */
  overrides?: Record<string, boolean> | null;
}

export interface GroupMembershipInput {
  group_id: string;
  /** built-in templates only */
  role_key: string;
}

export interface UserCreateWithProjects extends UserCreateBody {
  projects?: ProjectMembershipInput[];
  groups?: GroupMembershipInput[];
}

/**
 * `PATCH /api/users/{id}`. `projects` / `groups` replace the direct memberships
 * (null / [] clears them — the default project's `inherit` row keeps following
 * `workspaces`). An approval is `{status: "active", projects, groups}`.
 */
export interface UserMembershipPatch {
  status?: "pending" | "active" | "disabled";
  role?: "admin" | "operator" | "member";
  extend_days?: number;
  expires_at?: string | null;
  workspaces?: string[] | null;
  projects?: ProjectMembershipInput[] | null;
  groups?: GroupMembershipInput[] | null;
}

/** `GET /api/users/{id}/access`: that account's `/api/me/access`, plus whether it can sign in. */
export interface UserAccess extends MyAccess {
  state: ConsoleUser["state"];
}

export interface BulkProjectAdd {
  user_ids: string[];
  project_id: string;
  role_key: string;
  overrides?: Record<string, boolean> | null;
}

export interface BulkProjectResult {
  project_id: string;
  role_key: string;
  added: string[];
  updated: string[];
  unchanged: string[];
  /** administrators reach every project by role and get no row */
  skipped: { user_id: string; reason: "admin" }[];
}

export function userPath(userId: string, ...rest: string[]): string {
  return ["/api/users", enc(userId), ...rest.map(enc)].join("/");
}

export const usersApi = {
  list: (filter: UserListFilter = {}) =>
    getJson<ProjectUserList>(
      `/api/users${queryString({ ...filter, status: filter.status === "all" ? undefined : filter.status })}`,
    ),
  get: (userId: string) => getJson<ProjectUser>(userPath(userId)),
  access: (userId: string) => getJson<UserAccess>(userPath(userId, "access")),
  create: (body: UserCreateWithProjects) => sendJson<ProjectUser>("/api/users", "POST", body),
  update: (userId: string, patch: UserMembershipPatch) =>
    sendJson<ProjectUser>(userPath(userId), "PATCH", patch),
  approve: (
    userId: string,
    body: { projects?: ProjectMembershipInput[]; groups?: GroupMembershipInput[]; workspaces?: string[] } &
      Pick<UserMembershipPatch, "extend_days" | "expires_at" | "role"> = {},
  ) => sendJson<ProjectUser>(userPath(userId), "PATCH", { ...body, status: "active" }),
  bulkAddToProject: (body: BulkProjectAdd) =>
    sendJson<BulkProjectResult>("/api/users/bulk/projects", "POST", body),
};
