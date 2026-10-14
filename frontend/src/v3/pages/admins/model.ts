import type { ConsoleUser } from "../../../lib/api";
import type { AdminAccount } from "../../../lib/api/projects";
import type { UserCreateWithProjects } from "../../../lib/api/users";
import { createBody, type CreateErrors, type CreateForm, emptyCreateForm, validateCreate } from "../users/forms";

/**
 * Pure logic of the administrator page (projects §7.3). The backend decides
 * (`can_demote`, 409 `users.last_admin` / `users.self_demote`); these explain a
 * disabled button before a round-trip. Values are i18n keys.
 */

export type DemoteBlock = "builtin" | "last" | "self";

/** Why "demote" is unavailable on a row, or null when it is allowed. */
export function demoteBlock(admin: Pick<AdminAccount, "builtin" | "can_demote" | "last_signable">): DemoteBlock | null {
  if (admin.builtin) return "builtin";
  if (admin.can_demote) return null;
  return admin.last_signable ? "last" : "self";
}

/** Accounts an administrator could be promoted from: registered, not already admin. */
export function promotable(users: ConsoleUser[]): ConsoleUser[] {
  return users.filter((u) => !u.builtin && u.role !== "admin");
}

/** The "new administrator account" form: the create form, fixed to the admin role. */
export function emptyAdminForm(): CreateForm {
  return { ...emptyCreateForm(), role: "admin", workspaces: [], expiry: "never" };
}

export function validateAdminForm(form: CreateForm, reservedName?: string | null): CreateErrors {
  return validateCreate({ ...form, role: "admin" }, reservedName);
}

/** `POST /api/users {role: "admin", workspaces: []}` — an administrator holds no grant (`users.admin_no_grants`). */
export function adminCreateBody(form: CreateForm): UserCreateWithProjects {
  return createBody({ ...form, role: "admin", workspaces: [], projects: [], groups: [] });
}
