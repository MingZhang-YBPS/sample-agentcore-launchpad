import { useMemo } from "react";

import { api } from "../../../lib/api";
import { departmentsApi, projectGroupsApi } from "../../../lib/api/project-groups";
import { useProject } from "../../../workspace/project-context";
import { useLoad } from "../../hooks";
import { groupsOfDepartments } from "../groups/model";

/** id → name lookups of the list's department / group / workspace columns */
export function useStructureNames(isAdmin: boolean) {
  const { managedDepartments } = useProject();
  const managed = isAdmin ? "" : managedDepartments.map((d) => d.id).join(",");
  const departments = useLoad(() => departmentsApi.list("active"), "v3-projects:departments");
  // project groups are an administrator's structure — and a department
  // administrator's for their departments (§11.1: the list needs `department_id`)
  const groups = useLoad(
    async () => {
      if (isAdmin) return projectGroupsApi.list({ status: "active" });
      if (!managed) return { groups: [] };
      const pages = await Promise.all(managed.split(",").map((d) => projectGroupsApi.list({ status: "active", department_id: d })));
      return { groups: groupsOfDepartments(pages) };
    },
    `v3-projects:groups:${isAdmin}:${managed}`,
  );
  const workspaces = useLoad(() => api.listWorkspaces(), "v3-projects:workspaces");
  return useMemo(() => {
    const dep = new Map((departments.data?.departments ?? []).map((d) => [d.id, d.name]));
    const grp = new Map((groups.data?.groups ?? []).map((g) => [g.id, g]));
    return {
      departments: departments.data?.departments ?? [],
      groups: groups.data?.groups ?? [],
      workspaces: workspaces.data?.workspaces ?? [],
      department: (id: string | null) => (id ? dep.get(id) ?? id : null),
      group: (id: string | null) => (id ? grp.get(id)?.name ?? id : null),
    };
  }, [departments.data, groups.data, workspaces.data]);
}
