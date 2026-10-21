/**
 * Typed client of the budget dashboards (projects §11.3): the department cost page
 * (`/api/departments/{id}/budget`), the platform cost page (`/api/budgets/platform`)
 * and which of them the caller may open (`/api/budgets/me`). Budgets are project
 * budgets; a department budget only caps what its projects may be allocated, and
 * never blocks an operation itself. The project page is `projectQuotasApi`.
 */
import { getJson, sendJson } from "../api";
import { departmentPath } from "./departments";
import type { BudgetLine } from "./projects";

/** One project's row on the department / platform page. */
export interface BudgetProjectRow {
  project_id: string;
  name: string;
  status: string;
  budget_usd: number | null;
  /** month-to-date spend (the gate's figure); null when unreadable */
  spent_usd: number | null;
  fraction: number | null;
  allow_overrun: boolean;
  block_at_usd: number | null;
  exhausted: boolean;
  lines: BudgetLine[];
  basis: "estimate" | "billing";
}

/** A department budget line crossed this UTC month (notify only). */
export interface DepartmentBudgetAlert {
  department_id: string;
  name: string;
  level: number;
  fired_at: string;
  spent_usd: number | null;
  budget_usd: number;
}

export interface DepartmentCostView {
  department_id: string;
  name: string;
  /** the allocation ceiling; null = no department budget */
  monthly_budget_usd: number | null;
  allow_overrun: boolean;
  allocated_usd: number;
  unallocated_usd: number | null;
  spent_usd: number | null;
  fraction: number | null;
  alert_fractions: number[];
  projects: BudgetProjectRow[];
  alerts: DepartmentBudgetAlert[];
  trend: { date: string; spent_usd: number | null }[];
  month_start: string;
  reset_at: string;
}

/** `PUT /api/departments/{id}/budget` — platform administrators only; partial. */
export interface DepartmentBudgetPut {
  monthly_budget_usd?: number | null;
  allow_overrun?: boolean;
}

export interface PlatformWorkspaceLine {
  workspace_id: string;
  name: string;
  estimate_usd: number | null;
  /** the month-to-date estimate no project carries (0 in a single-project workspace) */
  unattributed_usd: number | null;
  /** shared infrastructure the estimate cannot price (see the billing view) */
  components: string[];
  detail: string;
}

export interface PlatformCostView {
  departments: (Omit<DepartmentCostView, "unallocated_usd" | "fraction" | "alert_fractions" | "alerts" | "trend" | "month_start" | "reset_at"> & { archived: boolean })[];
  unassigned_projects: BudgetProjectRow[];
  projects_spent_usd: number | null;
  platform: { spent_usd: number | null; workspaces: PlatformWorkspaceLine[] };
  total_usd: number | null;
  alerts: DepartmentBudgetAlert[];
  month_start: string;
  reset_at: string;
}

/** Which cost views the caller may open, and its department alerts. */
export interface BudgetsMe {
  platform: boolean;
  departments: { id: string; name: string }[];
  alerts: DepartmentBudgetAlert[];
  /** where the caller may set budgets (`budget.manage` + identity): every department
   *  and project ("platform"), the projects of `budget_departments` ("department"),
   *  or nowhere (null) */
  budget_scope: "platform" | "department" | null;
  budget_departments: string[];
}

export const budgetsApi = {
  me: () => getJson<BudgetsMe>("/api/budgets/me"),
  platform: () => getJson<PlatformCostView>("/api/budgets/platform"),
  department: (id: string) => getJson<DepartmentCostView>(departmentPath(id, "budget")),
  putDepartment: (id: string, body: DepartmentBudgetPut) =>
    sendJson<DepartmentCostView>(departmentPath(id, "budget"), "PUT", body),
};
