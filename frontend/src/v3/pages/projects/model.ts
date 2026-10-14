/**
 * Pure helpers of the project administration pages (projects design §6.3, §6.4,
 * §7.8): id checks, who may change which member row, the override diff a member
 * edit sends, the settings patch a form sends, and the shapes of the refusals the
 * pages explain (unbind with rows left, combined duties).
 */
import { ApiError } from "../../../lib/api";
import type {
  CustomRole,
  ProjectDetail,
  ProjectMember,
  ProjectPatch,
  ProjectRoles,
  ProjectSettings,
} from "../../../lib/api/projects";
import type { ProjectContextValue } from "../../../workspace/project-context";

/** `models/projects.py::PROJECT_ID_PATTERN` */
export const PROJECT_ID_RE = /^[a-z0-9][a-z0-9-]{1,31}$/;
/** `services/projects.py::ROLE_KEY_PATTERN` */
export const ROLE_KEY_RE = /^[a-z][a-z0-9_]{1,31}$/;
/** `services/projects.py::SOD_PAIRS` — the combined duties the member views flag */
export const SOD_PAIRS: readonly (readonly [string, string])[] = [
  ["agents.deploy", "release.sign"],
  ["criteria.manage", "criteria.sign"],
];
export const PROJECT_TABS = ["overview", "members", "roles", "workspaces", "settings"] as const;
export type ProjectTab = (typeof PROJECT_TABS)[number];
export const INHERIT_ROLE = "inherit";
/** the template a project's owners hold (the Overview lists them) */
export const OWNER_ROLE = "project_owner";

export function isProjectTab(raw: string | null): raw is ProjectTab {
  return raw !== null && (PROJECT_TABS as readonly string[]).includes(raw);
}

export function isProjectId(raw: string): boolean {
  return PROJECT_ID_RE.test(raw);
}

/** A suggestion for the id field while the name is typed: lowercase ASCII, `-` joins. */
export function slugFromName(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32)
    .replace(/-+$/, "");
}

/** A custom role key: the pattern, and not a built-in role (templates or `inherit`). */
export function roleKeyError(key: string, builtin: readonly string[]): "pattern" | "builtin" | null {
  if (!ROLE_KEY_RE.test(key)) return "pattern";
  if (key === INHERIT_ROLE || builtin.includes(key)) return "builtin";
  return null;
}

/** i18n key suffix of a permission key (`mcp.approve` → `mcp_approve`). */
export function keySlug(key: string): string {
  return key.replace(/\./g, "_");
}

/** The SoD pairs a key set holds both sides of (the role editor's live hint). */
export function sodPairsIn(keys: Iterable<string>): [string, string][] {
  const set = new Set(keys);
  return SOD_PAIRS.filter(([a, b]) => set.has(a) && set.has(b)).map(([a, b]) => [a, b]);
}

/** A role's keys in this project; null for `inherit` (it follows the account's global keys). */
export function roleKeys(roles: Pick<ProjectRoles, "templates" | "custom">, roleKey: string): string[] | null {
  if (roleKey === INHERIT_ROLE) return null;
  const template = roles.templates.find((r) => r.key === roleKey);
  if (template) return [...template.permissions];
  const custom = roles.custom.find((r) => r.key === roleKey);
  return custom ? [...custom.permissions] : [];
}

/** Roles a member can be given here: templates, the project's custom roles, and
 *  `inherit` only where the backend offers it (the default project). */
export function assignableRoles(roles: ProjectRoles): { key: string; custom: CustomRole | null }[] {
  return [
    ...roles.templates.map((r) => ({ key: r.key, custom: null })),
    ...roles.custom.map((r) => ({ key: r.key, custom: r })),
    ...(roles.inherit_available ? [{ key: roles.inherit, custom: null }] : []),
  ];
}

/**
 * The `overrides` a member edit sends: only what differs from the role — `true`
 * for a key added on top, `false` for a key of the role taken away. Null when the
 * chosen keys are exactly the role's.
 */
export function overridesFor(roleKeySet: readonly string[], chosen: Iterable<string>): Record<string, boolean> | null {
  const role = new Set(roleKeySet);
  const picked = new Set(chosen);
  const out: Record<string, boolean> = {};
  for (const key of [...picked].sort()) if (!role.has(key)) out[key] = true;
  for (const key of [...role].sort()) if (!picked.has(key)) out[key] = false;
  return Object.keys(out).length ? out : null;
}

/** The keys a role plus a direct row's overrides give (the member editor's start). */
export function applyOverrides(roleKeySet: readonly string[], overrides: Record<string, boolean> | null | undefined): string[] {
  const out = new Set(roleKeySet);
  for (const [key, on] of Object.entries(overrides ?? {})) {
    if (on) out.add(key);
    else out.delete(key);
  }
  return [...out].sort();
}

/**
 * What the Members tab offers on a row (§7.8): a manager edits and removes direct
 * rows; a group-only row is managed on the group (a direct row can still be added
 * on top); nobody but an administrator changes their own membership (§3.6).
 */
export type MemberRowState = "editable" | "group" | "self" | "readonly";

export function memberRowState(
  member: Pick<ProjectMember, "username" | "direct">,
  opts: { canManage: boolean; isAdmin: boolean; me: string | null },
): MemberRowState {
  if (!opts.canManage) return "readonly";
  if (!opts.isAdmin && opts.me !== null && member.username === opts.me) return "self";
  return member.direct ? "editable" : "group";
}

/** `group:<id>` → the group id; null for a direct source. */
export function sourceGroup(source: string): string | null {
  return source.startsWith("group:") ? source.slice("group:".length) : null;
}

export type SourceFilter = "all" | "direct" | "group";

/** The Members tab's filters: role (any source), source, and a search on name / email. */
export function filterMembers(
  members: readonly ProjectMember[],
  filter: { role: string; source: SourceFilter; q: string },
): ProjectMember[] {
  const needle = filter.q.trim().toLowerCase();
  return members.filter((m) => {
    if (filter.role !== "all" && !m.sources.some((s) => s.role_key === filter.role)) return false;
    if (filter.source === "direct" && !m.direct) return false;
    if (filter.source === "group" && !m.group) return false;
    return !needle || `${m.username ?? ""} ${m.email ?? ""} ${m.user_id}`.toLowerCase().includes(needle);
  });
}

/** Direct members holding the owner template (the Overview's "owners"). */
export function projectOwners(members: readonly ProjectMember[]): ProjectMember[] {
  return members.filter((m) => m.sources.some((s) => s.role_key === OWNER_ROLE));
}

export interface SettingsDraft {
  name: string;
  description: string;
  department_id: string;
  group_id: string;
  resource_visibility: "shared" | "isolated";
  sod_strict: boolean;
}

export function settingsDraft(project: ProjectDetail): SettingsDraft {
  return {
    name: project.name,
    description: project.description ?? "",
    department_id: project.department_id ?? "",
    group_id: project.group_id ?? "",
    resource_visibility: project.settings?.resource_visibility === "isolated" ? "isolated" : "shared",
    sod_strict: Boolean(project.settings?.sod_strict),
  };
}

/**
 * The PATCH a Settings save sends: only the changed fields, and only the ones the
 * caller may change — name / department / group and `sod_strict` are an
 * administrator's (the backend refuses them from an owner); description and
 * `resource_visibility` are the owner's too. Null when nothing changed.
 */
export function settingsPatch(project: ProjectDetail, draft: SettingsDraft, isAdmin: boolean): ProjectPatch | null {
  const before = settingsDraft(project);
  const patch: ProjectPatch = {};
  if (isAdmin) {
    if (draft.name.trim() && draft.name.trim() !== before.name) patch.name = draft.name.trim();
    if (draft.department_id !== before.department_id) patch.department_id = draft.department_id || null;
    if (draft.group_id !== before.group_id) patch.group_id = draft.group_id || null;
  }
  if (draft.description !== before.description) patch.description = draft.description;
  const settings: ProjectSettings = {};
  if (draft.resource_visibility !== before.resource_visibility) settings.resource_visibility = draft.resource_visibility;
  if (isAdmin && draft.sod_strict !== before.sod_strict) settings.sod_strict = draft.sod_strict;
  if (Object.keys(settings).length) patch.settings = settings;
  return Object.keys(patch).length ? patch : null;
}

/** The rows a refused unbind left behind (409 `project.workspace_in_use`), by table. */
export function inUseRows(err: unknown): Record<string, number> | null {
  if (!(err instanceof ApiError) || err.code !== "project.workspace_in_use") return null;
  const rows = (err.detail as { rows?: Record<string, number> } | null)?.rows;
  return rows && typeof rows === "object" ? rows : {};
}

/** The members a refused write would have left combining duties (409 `project.sod_conflict`). */
export function sodRefusal(err: unknown): { username: string | null; user_id: string; pairs: string[][] }[] | null {
  if (!(err instanceof ApiError) || err.code !== "project.sod_conflict") return null;
  const conflicts = (err.detail as { conflicts?: unknown } | null)?.conflicts;
  return Array.isArray(conflicts) ? (conflicts as { username: string | null; user_id: string; pairs: string[][] }[]) : [];
}

/**
 * §6.2: an administrator in a workspace that binds no active project sees the
 * "bind a project" banner. Only when the access list is known to list nothing
 * here (a workspace is selected, no project resolved, none bound).
 */
export function showsBindBanner(
  isAdmin: boolean,
  workspaceId: string | null | undefined,
  project: Pick<ProjectContextValue, "projects" | "current" | "boundProjects">,
): boolean {
  return isAdmin && !!workspaceId && project.current === null && project.projects.length === 0 && project.boundProjects === 0;
}

/** The detail page URL (hub-global: the page path stays `/v3/projects`). */
export function projectHref(projectId: string, tab?: ProjectTab): string {
  const q = new URLSearchParams({ id: projectId });
  if (tab && tab !== "overview") q.set("tab", tab);
  return `/v3/projects?${q.toString()}`;
}

/** The list's "bind a project to this workspace" entry the §6.2 banner links to. */
export function bindHref(workspaceId: string): string {
  return `/v3/projects?${new URLSearchParams({ bind: workspaceId }).toString()}`;
}
