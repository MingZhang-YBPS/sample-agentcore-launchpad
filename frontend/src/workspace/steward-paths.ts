import { isSteward } from "../lib/api/steward";

/**
 * Projects design §11.4: the "Configure" pages a steward (structure-only platform
 * administrator) works in — hub-global, no workspace or project data. Every other
 * page is a business page, where a steward sees `StewardEmptyState` instead.
 */
export const STEWARD_STRUCTURE_PATHS: readonly string[] = [
  "/v2/users",
  "/v3/users",
  "/v3/admins",
  "/v3/projects",
  "/v3/project-groups",
  "/v3/departments",
  "/v2/workspaces",
  "/v3/models",
  "/v3/account",
  "/v2/announcements",
  "/v3/announcements",
  "/v2/video-management",
  "/v3/video-management",
];

/** Whether `pathname` is one of the structure pages (or below one). */
export function isStructurePath(pathname: string): boolean {
  return STEWARD_STRUCTURE_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/** Whether the signed-in caller should see the empty state on `pathname`. */
export function showsStewardEmptyState(role: string | null | undefined, pathname: string): boolean {
  return isSteward(role) && !isStructurePath(pathname);
}
