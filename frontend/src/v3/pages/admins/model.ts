import type { ConsoleUser } from "../../../lib/api";
import type { AdminAccount } from "../../../lib/api/projects";
import { isSteward, STEWARD_ROLE } from "../../../lib/api/steward";
import type { UserCreateWithProjects } from "../../../lib/api/users";
import { createBody, type CreateErrors, type CreateForm, emptyCreateForm, validateCreate } from "../users/forms";

/**
 * Pure logic of the administrator page (projects §7.3). The backend decides
 * (`can_demote`, 409 `users.last_admin` / `users.self_demote`); these explain a
 * disabled button before a round-trip. Values are i18n keys.
 */

export type DemoteBlock = "builtin" | "last" | "self" | "manage";

/** The page's two account types (§11.4): platform administrator, or steward (structure only). */
export type PlatformType = "admin" | "steward";

/** A row's type: `steward` rows are listed beside the administrators. */
export function platformType(account: Pick<AdminAccount, "role">): PlatformType {
  return isSteward(account.role) ? "steward" : "admin";
}

/** Why "demote" is unavailable on a row, or null when it is allowed. A steward
 * viewing the page manages neither type (`can_manage` false). */
export function demoteBlock(
  admin: Pick<AdminAccount, "builtin" | "can_demote" | "last_signable">,
  canManage = true,
): DemoteBlock | null {
  if (admin.builtin) return "builtin";
  if (admin.can_demote) return null;
  if (!canManage) return "manage";
  return admin.last_signable ? "last" : "self";
}

/** Accounts that could be promoted (to either type): registered, not already an administrator or a steward. */
export function promotable(users: ConsoleUser[]): ConsoleUser[] {
  return users.filter((u) => !u.builtin && u.role !== "admin" && !isSteward(u.role));
}

/** The "new administrator account" form: the create form, fixed to the admin role. */
export function emptyAdminForm(): CreateForm {
  return { ...emptyCreateForm(), role: "admin", workspaces: [], expiry: "never" };
}

export function validateAdminForm(form: CreateForm, reservedName?: string | null): CreateErrors {
  return validateCreate({ ...form, role: "admin" }, reservedName);
}

/**
 * `POST /api/users {role: "admin" | "steward", workspaces: []}` — neither type holds a
 * grant or a membership (`users.admin_no_grants` / `users.steward_no_grants`).
 */
export function adminCreateBody(form: CreateForm, type: PlatformType = "admin"): UserCreateWithProjects {
  const body = createBody({ ...form, role: "admin", workspaces: [], projects: [], groups: [] });
  // the console's role type predates the steward; the backend takes it
  return type === "steward" ? { ...body, role: STEWARD_ROLE as UserCreateWithProjects["role"] } : body;
}
