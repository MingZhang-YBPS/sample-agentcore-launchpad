import type { BudgetsMe, DepartmentCostView } from "../../../lib/api/budgets";
import type { BudgetLine } from "../../../lib/api/projects";
import type { Signal } from "../../ui";

/** The Costs page's `?view=` sub-pages. `spend` / `alerts`: the workspace spend
 *  report and its rules (administrators); the three budget dashboards follow. */
export type CostView = "spend" | "alerts" | "project" | "department" | "platform";

/**
 * Which views the caller can open, in order (the backend enforces the same split):
 * the workspace spend report and rules are an administrator's; the current
 * project's budget every member's; a department's page its administrators' and the
 * platform's (`me.departments`); the platform page an administrator's (`me.platform`).
 */
export function costViews(isAdmin: boolean, me: BudgetsMe | null, hasProject: boolean): CostView[] {
  const out: CostView[] = isAdmin ? ["spend", "alerts"] : [];
  if (hasProject) out.push("project");
  if ((me?.departments.length ?? 0) > 0) out.push("department");
  if (me?.platform) out.push("platform");
  return out;
}

/** The asked-for view when the caller can open it, else the first one they can. */
export function pickView(asked: string | null, views: readonly CostView[]): CostView | null {
  if (asked && (views as readonly string[]).includes(asked)) return asked as CostView;
  return views[0] ?? null;
}

/** A fraction as a whole percentage ("105 %" for 1.05). */
export function percent(fraction: number): string {
  return `${Math.round(fraction * 100)}%`;
}

/** The lines as one label: "80% · 100% · block 100%". */
export function linesLabel(lines: readonly BudgetLine[]): string {
  return lines.map((line) => percent(line.fraction)).join(" · ");
}

/** Green below 80 %, amber from 80 %, red from 100 % (the department's lines). */
export function spendSignal(fraction: number | null | undefined): Signal {
  if (fraction == null) return "off";
  return fraction >= 1 ? "act" : fraction >= 0.8 ? "wait" : "ok";
}

/**
 * A typed budget amount: `null` = clear it (empty input), `undefined` = invalid. The
 * backend bounds (0 – 10 000 000 USD) are repeated here so the save stays disabled.
 */
export function parseBudget(text: string): number | null | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0 || value > 10_000_000) return undefined;
  return value;
}

/**
 * What the caller may change on a department's page, from `GET /api/budgets/me`
 * (the backend enforces the same): the department's own budget and switch at the
 * organization level only; its projects' budgets at the organization level, or as an
 * administrator of that department — the organization level needs the
 * `budget.manage` key, a department administrator needs none.
 */
export function budgetRights(me: Pick<BudgetsMe, "budget_scope" | "budget_departments"> | null | undefined,
  departmentId: string): { setDepartment: boolean; allocate: boolean } {
  const platform = me?.budget_scope === "platform";
  const department = me?.budget_scope === "department" && (me.budget_departments ?? []).includes(departmentId);
  return { setDepartment: platform, allocate: platform || department };
}

/**
 * Why a project's new budget would be refused by its department, before asking the
 * backend (409 `department.budget_overallocated`): `null` when it fits. Only an
 * increase is checked, like the backend; a department without a budget is unlimited.
 */
export function allocationProblem(
  view: Pick<DepartmentCostView, "monthly_budget_usd" | "projects">,
  projectId: string,
  next: number | null,
): "overallocated" | null {
  const row = view.projects.find((p) => p.project_id === projectId);
  const current = row?.budget_usd ?? null;
  if (next === null || (current !== null && next <= current)) return null;
  if (view.monthly_budget_usd === null) return null;
  const others = view.projects
    .filter((p) => p.project_id !== projectId)
    .reduce((sum, p) => sum + (p.budget_usd ?? 0), 0);
  return others + next > view.monthly_budget_usd + 1e-6 ? "overallocated" : null;
}
