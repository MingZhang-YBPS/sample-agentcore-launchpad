import type { Department } from "../../../lib/api/project-groups";

/** A department can be deleted only while no project and no group names it (409 `department.in_use`). */
export function deletable(d: Pick<Department, "project_count" | "group_count">): boolean {
  return d.project_count === 0 && d.group_count === 0;
}
