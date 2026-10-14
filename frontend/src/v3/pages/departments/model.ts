import type { Department, DepartmentPatch, DepartmentSettings, DepartmentWorkspace } from "../../../lib/api/project-groups";

/** A department can be deleted only while no project and no group names it (409 `department.in_use`). */
export function deletable(d: Pick<Department, "project_count" | "group_count">): boolean {
  return d.project_count === 0 && d.group_count === 0;
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
