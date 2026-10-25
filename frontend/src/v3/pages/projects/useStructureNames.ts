import { useMemo } from "react";

import { api } from "../../../lib/api";
import { departmentsApi } from "../../../lib/api/departments";
import { useLoad } from "../../hooks";

/** id → name lookups of the list's department / workspace columns */
export function useStructureNames() {
  const departments = useLoad(() => departmentsApi.list("active"), "v3-projects:departments");
  const workspaces = useLoad(() => api.listWorkspaces(), "v3-projects:workspaces");
  return useMemo(() => {
    const dep = new Map((departments.data?.departments ?? []).map((d) => [d.id, d.name]));
    return {
      departments: departments.data?.departments ?? [],
      workspaces: workspaces.data?.workspaces ?? [],
      department: (id: string | null) => (id ? dep.get(id) ?? id : null),
    };
  }, [departments.data, workspaces.data]);
}
