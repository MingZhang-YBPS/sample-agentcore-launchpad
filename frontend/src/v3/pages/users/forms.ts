import type { ConsoleRole, UserCreateBody } from "../../../lib/api";
import { DEFAULT_GRANT, MAX_EXTEND_DAYS } from "../../../v2/pages/users/common";

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

export interface CreateForm {
  username: string;
  email: string;
  role: ConsoleRole;
  passwordChoice: PasswordChoice;
  password: string;
  workspaces: string[];
  expiry: ExpiryChoice;
  days: string;
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
  };
}

export type CreateErrors = Partial<Record<"username" | "email" | "password" | "days", string>>;

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
  return errors;
}

/** The request body for a valid form (administrators carry no workspace grant). */
export function createBody(form: CreateForm): UserCreateBody {
  const body: UserCreateBody = {
    username: form.username.trim(),
    email: form.email.trim(),
    role: form.role,
    workspaces: form.role === "admin" ? [] : [...new Set(form.workspaces)],
  };
  if (form.passwordChoice === "set") body.password = form.password;
  if (form.expiry === "days") body.valid_days = Number(form.days);
  if (form.expiry === "never") body.never_expires = true;
  return body;
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
