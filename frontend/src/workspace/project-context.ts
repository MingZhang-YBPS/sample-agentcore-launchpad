import { createContext, useContext } from "react";

import type { AccessProject, AccessWorkspace } from "../lib/api/projects";
import { isProjectId } from "../lib/workspace-header";

export interface ProjectContextValue {
  /** the current workspace's reachable projects (every bound one for an admin) */
  projects: AccessProject[];
  /** the project every workspace-scoped request runs in; null ⇒ none sent (backend default) */
  current: AccessProject | null;
  /** more than one project to choose from here: the switcher is shown (§2.7) */
  switchable: boolean;
  /** switch projects: persists the choice and remounts the routed subtree */
  select: (id: string) => void;
  /** re-read `GET /api/me/access` (after a membership or binding change) */
  refresh: () => Promise<void>;
}

export const ProjectContext = createContext<ProjectContextValue>({
  projects: [],
  current: null,
  switchable: false,
  select: () => undefined,
  refresh: async () => undefined,
});

export function useProject() {
  return useContext(ProjectContext);
}

/**
 * Which project a workspace's selection means (§2.6): the first preference that is
 * still reachable — a deep link, then the remembered one — else what a header-less
 * request resolves to (`default_project_id`), else the first. `refused` is an id the
 * backend just turned down, skipped until the access list has been re-read.
 */
export function resolveProject(
  entry: AccessWorkspace | undefined,
  preferred: readonly (string | null | undefined)[],
  refused: string | null = null,
): AccessProject | null {
  const rows = (entry?.projects ?? []).filter((p) => p.id !== refused);
  if (rows.length === 0) return null;
  for (const id of [...preferred, entry?.default_project_id]) {
    const hit = id ? rows.find((p) => p.id === id) : undefined;
    if (hit) return hit;
  }
  return rows[0];
}

/** The switcher only appears when there is a choice: a single-project workspace
 *  (every workshop one) looks exactly as before. */
export function isSwitchable(projects: readonly AccessProject[]): boolean {
  return projects.length > 1;
}

/**
 * `?project=<id>` of a shared page link: the id (null when absent or malformed)
 * and the query string without it — the parameter is consumed, never kept.
 */
export function readProjectLink(search: string): { present: boolean; projectId: string | null; rest: string } {
  const params = new URLSearchParams(search);
  if (!params.has("project")) return { present: false, projectId: null, rest: search };
  const raw = params.get("project");
  params.delete("project");
  const text = params.toString();
  return { present: true, projectId: isProjectId(raw) ? raw : null, rest: text ? `?${text}` : "" };
}

/** The caller's role in a project for the switcher; null for an administrator who
 *  is not a member (shown as "admin access"). The direct membership comes first. */
export function projectRole(project: AccessProject): string | null {
  if (project.bypass) return null;
  const direct = project.role_sources.find((s) => s.source === "direct");
  return (direct ?? project.role_sources[0])?.role_key ?? null;
}

/** Member projects first, then (admins only) the ones reached without a membership. */
export function splitByAccess(projects: readonly AccessProject[]): {
  member: AccessProject[];
  adminAccess: AccessProject[];
} {
  return {
    member: projects.filter((p) => !p.bypass),
    adminAccess: projects.filter((p) => p.bypass),
  };
}

/** Remount key of the routed subtree: a page's data belongs to one workspace and one project. */
export function scopeKey(workspaceId: string | null | undefined, projectId: string | null | undefined): string {
  return `${workspaceId ?? "none"}:${projectId ?? "-"}`;
}
