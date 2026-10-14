import type { ConsoleRole } from "../../../lib/api";
import type {
  GroupMembershipInput,
  ProjectMembershipInput,
  ProjectUser,
  UserCreateWithProjects,
  UserListFilter,
  UserMembershipPatch,
} from "../../../lib/api/users";
import { DEFAULT_GRANT, isStatusFilter, MAX_EXTEND_DAYS } from "../../../v2/pages/users/common";

/**
 * Pure form logic of the Users page dialogs (create account, change password),
 * kept out of the components so it is unit-tested. The rules mirror the backend's
 * (`services/users.py`): the backend stays authoritative and re-checks everything;
 * these only catch mistakes before a round-trip. Values are i18n keys.
 */

/** Same pattern as `services/users._USERNAME_RE`. */
export const USERNAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$/;
/** Same pattern as `services/users._EMAIL_RE`. */
export const EMAIL_RE = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9]([A-Za-z0-9.-]{0,251})\.[A-Za-z]{2,24}$/;
export const MIN_PASSWORD_LENGTH = 8;

export type ExpiryChoice = "default" | "days" | "never";
export type PasswordChoice = "generate" | "set";

/** One project (or project-group) row of a dialog: the target id and a role key. */
export interface MembershipRow {
  id: string;
  role: string;
}

export interface CreateForm {
  username: string;
  email: string;
  role: ConsoleRole;
  passwordChoice: PasswordChoice;
  password: string;
  workspaces: string[];
  expiry: ExpiryChoice;
  days: string;
  projects: MembershipRow[];
  groups: MembershipRow[];
}

export function emptyCreateForm(): CreateForm {
  return {
    username: "",
    email: "",
    role: "member",
    passwordChoice: "generate",
    password: "",
    workspaces: [DEFAULT_GRANT],
    expiry: "default",
    days: "30",
    projects: [],
    groups: [],
  };
}

export type CreateErrors = Partial<Record<"username" | "email" | "password" | "days" | "projects" | "groups", string>>;

/**
 * A membership list's error key, or null: every row names a target and a role,
 * and no target twice (the backend refuses both).
 */
export function validateMemberships(rows: MembershipRow[], kind: "projects" | "groups"): string | null {
  if (rows.some((row) => !row.id || !row.role)) return `v3.users.memberships.err.${kind}Incomplete`;
  const ids = rows.map((row) => row.id);
  if (new Set(ids).size !== ids.length) return `v3.users.memberships.err.${kind}Duplicate`;
  return null;
}

export function projectItems(rows: MembershipRow[]): ProjectMembershipInput[] {
  return rows.map((row) => ({ project_id: row.id, role_key: row.role }));
}

export function groupItems(rows: MembershipRow[]): GroupMembershipInput[] {
  return rows.map((row) => ({ group_id: row.id, role_key: row.role }));
}

export function validateCreate(form: CreateForm, reservedName?: string | null): CreateErrors {
  const errors: CreateErrors = {};
  const name = form.username.trim();
  if (!USERNAME_RE.test(name)) errors.username = "v3.users.create.err.username";
  else if (reservedName && name.toLowerCase() === reservedName.trim().toLowerCase()) errors.username = "v3.users.create.err.reserved";
  if (!EMAIL_RE.test(form.email.trim().toLowerCase())) errors.email = "v3.users.create.err.email";
  if (form.passwordChoice === "set" && form.password.length < MIN_PASSWORD_LENGTH) errors.password = "v3.users.create.err.password";
  if (form.expiry === "days") {
    const days = Number(form.days);
    if (!Number.isInteger(days) || days < 1 || days > MAX_EXTEND_DAYS) errors.days = "v3.users.create.err.days";
  }
  if (form.role !== "admin") {
    const projects = validateMemberships(form.projects, "projects");
    const groups = validateMemberships(form.groups, "groups");
    if (projects) errors.projects = projects;
    if (groups) errors.groups = groups;
  }
  return errors;
}

/**
 * The request body for a valid form. Administrators carry no workspace grant and no
 * membership (they reach every project by role); empty membership lists are left
 * out, so a form without projects sends exactly what it did before projects.
 */
export function createBody(form: CreateForm): UserCreateWithProjects {
  const admin = form.role === "admin";
  const body: UserCreateWithProjects = {
    username: form.username.trim(),
    email: form.email.trim(),
    role: form.role,
    workspaces: admin ? [] : [...new Set(form.workspaces)],
  };
  if (form.passwordChoice === "set") body.password = form.password;
  if (form.expiry === "days") body.valid_days = Number(form.days);
  if (form.expiry === "never") body.never_expires = true;
  if (!admin && form.projects.length > 0) body.projects = projectItems(form.projects);
  if (!admin && form.groups.length > 0) body.groups = groupItems(form.groups);
  return body;
}

/* ── approval (projects §7.5) ────────────────────────────────────────────── */

export interface ApproveForm {
  role: "member" | "operator";
  /** legacy workspace grants: `default` joins the default project as `inherit` */
  workspaces: string[];
  projects: MembershipRow[];
  groups: MembershipRow[];
}

export function emptyApproveForm(user: Pick<ProjectUser, "role" | "workspaces">): ApproveForm {
  return {
    role: (user.role as string) === "operator" ? "operator" : "member",
    workspaces: user.workspaces.length > 0 ? user.workspaces : [DEFAULT_GRANT],
    projects: [],
    groups: [],
  };
}

export function validateApprove(form: ApproveForm): Partial<Record<"projects" | "groups", string>> {
  const errors: Partial<Record<"projects" | "groups", string>> = {};
  const projects = validateMemberships(form.projects, "projects");
  const groups = validateMemberships(form.groups, "groups");
  if (projects) errors.projects = projects;
  if (groups) errors.groups = groups;
  return errors;
}

/**
 * `PATCH /api/users/{id}` of an approval: active, the grants as before, and the
 * projects / groups when any were chosen. `role` only when it changes, so an
 * approval without choices is the same request as before projects.
 */
export function approveBody(form: ApproveForm, currentRole: ConsoleRole | "operator"): UserMembershipPatch {
  const body: UserMembershipPatch = { status: "active", workspaces: [...new Set(form.workspaces)] };
  if (form.role !== currentRole) body.role = form.role;
  if (form.projects.length > 0) body.projects = projectItems(form.projects);
  if (form.groups.length > 0) body.groups = groupItems(form.groups);
  return body;
}

/* ── memberships of one account ─────────────────────────────────────────── */

/**
 * The direct project rows an editor starts from: a project the account holds
 * directly, with that direct role. The default project's `inherit` row follows the
 * workspace grants (the backend spares it on replacement), so it is not edited here.
 */
export function directProjectRows(user: Pick<ProjectUser, "projects">): MembershipRow[] {
  const rows: MembershipRow[] = [];
  for (const project of user.projects ?? []) {
    const direct = project.sources.find((s) => s.source === "direct");
    if (!direct || (project.is_default && direct.role_key === "inherit")) continue;
    rows.push({ id: project.id, role: direct.role_key });
  }
  return rows;
}

export function groupRows(user: Pick<ProjectUser, "groups">): MembershipRow[] {
  return (user.groups ?? []).map((g) => ({ id: g.id, role: g.role }));
}

/** `group:<id>` → the group id; `direct` → null. */
export function sourceGroup(source: string): string | null {
  return source.startsWith("group:") ? source.slice("group:".length) : null;
}

/* ── list filters in the URL (projects §7.4) ─────────────────────────────── */

const ROLES = ["admin", "operator", "member"] as const;

/** `?status= &q= &role= &project= &group= &page=` → the list request. */
export function listFilterFrom(params: URLSearchParams, pageSize: number): UserListFilter & { page: number } {
  const status = params.get("status");
  const role = params.get("role");
  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);
  return {
    q: params.get("q") || undefined,
    status: isStatusFilter(status) ? status : "all",
    role: (ROLES as readonly string[]).includes(role ?? "") ? (role as UserListFilter["role"]) : undefined,
    project: params.get("project") || undefined,
    group: params.get("group") || undefined,
    limit: pageSize,
    offset: (page - 1) * pageSize,
    page,
  };
}

export interface PasswordChangeForm {
  current: string;
  next: string;
  confirm: string;
}

export type PasswordChangeErrors = Partial<Record<keyof PasswordChangeForm, string>>;

export function validatePasswordChange(form: PasswordChangeForm): PasswordChangeErrors {
  const errors: PasswordChangeErrors = {};
  if (!form.current) errors.current = "v3.account.password.err.current";
  if (form.next.length < MIN_PASSWORD_LENGTH) errors.next = "v3.users.create.err.password";
  else if (form.next === form.current) errors.next = "v3.account.password.err.same";
  if (form.confirm !== form.next) errors.confirm = "v3.account.password.err.mismatch";
  return errors;
}

/**
 * The one-time password view: a response carries `generated_password` exactly once
 * (create / reset); the dialog shows it until closed, and nothing else keeps it.
 */
export function oneTimePassword(
  response: { username: string; generated_password?: string },
  kind: "created" | "reset",
): { username: string; password: string; kind: "created" | "reset" } | null {
  return response.generated_password ? { username: response.username, password: response.generated_password, kind } : null;
}

/** The role a new row starts with: the template the global role suggests (§3.7), else the first offered. */
export function suggestedRole(options: { key: string }[], globalRole: ConsoleRole | "operator" = "member"): string {
  const suggested = globalRole === "operator" ? "release_ops" : "developer";
  return options.some((o) => o.key === suggested) ? suggested : (options[0]?.key ?? "");
}
