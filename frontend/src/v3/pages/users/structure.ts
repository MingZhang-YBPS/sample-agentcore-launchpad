import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { type ProjectGroup, projectGroupsApi } from "../../../lib/api/project-groups";
import { type ProjectRoles, type ProjectSummary, projectsApi, type RoleTemplates } from "../../../lib/api/projects";
import { useLoad } from "../../hooks";

export interface RoleOption {
  key: string;
  label: string;
}

/** A role key's label: a built-in template's translated name, else the key itself. */
export function useRoleLabel() {
  const { t } = useTranslation();
  return (key: string) => t(`v3.project.roles.${key}`, { defaultValue: key });
}

/**
 * What the membership dialogs choose from: the active projects, the live project
 * groups and the built-in templates (one fetch each, shared by the page), plus each
 * chosen project's own roles (custom roles, and `inherit` on the default project),
 * fetched once per project as rows pick it.
 */
export function useStructure(enabled = true) {
  const projects = useLoad<ProjectSummary[]>(
    async () => (enabled ? (await projectsApi.list({ status: "active" })).projects : []),
    `v3-users-structure-projects:${enabled}`,
  );
  const groups = useLoad<ProjectGroup[]>(
    async () => (enabled ? (await projectGroupsApi.list()).groups : []),
    `v3-users-structure-groups:${enabled}`,
  );
  const templates = useLoad<RoleTemplates | null>(
    async () => (enabled ? projectsApi.roleTemplates() : null),
    `v3-users-structure-templates:${enabled}`,
  );
  return {
    projects: projects.data ?? [],
    groups: groups.data ?? [],
    templates: templates.data?.templates.map((tpl) => tpl.key) ?? [],
    /** the platform has more than the default project — single-project hubs keep the old dialogs */
    multiProject: (projects.data ?? []).some((p) => !p.is_default),
  };
}

export type Structure = ReturnType<typeof useStructure>;

/** Per-project role options for the rows' chosen projects, fetched lazily and cached. */
export function useProjectRoleOptions(projectIds: string[], templates: string[]) {
  const label = useRoleLabel();
  const [roles, setRoles] = useState<Record<string, ProjectRoles | "error">>({});
  const wanted = useMemo(() => [...new Set(projectIds.filter(Boolean))].sort().join("\n"), [projectIds]);
  useEffect(() => {
    let cancelled = false;
    for (const id of wanted ? wanted.split("\n") : []) {
      if (roles[id]) continue;
      projectsApi.roles(id).then(
        (r) => !cancelled && setRoles((prev) => ({ ...prev, [id]: r })),
        // the templates still apply; a custom role is just not offered
        () => !cancelled && setRoles((prev) => ({ ...prev, [id]: "error" })),
      );
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the cache is read, not watched
  }, [wanted]);
  return (projectId: string): RoleOption[] => {
    const r = roles[projectId];
    if (!r || r === "error") return templates.map((key) => ({ key, label: label(key) }));
    return [
      ...r.templates.map((tpl) => ({ key: tpl.key, label: label(tpl.key) })),
      ...r.custom.map((c) => ({ key: c.key, label: c.name || c.key })),
      ...(r.inherit_available ? [{ key: r.inherit, label: label(r.inherit) }] : []),
    ];
  };
}
