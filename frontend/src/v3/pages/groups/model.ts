import type { GroupProject, ProjectGroup } from "../../../lib/api/project-groups";
import type { ProjectSummary } from "../../../lib/api/projects";

/**
 * Pure logic of the project-group and department pages (projects §7.7). The
 * backend re-checks everything; values are i18n keys.
 */

/** Same pattern as `models/projects.PROJECT_ID_PATTERN` (groups and departments share it). */
export const STRUCTURE_ID_RE = /^[a-z0-9][a-z0-9-]{1,31}$/;

export interface StructureForm {
  id: string;
  name: string;
  description: string;
  /** groups only; "" = none */
  department_id: string;
}

export type StructureErrors = Partial<Record<"id" | "name", string>>;

export function emptyStructureForm(): StructureForm {
  return { id: "", name: "", description: "", department_id: "" };
}

/** The id is checked on create only — it never changes afterwards. */
export function validateStructure(form: StructureForm, creating: boolean): StructureErrors {
  const errors: StructureErrors = {};
  if (creating && !STRUCTURE_ID_RE.test(form.id.trim())) errors.id = "v3.groups.form.err.id";
  if (!form.name.trim()) errors.name = "v3.groups.form.err.name";
  return errors;
}

/** A display name → a suggested id ("Risk Platform" → "risk-platform"); the admin may edit it. */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32)
    .replace(/-+$/g, "");
}

export interface ProjectChoice {
  id: string;
  name: string;
  /** another group holds it: a project is in at most one group (409 `group.project_in_other_group`) */
  otherGroup: string | null;
}

/** Projects that could join the group: active, not already in it; those in another group are flagged. */
export function projectChoices(projects: ProjectSummary[], group: Pick<ProjectGroup, "id" | "projects">): ProjectChoice[] {
  const inGroup = new Set(group.projects.map((p) => p.id));
  return projects
    .filter((p) => p.status === "active" && !inGroup.has(p.id))
    .map((p) => ({ id: p.id, name: p.name, otherGroup: p.group_id && p.group_id !== group.id ? p.group_id : null }))
    .sort((a, b) => Number(a.otherGroup !== null) - Number(b.otherGroup !== null) || a.name.localeCompare(b.name));
}

/** "a, b, c +2" of a group's projects for the list. */
export function projectSummary(projects: GroupProject[], max = 3): { names: string; more: number } {
  return { names: projects.slice(0, max).map((p) => p.name).join(", "), more: Math.max(0, projects.length - max) };
}
