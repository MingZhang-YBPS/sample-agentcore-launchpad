import { useMemo } from "react";

import { api } from "../../../lib/api";
import { departmentsApi, projectGroupsApi } from "../../../lib/api/project-groups";
import { useLoad } from "../../hooks";

/** id → name lookups of the list's department / group / workspace columns */
export function useStructureNames(isAdmin: boolean) {
  const departments = useLoad(() => departmentsApi.list("active"), "v3-projects:departments");
  // project groups are an administrator's structure (`GET /api/project-groups` is ADMIN)
  const groups = useLoad(
    () => (isAdmin ? projectGroupsApi.list({ status: "active" }) : Promise.resolve({ groups: [] })),
    `v3-projects:groups:${isAdmin}`,
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
