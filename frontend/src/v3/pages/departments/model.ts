import type { Department, DepartmentPatch, DepartmentSettings, DepartmentWorkspace } from "../../../lib/api/departments";

/**
 * Pure logic of the department pages (projects §7.10, §11.1). The backend
 * re-checks everything; values are i18n keys.
 */

/** Same pattern as `models/projects.PROJECT_ID_PATTERN`. */
export const STRUCTURE_ID_RE = /^[a-z0-9][a-z0-9-]{1,31}$/;

export interface StructureForm {
  id: string;
  name: string;
  description: string;
}

export type StructureErrors = Partial<Record<"id" | "name", string>>;

export function emptyStructureForm(): StructureForm {
  return { id: "", name: "", description: "" };
}

/** The id is checked on create only — it never changes afterwards. */
export function validateStructure(form: StructureForm, creating: boolean): StructureErrors {
  const errors: StructureErrors = {};
  if (creating && !STRUCTURE_ID_RE.test(form.id.trim())) errors.id = "v3.structure.form.err.id";
  if (!form.name.trim()) errors.name = "v3.structure.form.err.name";
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

/** A department can be deleted only while no project names it (409 `department.in_use`). */
export function deletable(d: Pick<Department, "project_count">): boolean {
  return d.project_count === 0;
}

/* ── §11.1 department administrators: settings form and workspace choices ── */

export interface SettingsForm {
  canCreateUsers: boolean;
  /** "" = no cap */
  maxProjects: string;
  validityDays: string;
}

export function settingsForm(s: DepartmentSettings): SettingsForm {
  return {
    canCreateUsers: s.can_create_users,
    maxProjects: s.max_projects == null ? "" : String(s.max_projects),
    validityDays: String(s.user_validity_max_days),
  };
}

/** A whole number in 1..max; "" is `null` when `blank` allows it; anything else `undefined`. */
export function parseCount(text: string, max: number, blank = false): number | null | undefined {
  const value = text.trim();
  if (!value) return blank ? null : undefined;
  if (!/^\d+$/.test(value)) return undefined;
  const n = Number(value);
  return n >= 1 && n <= max ? n : undefined;
}

export type SettingsErrors = Partial<Record<"maxProjects" | "validityDays", string>>;

/** The PATCH `settings` body from the form, or the i18n keys of its errors. */
export function settingsPatch(form: SettingsForm): { settings: NonNullable<DepartmentPatch["settings"]> } | { errors: SettingsErrors } {
  const maxProjects = parseCount(form.maxProjects, 10000, true);
  const validity = parseCount(form.validityDays, 3650);
  const errors: SettingsErrors = {};
  if (maxProjects === undefined) errors.maxProjects = "v3.departments.settings.err.count";
  if (validity === undefined || validity === null) errors.validityDays = "v3.departments.settings.err.days";
  if (Object.keys(errors).length) return { errors };
  return { settings: { can_create_users: form.canCreateUsers, max_projects: maxProjects ?? null, user_validity_max_days: validity as number } };
}

/** Workspaces not yet opened to the department, by name. */
export function unopened<W extends { id: string; name: string }>(all: W[], opened: Pick<DepartmentWorkspace, "workspace_id">[]): W[] {
  const taken = new Set(opened.map((w) => w.workspace_id));
  return all.filter((w) => !taken.has(w.id)).sort((a, b) => a.name.localeCompare(b.name));
}
