import { createContext, useContext } from "react";

import type { AgentPermission, ConsoleRole } from "../lib/api";

export interface AuthContextValue {
  authRequired: boolean;
  username: string | null;
  role: ConsoleRole | null;
  email: string | null;
  /** ISO account-validity end for registered users; null = never expires */
  accountExpiresAt: string | null;
  /** the config-driven built-in admin (its password replaces the configured one) */
  builtin: boolean;
  /** where the signed-in account came from (sso §2.7): an SSO account has no
   *  console password to change; null = not signed in / an open console */
  accountSource: "local" | "sso" | null;
  /** admin-only surfaces (an open console keeps full local-operator access) */
  isAdmin: boolean;
  /** member-grantable agent-management capability (admins always pass). Inside a
   *  project (`ProjectProvider`) it reads that project's effective keys instead of
   *  the account's global ones (projects design §6.1). */
  can: (permission: AgentPermission) => boolean;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue>({
  authRequired: false,
  username: null,
  role: null,
  email: null,
  accountExpiresAt: null,
  builtin: false,
  accountSource: null,
  isAdmin: true,
  can: () => true,
  logout: async () => undefined,
});

/**
 * `can()` for a caller inside a project: an administrator passes; otherwise the
 * project's effective keys decide — a project role replaces the global grants, it
 * does not add to them (§2.5). Outside a project (`null`) the global check stands.
 */
export function projectCan(
  isAdmin: boolean,
  projectKeys: readonly string[] | null,
  globalCan: (permission: AgentPermission) => boolean,
): (permission: AgentPermission) => boolean {
  if (isAdmin) return () => true;
  if (projectKeys === null) return globalCan;
  const granted = new Set(projectKeys);
  return (permission) => granted.has(permission);
}

/**
 * Whether the signed-in account has a console password to change: an open console
 * has no accounts, and an SSO account signs in at its identity provider (sso §2.7).
 */
export function hasConsolePassword(authRequired: boolean, accountSource: "local" | "sso" | null): boolean {
  return authRequired && accountSource !== "sso";
}

export function useAuth() {
  return useContext(AuthContext);
}
