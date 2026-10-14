/**
 * Typed client of the project-administration API (projects design §7.10), the
 * caller's access (`GET /api/me/access`), administrators and forced logout
 * (`routers/users.py`) and network profiles (`routers/network_profiles.py`).
 *
 * The project routes are hub-global — the project is the one in the path, no
 * `X-Project` is read. Network profiles are workspace infrastructure: they follow
 * the shared workspace selection (`X-Workspace`) like every other workspace route.
 */
import { type ConsoleUser, getJson, sendJson } from "../api";

const enc = encodeURIComponent;

/** `?a=1&b=2` of the defined, non-empty values; `""` when none. */
export function queryString(params: Record<string, string | number | boolean | null | undefined>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    q.set(key, String(value));
  }
  const text = q.toString();
  return text ? `?${text}` : "";
}

/** `/api/projects[/{id}[/{...rest}]]`, each segment encoded. */
export function projectPath(projectId?: string, ...rest: string[]): string {
  if (projectId === undefined) return "/api/projects";
  return ["/api/projects", enc(projectId), ...rest.map(enc)].join("/");
}

/** `/api/network-profiles[/{id}]`. */
export function networkProfilePath(profileId?: string): string {
  return profileId === undefined ? "/api/network-profiles" : `/api/network-profiles/${enc(profileId)}`;
}

/* ── projects ─────────────────────────────────────────────────────────────── */

export type ProjectStatus = "active" | "archived";
/** `resource_visibility` (owner + admin) and `sod_strict` (admin only) */
export type ProjectSettings = { resource_visibility?: "shared" | "isolated"; sod_strict?: boolean } & Record<string, unknown>;

export interface ProjectBinding {
  workspace_id: string;
  name: string;
  region: string | null;
  tier: string | null;
  /** what a header-less request in that workspace means */
  is_workspace_default: boolean;
  enabled_by: string | null;
  enabled_at: string | null;
}

export interface ProjectSummary {
  id: string;
  name: string;
  description: string;
  department_id: string | null;
  group_id: string | null;
  status: ProjectStatus;
  /** the platform's default project (cannot be archived or unbound) */
  is_default: boolean;
  settings: ProjectSettings;
  created_by: string | null;
  created_by_name: string | null;
  created_at: string | null;
  updated_at: string | null;
  archived_at: string | null;
  workspaces: ProjectBinding[];
  member_count: number;
  agent_count: number;
}

/** A single project as its detail routes return it: plus the caller's effective keys. */
export interface ProjectDetail extends ProjectSummary {
  my_permissions: string[];
}

export interface ProjectListFilter {
  department_id?: string;
  group_id?: string;
  status?: ProjectStatus;
  workspace_id?: string;
  q?: string;
}

export interface ProjectCreate {
  /** 2-32 lowercase letters, digits or `-` */
  id: string;
  name: string;
  description?: string;
  department_id?: string | null;
  group_id?: string | null;
  /** at least one */
  workspaces: string[];
  /** users.id of the initial owners */
  owners?: string[];
  settings?: ProjectSettings | null;
}

export interface ProjectPatch {
  name?: string;
  description?: string;
  department_id?: string | null;
  group_id?: string | null;
  settings?: ProjectSettings | null;
}

/** `POST …/archive`: what archiving cut off besides the project itself. */
export interface ProjectArchiveResult extends ProjectDetail {
  links_revoked: number;
  keys_disabled: number;
}

export interface BindingsPut {
  bind?: string[];
  unbind?: string[];
  /** make this project those workspaces' default */
  make_default?: string[];
}

export interface BindingsResult {
  workspaces: ProjectBinding[];
  /** workspace ids newly bound */
  bound: string[];
}

/* members */

export interface ProjectRoleSource {
  /** `direct` or `group:<group_id>` */
  source: string;
  role_key: string;
}

export interface ProjectMember {
  user_id: string;
  username: string | null;
  /** masked (`z***@corp.com`) unless the caller manages structure */
  email: string | null;
  account_role: string | null;
  status: string | null;
  direct: {
    role_key: string;
    overrides: Record<string, boolean>;
    added_by: string | null;
    added_at: string | null;
  } | null;
  group: { group_id: string; role_key: string } | null;
  sources: ProjectRoleSource[];
  /** an administrator holds every key anyway */
  bypass: boolean;
  permissions: string[];
  /** separation-of-duty pairs this member's keys hold together (`[key, key]`) */
  sod_conflicts: [string, string][];
}

/** `GET /api/projects/{id}/members`. */
export interface ProjectMembers {
  members: ProjectMember[];
  /** the project refuses (rather than warns about) a separation-of-duty conflict */
  sod_strict: boolean;
}

export interface MemberUpsert {
  user_id: string;
  role_key: string;
  /** per-key grants (true) / revocations (false) over the role */
  overrides?: Record<string, boolean> | null;
}

export interface MembersPut {
  upsert?: MemberUpsert[];
  remove?: string[];
}

export interface MembersResult {
  added: string[];
  updated: string[];
  removed: string[];
  members: ProjectMember[];
}

export interface MemberCandidate {
  user_id: string;
  username: string;
  /** always masked */
  email: string;
  is_member: boolean;
}

/* roles */

export interface RoleTemplate {
  key: string;
  permissions: string[];
}

/** `GET /api/role-templates`. */
export interface RoleTemplates {
  templates: RoleTemplate[];
  /** the role key that follows the member's workspace grant (default project only) */
  inherit: string;
  /** agent-level keys (`agents.deploy`, `eval.run`, …) */
  agent_keys: string[];
  /** project-level keys (`project.use`, `project.write`, `project.members`, …) */
  project_keys: string[];
}

export interface CustomRole {
  key: string;
  name: string;
  permissions: string[];
  based_on: string | null;
  /** direct members holding it */
  members: number;
  updated_at: string | null;
}

/** `GET /api/projects/{id}/roles`. */
export interface ProjectRoles extends RoleTemplates {
  inherit_available: boolean;
  custom: CustomRole[];
}

export interface RolePut {
  name: string;
  permissions: string[];
  based_on?: string | null;
}

/* transfer */

export interface TransferBody {
  /** phase 1 moves agents only */
  kind?: "agent";
  resource_id: string;
  to_project_id: string;
}

export interface TransferResult {
  agent_id: string;
  from_project_id: string;
  to_project_id: string;
  /** rows moved, by table */
  rows: Record<string, number>;
}

/* ── the caller's access (GET /api/me/access) ─────────────────────────────── */

export interface AccessProject {
  id: string;
  name: string;
  status: ProjectStatus;
  is_default: boolean;
  is_workspace_default: boolean;
  permissions: string[];
  role_sources: ProjectRoleSource[];
  /** an administrator reaching it without a membership */
  bypass: boolean;
  /** the switcher groups by department (§2.7); null when the project has none */
  department_id: string | null;
  department_name: string | null;
}

export interface AccessWorkspace {
  workspace_id: string;
  name: string;
  /** active projects the workspace binds, reachable or not: the read-only project
   *  chip shows when the caller reaches one of several (§2.7) */
  bound_projects: number;
  /** what a request without `X-Project` resolves to; null ⇒ the header is required */
  default_project_id: string | null;
  projects: AccessProject[];
}

export interface MyAccess {
  username: string;
  user_id: string | null;
  role: string;
  is_admin: boolean;
  can_manage_structure: boolean;
  workspaces: AccessWorkspace[];
}

/* ── administrators, forced logout ────────────────────────────────────────── */

export interface AdminAccount extends ConsoleUser {
  builtin: boolean;
  can_sign_in: boolean;
  /** false for the built-in admin, the caller and the last admin who can sign in */
  can_demote: boolean;
  /** the last administrator who can sign in (absent on the built-in one) */
  last_signable?: boolean;
}

export interface AdminList {
  items: AdminAccount[];
  total: number;
  /** administrators who can sign in */
  can_sign_in: number;
}

export interface SessionsRevoked {
  ok: true;
  user_id: string;
  session_epoch: number;
}

/* ── network profiles ─────────────────────────────────────────────────────── */

export type NetworkPurpose = "agents" | "tools" | "both";
export type NetworkInternet = "none" | "nat";

/** A declared private target: either `cidr` or `host`, and a port. */
export interface NetworkReachable {
  cidr?: string;
  host?: string;
  port: number;
  note?: string;
}

export interface NetworkProfile {
  id: string;
  workspace_id: string;
  name: string;
  vpc_id: string;
  subnet_ids: string[];
  security_group_ids: string[];
  purpose: NetworkPurpose;
  internet: NetworkInternet;
  reachable: NetworkReachable[];
  status: string;
  check_results: unknown;
  checked_at: string | null;
  /** migrated from an agent's inline subnets */
  legacy: boolean;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  archived_at: string | null;
  /** relative to the request's project: may it pick this profile */
  allowed: boolean;
  /** …and is it preselected */
  is_default: boolean;
  /** administrator view only */
  projects?: Record<string, unknown>[];
  agents?: number;
}

export interface NetworkProfileBody {
  name: string;
  vpc_id: string;
  /** 2-8 (two AZs) */
  subnet_ids: string[];
  /** 1-5 */
  security_group_ids: string[];
  purpose?: NetworkPurpose;
  internet?: NetworkInternet;
  reachable?: NetworkReachable[];
}

export type NetworkProfilePatch = Partial<NetworkProfileBody>;

export interface ProjectNetworkProfiles {
  project_id: string;
  workspace_id: string;
  profiles: NetworkProfile[];
  default_profile_id: string | null;
}

export interface ProjectNetworkProfilesPut {
  profile_ids: string[];
  default_profile_id?: string | null;
}

/* ── the client ───────────────────────────────────────────────────────────── */

export const projectsApi = {
  list: (filter: ProjectListFilter = {}) =>
    getJson<{ projects: ProjectSummary[] }>(`${projectPath()}${queryString({ ...filter })}`),
  create: (body: ProjectCreate) => sendJson<ProjectDetail>(projectPath(), "POST", body),
  get: (projectId: string) => getJson<ProjectDetail>(projectPath(projectId)),
  update: (projectId: string, patch: ProjectPatch) => sendJson<ProjectDetail>(projectPath(projectId), "PATCH", patch),
  archive: (projectId: string) => sendJson<ProjectArchiveResult>(projectPath(projectId, "archive"), "POST"),
  unarchive: (projectId: string) => sendJson<ProjectDetail>(projectPath(projectId, "unarchive"), "POST"),

  workspaces: (projectId: string) =>
    getJson<{ workspaces: ProjectBinding[] }>(projectPath(projectId, "workspaces")),
  putWorkspaces: (projectId: string, body: BindingsPut) =>
    sendJson<BindingsResult>(projectPath(projectId, "workspaces"), "PUT", body),

  members: (projectId: string) => getJson<ProjectMembers>(projectPath(projectId, "members")),
  putMembers: (projectId: string, body: MembersPut) =>
    sendJson<MembersResult>(projectPath(projectId, "members"), "PUT", body),
  removeMember: (projectId: string, userId: string) =>
    sendJson<void>(projectPath(projectId, "members", userId), "DELETE"),
  candidates: (projectId: string, q = "") =>
    getJson<{ candidates: MemberCandidate[] }>(`${projectPath(projectId, "candidates")}${queryString({ q })}`),

  roles: (projectId: string) => getJson<ProjectRoles>(projectPath(projectId, "roles")),
  putRole: (projectId: string, roleKey: string, body: RolePut) =>
    sendJson<ProjectRoles>(projectPath(projectId, "roles", roleKey), "PUT", body),
  deleteRole: (projectId: string, roleKey: string) =>
    sendJson<void>(projectPath(projectId, "roles", roleKey), "DELETE"),

  transfer: (projectId: string, body: TransferBody) =>
    sendJson<TransferResult>(projectPath(projectId, "transfer"), "POST", { kind: "agent", ...body }),

  roleTemplates: () => getJson<RoleTemplates>("/api/role-templates"),
  myAccess: () => getJson<MyAccess>("/api/me/access"),
};

export const adminsApi = {
  list: () => getJson<AdminList>("/api/admins"),
  revokeSessions: (userId: string) =>
    sendJson<SessionsRevoked>(`/api/users/${enc(userId)}/sessions/revoke`, "POST"),
};

export const networkProfilesApi = {
  list: (includeArchived = false) =>
    getJson<{ profiles: NetworkProfile[] }>(
      `${networkProfilePath()}${queryString({ include_archived: includeArchived || undefined })}`,
    ),
  create: (body: NetworkProfileBody) => sendJson<NetworkProfile>(networkProfilePath(), "POST", body),
  get: (profileId: string) => getJson<NetworkProfile>(networkProfilePath(profileId)),
  update: (profileId: string, patch: NetworkProfilePatch) =>
    sendJson<NetworkProfile>(networkProfilePath(profileId), "PATCH", patch),
  /** archives (soft delete); refused while agents still use it */
  archive: (profileId: string) => sendJson<void>(networkProfilePath(profileId), "DELETE"),
  forProject: (projectId: string) =>
    getJson<ProjectNetworkProfiles>(`${networkProfilePath()}/projects/${enc(projectId)}`),
  setForProject: (projectId: string, body: ProjectNetworkProfilesPut) =>
    sendJson<ProjectNetworkProfiles>(`${networkProfilePath()}/projects/${enc(projectId)}`, "PUT", body),
};
