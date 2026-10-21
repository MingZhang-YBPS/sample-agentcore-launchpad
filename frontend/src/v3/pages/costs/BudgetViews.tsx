import { Save } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { type BudgetProjectRow, type BudgetsMe, budgetsApi, type DepartmentCostView, type PlatformCostView } from "../../../lib/api/budgets";
import { type BudgetLine, projectQuotasApi, type ProjectQuotas } from "../../../lib/api/projects";
import { useLoad, useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Select } from "../../select";
import { Btn, Chip, Empty, Lamp, Notice, Panel, Skeleton, Spark, Stat } from "../../ui";
import { allocationProblem, budgetRights, linesLabel, parseBudget, percent, spendSignal } from "./budgetModel";

function usd(value: number | null | undefined): string {
  if (value == null) return "—";
  return value > 0 && value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

/** A spend bar with a tick at each budget line (80 %, 100 / 105 %, the block). */
function BudgetBar({ fraction, lines, testId }: { fraction: number | null; lines: readonly BudgetLine[]; testId?: string }) {
  // the scale reaches the block line, so an overrun budget's 110 % is visible
  const top = Math.max(1, ...lines.map((l) => l.fraction));
  const fill = fraction == null ? 0 : Math.min(1, Math.max(0, fraction / top));
  return (
    <div className="v3-cost-gauge" data-s={spendSignal(fraction)} data-testid={testId} aria-hidden="true">
      <i style={{ width: `${fill * 100}%` }} />
      {lines.map((line) => (
        <span key={`${line.kind}-${line.fraction}`} className="line" style={{ left: `${(line.fraction / top) * 100}%` }} />
      ))}
    </div>
  );
}

/* ── the project page ─────────────────────────────────────────────────── */

/** The current project's budget, switch and lines — every member's. */
export function ProjectBudgetView({ projectId }: { projectId: string }) {
  const loaded = useLoad(() => projectQuotasApi.get(projectId), `v3-costs-project:${projectId}`);
  const view = loaded.data;
  if (loaded.loading && !view) return <Panel><Skeleton rows={4} /></Panel>;
  if (!view) return <Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice>;
  return <ProjectBudgetPanel view={view} />;
}

export function ProjectBudgetPanel({ view }: { view: ProjectQuotas }) {
  const { t } = useTranslation();
  const budget = view.budget;
  const overrun = Boolean(view.allow_overrun);
  const dept = view.department_budget ?? null;
  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="v3-costs-project">
      {budget ? (
        <>
          <div className="v3-grid c4">
            <Panel signal={spendSignal(budget.fraction)}>
              <Stat label={t("v3.costs.budget.spent")} value={usd(budget.spent_usd)}
                foot={budget.fraction == null ? t("v3.costs.budget.notMeasured") : percent(budget.fraction)} />
            </Panel>
            <Panel><Stat label={t("v3.costs.budget.budget")} value={usd(budget.budget_usd)} foot={t("v3.costs.budget.resets", { date: budget.reset_at.slice(0, 10) })} /></Panel>
            <Panel><Stat label={t("v3.costs.budget.remaining")} value={usd(budget.remaining_usd)} /></Panel>
            <Panel signal={budget.exhausted ? "act" : undefined}>
              <Stat label={t("v3.costs.budget.blockAt")} value={usd(budget.block_at_usd ?? budget.budget_usd)}
                foot={overrun ? t("v3.costs.budget.overrunOn") : t("v3.costs.budget.overrunOff")} />
            </Panel>
          </div>
          <Panel title={t("v3.costs.budget.linesTitle")}>
            <div style={{ display: "grid", gap: 10 }}>
              <BudgetBar fraction={budget.fraction} lines={budget.lines ?? []} testId="v3-costs-project-bar" />
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }} data-testid="v3-costs-project-lines">
                {(budget.lines ?? []).map((line) => (
                  <Chip key={`${line.kind}-${line.fraction}`} s={line.kind === "block" ? "act" : "wait"}>
                    {t(`v3.costs.budget.line.${line.kind}`, { pct: percent(line.fraction), usd: usd(line.usd) })}
                  </Chip>
                ))}
              </div>
              {budget.exhausted && <Notice s="act">{t("v3.costs.budget.exhausted", { date: budget.reset_at.slice(0, 10) })}</Notice>}
              <small className="v3-hint">{t("v3.costs.budget.lag", { basis: t(`v3.costs.budget.basis.${budget.basis}`) })}</small>
            </div>
          </Panel>
        </>
      ) : (
        <Panel><Empty title={t("v3.costs.budget.noBudget")}>{t("v3.costs.budget.noBudgetSub")}</Empty></Panel>
      )}
      {dept && (
        <Panel title={t("v3.costs.budget.departmentTitle", { name: dept.name })}>
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }} data-testid="v3-costs-project-department">
            <span><span className="v3-hint">{t("v3.costs.budget.departmentBudget")}</span> <b className="mono">{dept.monthly_budget_usd == null ? t("v3.costs.budget.unlimited") : usd(dept.monthly_budget_usd)}</b></span>
            <span><span className="v3-hint">{t("v3.costs.budget.allocated")}</span> <b className="mono">{usd(dept.allocated_usd)}</b></span>
            <span><span className="v3-hint">{t("v3.costs.budget.overrun")}</span> <b>{dept.allow_overrun ? t("v3.costs.budget.allowed") : t("v3.costs.budget.notAllowed")}</b></span>
          </div>
        </Panel>
      )}
    </div>
  );
}

/* ── the department page ──────────────────────────────────────────────── */

function DepartmentEditor({ view, onSaved }: { view: DepartmentCostView; onSaved: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [text, setText] = useState(view.monthly_budget_usd == null ? "" : String(view.monthly_budget_usd));
  const [overrun, setOverrun] = useState(view.allow_overrun);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    setText(view.monthly_budget_usd == null ? "" : String(view.monthly_budget_usd));
    setOverrun(view.allow_overrun);
  }, [view]);
  const value = parseBudget(text);
  const changed = value !== undefined && (value !== view.monthly_budget_usd || overrun !== view.allow_overrun);
  const below = value != null && value + 1e-6 < view.allocated_usd;
  const save = async () => {
    if (value === undefined) return;
    setBusy(true);
    setError(null);
    try {
      await budgetsApi.putDepartment(view.department_id, { monthly_budget_usd: value, allow_overrun: overrun });
      toast("ok", t("v3.costs.budget.saved"));
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title={t("v3.costs.budget.departmentEdit")}>
      <div style={{ display: "grid", gap: 10 }} data-testid="v3-costs-department-editor">
        <div style={{ display: "flex", gap: 12, alignItems: "end", flexWrap: "wrap" }}>
          <label className="v3-field">
            <span>{t("v3.costs.budget.departmentBudget")}</span>
            <input className="v3-input mono" inputMode="decimal" style={{ maxWidth: 160 }} value={text}
              placeholder={t("v3.costs.budget.unset")} aria-invalid={value === undefined || below}
              onChange={(e) => setText(e.target.value)} />
          </label>
          <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={overrun} onChange={(e) => setOverrun(e.target.checked)} />
            {t("v3.costs.budget.departmentOverrun")}
          </label>
          <Btn kind="primary" size="sm" busy={busy} disabled={!changed || below}
            disabledReason={below ? t("v3.costs.budget.belowAllocated") : t("story.reason.noChanges")}
            onClick={() => void save()}><Save size={13} /> {t("v3.costs.budget.save")}</Btn>
        </div>
        <small className="v3-hint">{t("v3.costs.budget.ceilingHint")}</small>
        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      </div>
    </Panel>
  );
}

function ProjectRow({ view, row, canEdit, onSaved }: {
  view: DepartmentCostView; row: BudgetProjectRow; canEdit: boolean; onSaved: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [text, setText] = useState(row.budget_usd == null ? "" : String(row.budget_usd));
  const [overrun, setOverrun] = useState(row.allow_overrun);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    setText(row.budget_usd == null ? "" : String(row.budget_usd));
    setOverrun(row.allow_overrun);
  }, [row]);
  const value = parseBudget(text);
  const active = row.status === "active";
  const editable = active && canEdit;
  const lockedReason = !canEdit ? t("v3.costs.budget.noBudgetPermission") : t("story.reason.projectArchived");
  const problem = value === undefined ? null : allocationProblem(view, row.project_id, value);
  const changed = value !== undefined && (value !== row.budget_usd || overrun !== row.allow_overrun);
  const overrunLocked = !view.allow_overrun && !row.allow_overrun;
  const save = async () => {
    if (value === undefined) return;
    setBusy(true);
    setError(null);
    try {
      const body: { monthly_budget_usd?: number | null; allow_overrun?: boolean } = {};
      if (value !== row.budget_usd) body.monthly_budget_usd = value;
      if (overrun !== row.allow_overrun) body.allow_overrun = overrun;
      await projectQuotasApi.putBudget(row.project_id, body);
      toast("ok", t("v3.costs.budget.savedProject", { name: row.name }));
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <tr data-testid={`v3-costs-department-row-${row.project_id}`}>
      <td>
        <b>{row.name}</b> <span className="mono v3-hint">{row.project_id}</span>
        {!active && <> <Chip>{t("v3.costs.budget.archived")}</Chip></>}
        {error != null && <div><ApiErrorDetail error={error} /></div>}
      </td>
      <td>
        <Reasoned disabledReason={lockedReason}>
          <input className="v3-input mono" inputMode="decimal" style={{ maxWidth: 120 }} value={text}
            placeholder={t("v3.costs.budget.unset")} disabled={!editable}
            aria-label={t("v3.costs.budget.projectBudgetFor", { name: row.name })}
            aria-invalid={value === undefined || problem !== null}
            onChange={(e) => setText(e.target.value)} />
        </Reasoned>
        {problem && <div><small className="v3-hint" style={{ color: "var(--v3-act)" }}>{t(`v3.costs.budget.problem.${problem}`)}</small></div>}
      </td>
      <td className="num mono">{usd(row.spent_usd)}</td>
      <td className="num">
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          <Lamp s={row.exhausted ? "act" : spendSignal(row.fraction)} />
          {row.fraction == null ? "—" : percent(row.fraction)}
        </span>
        {row.lines.length > 0 && <div><small className="v3-hint">{linesLabel(row.lines)}</small></div>}
      </td>
      <td>
        <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          <Reasoned disabledReason={!editable ? lockedReason : t("v3.costs.budget.overrunLocked")}>
            <input type="checkbox" checked={overrun} disabled={!editable || overrunLocked}
              aria-label={t("v3.costs.budget.overrunFor", { name: row.name })}
              onChange={(e) => setOverrun(e.target.checked)} />
          </Reasoned>
          {overrun ? t("v3.costs.budget.allowed") : t("v3.costs.budget.notAllowed")}
        </label>
      </td>
      <td>
        <Btn size="sm" busy={busy} disabled={!editable || !changed || problem !== null}
          disabledReason={!editable ? lockedReason
            : problem ? t(`v3.costs.budget.problem.${problem}`) : t("story.reason.noChanges")}
          onClick={() => void save()}><Save size={13} /> {t("v3.costs.budget.save")}</Btn>
      </td>
    </tr>
  );
}

/**
 * A department's budget page — its administrators' and the platform's: the ceiling,
 * what is allocated and unallocated, the month's spend and trend, and each project's
 * budget and overrun switch (editable here, within the ceiling, at the organization
 * level with `budget.manage` or by the department's administrators). Only the organization level edits the department's own
 * budget and switch; a department without a budget is unlimited.
 */
export function DepartmentBudgetView({ departments, me }: {
  departments: readonly { id: string; name: string }[];
  /** `GET /api/budgets/me`: where the caller may set budgets */
  me: Pick<BudgetsMe, "budget_scope" | "budget_departments"> | null | undefined;
}) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const asked = params.get("id");
  const id = departments.some((d) => d.id === asked) ? (asked as string) : departments[0]?.id ?? "";
  const [tick, setTick] = useState(0);
  const loaded = useLoad(() => budgetsApi.department(id), `v3-costs-department:${id}:${tick}`);
  const view = loaded.data;
  const refresh = () => setTick((n) => n + 1);
  if (!id) return <Panel><Empty title={t("v3.costs.budget.noDepartments")} /></Panel>;
  const picker = departments.length > 1 && (
    <label className="v3-field" style={{ maxWidth: 280 }}>
      <span>{t("v3.costs.budget.department")}</span>
      <Select className="v3-select" value={id} onChange={(e) => setParams({ view: "department", id: e.target.value })}>
        {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </Select>
    </label>
  );
  if (loaded.loading && !view) return <div style={{ display: "grid", gap: 16 }}>{picker}<Panel><Skeleton rows={4} /></Panel></div>;
  if (!view) return <div style={{ display: "grid", gap: 16 }}>{picker}<Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice></div>;
  return <div style={{ display: "grid", gap: 16 }}>{picker}<DepartmentBudgetPanel view={view} rights={budgetRights(me, id)} onSaved={refresh} /></div>;
}

export function DepartmentBudgetPanel({ view, rights, onSaved }: {
  view: DepartmentCostView; rights: { setDepartment: boolean; allocate: boolean }; onSaved: () => void;
}) {
  const { t } = useTranslation();
  const trend = view.trend.map((p) => p.spent_usd ?? 0);
  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="v3-costs-department">
      {view.alerts.map((alert) => (
        <Notice key={`${alert.department_id}-${alert.level}`} s={alert.level >= 100 ? "act" : "wait"}>
          {t("v3.costs.budget.alert", { level: alert.level, name: alert.name, when: alert.fired_at.slice(0, 10) })}
        </Notice>
      ))}
      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.costs.budget.departmentBudget")} value={view.monthly_budget_usd == null ? t("v3.costs.budget.unlimited") : usd(view.monthly_budget_usd)}
          foot={t("v3.costs.budget.resets", { date: view.reset_at.slice(0, 10) })} /></Panel>
        <Panel><Stat label={t("v3.costs.budget.allocated")} value={usd(view.allocated_usd)} /></Panel>
        <Panel><Stat label={t("v3.costs.budget.unallocated")} value={usd(view.unallocated_usd)} /></Panel>
        <Panel signal={spendSignal(view.fraction)}>
          <Stat label={t("v3.costs.budget.spent")} value={usd(view.spent_usd)} foot={view.fraction == null ? undefined : percent(view.fraction)} />
          <Spark values={trend} s={(view.fraction ?? 0) >= 1 ? "act" : "ok"} />
        </Panel>
      </div>
      {view.monthly_budget_usd == null && (
        <Notice s="info">{rights.setDepartment ? t("v3.costs.budget.unsetPlatform") : t("v3.costs.budget.unsetDepartment")}</Notice>
      )}
      {!rights.allocate && <Notice s="info">{t("v3.costs.budget.readOnly")}</Notice>}
      {rights.setDepartment && <DepartmentEditor view={view} onSaved={onSaved} />}
      <Panel flush title={t("v3.costs.budget.projectsTitle")}>
        {view.projects.length === 0 ? <Empty title={t("v3.costs.budget.noProjects")} /> : (
          <div className="v3-table-scroll">
            <table className="v3-table">
              <thead>
                <tr>
                  <th>{t("v3.costs.colProject")}</th>
                  <th>{t("v3.costs.budget.budget")}</th>
                  <th className="num">{t("v3.costs.budget.spent")}</th>
                  <th className="num">{t("v3.costs.budget.used")}</th>
                  <th>{t("v3.costs.budget.overrun")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {view.projects.map((row) => <ProjectRow key={row.project_id} view={view} row={row} canEdit={rights.allocate} onSaved={onSaved} />)}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

/* ── the platform page ────────────────────────────────────────────────── */

/** Every department and project, and the platform / unattributed line —
 *  platform administrators only. */
export function PlatformBudgetView() {
  const loaded = useLoad(() => budgetsApi.platform(), "v3-costs-platform");
  const view = loaded.data;
  if (loaded.loading && !view) return <Panel><Skeleton rows={4} /></Panel>;
  if (!view) return <Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice>;
  return <PlatformBudgetPanel view={view} />;
}

export function PlatformBudgetPanel({ view }: { view: PlatformCostView }) {
  const { t } = useTranslation();
  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="v3-costs-platform">
      {view.alerts.map((alert) => (
        <Notice key={`${alert.department_id}-${alert.level}`} s={alert.level >= 100 ? "act" : "wait"}>
          {t("v3.costs.budget.alert", { level: alert.level, name: alert.name, when: alert.fired_at.slice(0, 10) })}
        </Notice>
      ))}
      <div className="v3-grid c4">
        <Panel signal="info"><Stat label={t("v3.costs.budget.total")} value={usd(view.total_usd)} foot={t("v3.costs.budget.resets", { date: view.reset_at.slice(0, 10) })} /></Panel>
        <Panel><Stat label={t("v3.costs.budget.projectsSpend")} value={usd(view.projects_spent_usd)} /></Panel>
        <Panel><Stat label={t("v3.costs.budget.platformLine")} value={usd(view.platform.spent_usd)} foot={t("v3.costs.budget.platformLineFoot")} /></Panel>
        <Panel><Stat label={t("v3.costs.budget.departments")} value={view.departments.length} /></Panel>
      </div>
      <Panel flush title={t("v3.costs.budget.byDepartment")}>
        {view.departments.length === 0 ? <Empty title={t("v3.costs.budget.noDepartments")} /> : (
          <table className="v3-table" data-testid="v3-costs-platform-departments">
            <thead>
              <tr>
                <th>{t("v3.costs.budget.department")}</th>
                <th className="num">{t("v3.costs.budget.departmentBudget")}</th>
                <th className="num">{t("v3.costs.budget.allocated")}</th>
                <th className="num">{t("v3.costs.budget.spent")}</th>
                <th className="num">{t("v3.costs.budget.projectsCount")}</th>
              </tr>
            </thead>
            <tbody>
              {view.departments.map((d) => {
                const fraction = d.monthly_budget_usd && d.spent_usd != null ? d.spent_usd / d.monthly_budget_usd : null;
                return (
                  <tr key={d.department_id}>
                    <td>
                      <Link to={`/v3/costs?view=department&id=${encodeURIComponent(d.department_id)}`}><b>{d.name}</b></Link>
                      {d.archived && <> <Chip>{t("v3.costs.budget.archived")}</Chip></>}
                    </td>
                    <td className="num mono">{d.monthly_budget_usd == null ? t("v3.costs.budget.unlimited") : usd(d.monthly_budget_usd)}</td>
                    <td className="num mono">{usd(d.allocated_usd)}</td>
                    <td className="num"><span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                      <Lamp s={spendSignal(fraction)} /><span className="mono">{usd(d.spent_usd)}</span></span></td>
                    <td className="num">{d.projects.length}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {view.unassigned_projects.length > 0 && (
        <Panel flush title={t("v3.costs.budget.unassigned")}>
          <table className="v3-table">
            <thead><tr><th>{t("v3.costs.colProject")}</th><th className="num">{t("v3.costs.budget.budget")}</th><th className="num">{t("v3.costs.budget.spent")}</th></tr></thead>
            <tbody>
              {view.unassigned_projects.map((p) => (
                <tr key={p.project_id}>
                  <td><b>{p.name}</b> <span className="mono v3-hint">{p.project_id}</span></td>
                  <td className="num mono">{p.budget_usd == null ? t("v3.costs.budget.unset") : usd(p.budget_usd)}</td>
                  <td className="num mono">{usd(p.spent_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
      <Panel flush title={t("v3.costs.budget.platformTitle")}>
        <table className="v3-table" data-testid="v3-costs-platform-line">
          <thead>
            <tr>
              <th>{t("v3.costs.budget.workspace")}</th>
              <th className="num">{t("v3.costs.budget.unattributed")}</th>
              <th>{t("v3.costs.budget.components")}</th>
            </tr>
          </thead>
          <tbody>
            {view.platform.workspaces.map((w) => (
              <tr key={w.workspace_id}>
                <td><b>{w.name}</b> <span className="mono v3-hint">{w.workspace_id}</span></td>
                <td className="num mono">{usd(w.unattributed_usd)}</td>
                <td>
                  <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
                    {w.components.map((c) => <Chip key={c}>{t(`v3.costs.budget.component.${c}`, { defaultValue: c })}</Chip>)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ padding: "8px 16px" }}><small className="v3-hint">{t("v3.costs.budget.platformHint")}</small></div>
      </Panel>
    </div>
  );
}
