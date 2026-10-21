import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { BudgetsMe, DepartmentCostView, PlatformCostView } from "../../../lib/api/budgets";
import type { ProjectQuotas } from "../../../lib/api/projects";
import { hostedGroups } from "../../nav";
import { allocationProblem, costViews, linesLabel, parseBudget, pickView, spendSignal } from "./budgetModel";
import { DepartmentBudgetPanel, PlatformBudgetPanel, ProjectBudgetPanel } from "./BudgetViews";

const render = (node: JSX.Element) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);
const me = (over: Partial<BudgetsMe> = {}): BudgetsMe => ({ platform: false, departments: [], alerts: [], ...over });

const LINES_OFF = [
  { kind: "warn" as const, fraction: 0.8, usd: 80 },
  { kind: "alert" as const, fraction: 1, usd: 100 },
  { kind: "block" as const, fraction: 1, usd: 100 },
];
const LINES_ON = [
  { kind: "warn" as const, fraction: 0.8, usd: 80 },
  { kind: "alert" as const, fraction: 1.05, usd: 105 },
  { kind: "block" as const, fraction: 1.1, usd: 110 },
];

const row = (id: string, budget: number | null, over: Partial<DepartmentCostView["projects"][number]> = {}) => ({
  project_id: id, name: id.toUpperCase(), status: "active", budget_usd: budget, spent_usd: 10,
  fraction: budget ? 10 / budget : null, allow_overrun: false, block_at_usd: budget, exhausted: false,
  lines: budget ? LINES_OFF : [], basis: "estimate" as const, ...over,
});

const department = (over: Partial<DepartmentCostView> = {}): DepartmentCostView => ({
  department_id: "retail", name: "Retail", monthly_budget_usd: 100, allow_overrun: false,
  allocated_usd: 70, unallocated_usd: 30, spent_usd: 20, fraction: 0.2, alert_fractions: [0.8, 1],
  projects: [row("alpha", 50), row("beta", 20)], alerts: [], trend: [],
  month_start: "2026-10-01T00:00:00+00:00", reset_at: "2026-11-01T00:00:00+00:00", ...over,
});

describe("which cost views a caller can open", () => {
  it("offers the workspace report to administrators and the budgets by permission", () => {
    expect(costViews(true, me({ platform: true, departments: [{ id: "retail", name: "Retail" }] }), true))
      .toEqual(["spend", "alerts", "project", "department", "platform"]);
    // a project member: their project's budget only
    expect(costViews(false, me(), true)).toEqual(["project"]);
    // a department administrator: their project and their department
    expect(costViews(false, me({ departments: [{ id: "retail", name: "Retail" }] }), true)).toEqual(["project", "department"]);
    expect(costViews(false, null, false)).toEqual([]);
  });

  it("falls back to the first view the caller can open", () => {
    expect(pickView("platform", ["project"])).toBe("project");
    expect(pickView("department", ["project", "department"])).toBe("department");
    expect(pickView(null, [])).toBeNull();
  });

  it("shows each rail entry only to whoever can open it", () => {
    const tos = (admin: boolean, dept = false) => hostedGroups(admin, [], dept).flatMap((g) => g.items.map((i) => i.to));
    expect(tos(false)).toContain("/v3/costs?view=project");
    expect(tos(false)).not.toContain("/v3/costs?view=department");
    expect(tos(false)).not.toContain("/v3/costs?view=platform");
    expect(tos(false, true)).toContain("/v3/costs?view=department");
    expect(tos(false, true)).not.toContain("/v3/costs?view=platform");
    expect(tos(true)).toEqual(expect.arrayContaining(["/v3/costs?view=department", "/v3/costs?view=platform"]));
    // a steward's rail stays the structure entries
    expect(hostedGroups(false, [], false, true).flatMap((g) => g.items.map((i) => i.to)))
      .not.toContain("/v3/costs?view=project");
  });
});

describe("the budget model", () => {
  it("parses a budget amount like the backend bounds it", () => {
    expect(parseBudget(" ")).toBeNull();
    expect(parseBudget("12.5")).toBe(12.5);
    expect(parseBudget("-1")).toBeUndefined();
    expect(parseBudget("abc")).toBeUndefined();
    expect(parseBudget("10000001")).toBeUndefined();
  });

  it("checks an allocation against the department's remaining budget", () => {
    const view = department();
    expect(allocationProblem(view, "beta", 50, false)).toBeNull(); // 50 + 50 = 100
    expect(allocationProblem(view, "beta", 51, false)).toBe("overallocated");
    expect(allocationProblem(view, "beta", 5, false)).toBeNull(); // lowering always fits
    expect(allocationProblem(view, "beta", null, false)).toBeNull();
    const unset = department({ monthly_budget_usd: null });
    expect(allocationProblem(unset, "beta", 30, false)).toBe("unset");
    expect(allocationProblem(unset, "beta", 30, true)).toBeNull(); // a platform administrator
  });

  it("labels the 80/100 and 80/105/110 lines and colours the spend", () => {
    expect(linesLabel(LINES_OFF)).toBe("80% · 100% · 100%");
    expect(linesLabel(LINES_ON)).toBe("80% · 105% · 110%");
    expect([spendSignal(0.5), spendSignal(0.8), spendSignal(1), spendSignal(null)]).toEqual(["ok", "wait", "act", "off"]);
  });
});

describe("the budget pages", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  const quotas = (over: Partial<ProjectQuotas> = {}): ProjectQuotas => ({
    project_id: "alpha", department_id: "retail", quotas: { monthly_budget_usd: 100 }, items: [],
    budget: {
      project_id: "alpha", budget_usd: 100, spent_usd: 106, remaining_usd: 0, fraction: 1.06,
      exhausted: false, allow_overrun: true, block_at_usd: 110, lines: LINES_ON,
      month_start: "2026-10-01T00:00:00+00:00", reset_at: "2026-11-01T00:00:00+00:00",
      measured_at: null, detail: "", basis: "estimate",
    },
    budget_basis: "estimate", allow_overrun: true,
    department_budget: { department_id: "retail", name: "Retail", monthly_budget_usd: 500, allow_overrun: true, allocated_usd: 300, unallocated_usd: 200 },
    month_start: "2026-10-01T00:00:00+00:00", reset_at: "2026-11-01T00:00:00+00:00", ...over,
  });

  it("shows a project's budget with its overrun lines and its department", () => {
    const html = render(<ProjectBudgetPanel view={quotas()} />);
    expect(html).toContain("alert at 105% · $105.00");
    expect(html).toContain("blocks at 110% · $110.00");
    expect(html).toContain("overrun allowed — blocks at 110 %");
    expect(html).toContain("Department · Retail");
    expect(html).not.toContain("The budget is used up");
    const off = render(<ProjectBudgetPanel view={quotas({ budget: { ...quotas().budget!, allow_overrun: false, block_at_usd: 100, lines: LINES_OFF, exhausted: true }, allow_overrun: false })} />);
    expect(off).toContain("alert at 100% · $100.00");
    expect(off).toContain("The budget is used up");
    expect(render(<ProjectBudgetPanel view={quotas({ budget: null })} />)).toContain("This project has no monthly budget");
  });

  it("shows a department's allocation, and its editor only to a platform administrator", () => {
    const view = department({ alerts: [{ department_id: "retail", name: "Retail", level: 80, fired_at: "2026-10-20T00:00:00Z", spent_usd: 80, budget_usd: 100 }] });
    const dept = render(<DepartmentBudgetPanel view={view} platform={false} onSaved={() => {}} />);
    expect(dept).toContain("$70.00");
    expect(dept).toContain("$30.00");
    expect(dept).toContain("Retail crossed 80 % of its monthly budget on 2026-10-20.");
    expect(dept).toContain("v3-costs-department-row-alpha");
    expect(dept).not.toContain("v3-costs-department-editor");
    expect(render(<DepartmentBudgetPanel view={view} platform onSaved={() => {}} />)).toContain("v3-costs-department-editor");
    expect(render(<DepartmentBudgetPanel view={department({ monthly_budget_usd: null, unallocated_usd: null })} platform={false} onSaved={() => {}} />))
      .toContain("a platform administrator sets one before you can allocate");
  });

  it("shows the platform page with the departments and the platform line", () => {
    const view: PlatformCostView = {
      departments: [{ ...department(), archived: false }],
      unassigned_projects: [row("default", null)],
      projects_spent_usd: 30, total_usd: 43,
      platform: { spent_usd: 13, workspaces: [{ workspace_id: "default", name: "Hub", estimate_usd: 50, unattributed_usd: 13, components: ["coa_gateway", "shared_memory"], detail: "" }] },
      alerts: [], month_start: "2026-10-01T00:00:00+00:00", reset_at: "2026-11-01T00:00:00+00:00",
    };
    const html = render(<PlatformBudgetPanel view={view} />);
    expect(html).toContain("Platform / unattributed");
    expect(html).toContain("$13.00");
    expect(html).toContain("COA gateway");
    expect(html).toContain("/v3/costs?view=department&amp;id=retail");
    expect(html).toContain("Projects without a department");
  });
});
