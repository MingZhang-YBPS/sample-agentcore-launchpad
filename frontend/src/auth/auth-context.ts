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

export function useAuth() {
  return useContext(AuthContext);
}
