import { Save } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { type ProjectBudget, type ProjectDetail, projectQuotasApi } from "../../../lib/api/projects";
import { useLoad, useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { permReason } from "../../reasons";
import { Btn, Chip, Lamp, Notice, Panel, Skeleton } from "../../ui";
import type { ProjectAbilities } from "./ProjectDetail";
import { budgetProgress, formatQuota, type QuotaDraft, quotaDraft, quotaErrors, quotaPatch, usageSignal } from "./quotasModel";

const usd = (value: number | null | undefined) => formatQuota({ unit: "usd" }, value);

/** The monthly budget: spent this month, remaining, a progress bar and the reset date. */
function BudgetSummary({ budget }: { budget: ProjectBudget }) {
  const { t } = useTranslation();
  const progress = budgetProgress(budget);
  const date = budget.reset_at.slice(0, 10);
  return (
    <div style={{ display: "grid", gap: 8 }} data-testid="project-budget">
      <div className="v3-prj-usage">
        <div className="row">
          <span className="label">
            <Lamp s={progress?.signal ?? "off"} /> {t("v3.projects.quotas.budget.title")}
          </span>
          <span className="mono" data-testid="project-budget-spent">
            {t("v3.projects.quotas.budget.of", { spent: usd(budget.spent_usd), budget: usd(budget.budget_usd) })}
          </span>
          <div className="bar"><i data-s={progress?.signal ?? "off"} style={{ width: `${Math.round((progress?.fraction ?? 0) * 100)}%` }} /></div>
        </div>
      </div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <span><span className="v3-hint">{t("v3.projects.quotas.budget.spent")}</span> <b className="mono">{usd(budget.spent_usd)}</b></span>
        <span><span className="v3-hint">{t("v3.projects.quotas.budget.remaining")}</span> <b className="mono" data-testid="project-budget-remaining">{usd(budget.remaining_usd)}</b></span>
      </div>
      <small className="v3-hint">{t("v3.projects.quotas.budget.reset", { date })}</small>
      {progress === null && <Notice s="wait">{t("v3.projects.quotas.budget.notMeasured")}</Notice>}
      {progress?.blocked && <Notice s="act">{t("v3.projects.quotas.budget.exhausted", { date })}</Notice>}
      {progress && !progress.blocked && progress.signal === "wait" && (
        <Notice s="wait">{t("v3.projects.quotas.budget.warning")}</Notice>
      )}
      <small className="v3-hint">
        {t("v3.projects.quotas.budget.lag", { basis: t(`v3.projects.quotas.budget.basis.${budget.basis}`) })}
      </small>
    </div>
  );
}

/**
 * §11.3 "Quotas and monthly budget": every member (and the project's department
 * administrator) sees usage against the limits and the budget's month-to-date spend,
 * remaining and reset date (the 1st, UTC); only a platform administrator edits them
 * here (a department administrator sets the budget on /v3/costs?view=department).
 */
export function QuotasSection({ project, abilities }: { project: ProjectDetail; abilities: ProjectAbilities }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [tick, setTick] = useState(0);
  const loaded = useLoad(() => projectQuotasApi.get(project.id), `v3-project-quotas:${project.id}:${tick}`);
  const view = loaded.data;
  const [draft, setDraft] = useState<QuotaDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (view) setDraft(quotaDraft(view));
  }, [view]);
  // platform administrators only (no department overlay on PUT .../quotas)
  const editable = abilities.admin && project.status === "active";

  if (loaded.loading && !view) return <Panel title={t("v3.projects.quotas.title")}><Skeleton rows={4} /></Panel>;
  if (!view || !draft) {
    return (
      <Panel title={t("v3.projects.quotas.title")}>
        <Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice>
      </Panel>
    );
  }

  const invalid = quotaErrors(view, draft);
  const patch = quotaPatch(view, draft);
  const save = async () => {
    if (!patch) return;
    setBusy(true);
    setError(null);
    try {
      await projectQuotasApi.put(project.id, patch);
      toast("ok", t("v3.projects.quotas.saved", { name: project.name }));
      setTick((n) => n + 1);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={t("v3.projects.quotas.title")}>
      <div style={{ display: "grid", gap: 12 }} data-testid="project-quotas">
        {view.budget ? <BudgetSummary budget={view.budget} /> : (
          <small className="v3-hint" data-testid="project-budget-none">{t("v3.projects.quotas.budget.none")}</small>
        )}
        <small className="v3-hint">{t("v3.projects.quotas.sub")}</small>
        <div className="v3-table-scroll">
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v3.projects.quotas.col.quota")}</th>
                <th>{t("v3.projects.quotas.col.used")}</th>
                <th>{t("v3.projects.quotas.col.limit")}</th>
                <th>{t("v3.projects.quotas.col.project")}</th>
              </tr>
            </thead>
            <tbody>
              {view.items.map((item) => {
                const bad = invalid.includes(item.key);
                return (
                  <tr key={item.key} data-testid={`project-quota-${item.key}`}>
                    <td>
                      <div>{t(`v3.projects.quotas.key.${item.key}`)}</div>
                      <small className="v3-hint">{t(`v3.projects.quotas.enforcement.${item.enforcement}`)}</small>
                    </td>
                    <td>
                      <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                        <Lamp s={usageSignal(item)} />
                        {formatQuota(item, item.used)}
                        {item.state === "firing" && <Chip s="act">{t("v3.projects.quotas.firing")}</Chip>}
                      </span>
                    </td>
                    <td>{item.limit === null ? t("v3.projects.quotas.unlimited") : formatQuota(item, item.limit)}</td>
                    <td>
                      <Reasoned disabledReason={project.status !== "active" ? t("story.reason.projectArchived")
                        : !abilities.admin ? t("v3.projects.quotas.readOnly") : permReason(t, "admin")}>
                        <input
                          className="v3-input"
                          inputMode={item.unit === "usd" ? "decimal" : "numeric"}
                          style={{ maxWidth: 140 }}
                          value={draft[item.key]}
                          placeholder={t("v3.projects.quotas.unset")}
                          disabled={!editable}
                          aria-invalid={bad}
                          aria-label={t(`v3.projects.quotas.key.${item.key}`)}
                          onChange={(e) => setDraft({ ...draft, [item.key]: e.target.value })}
                        />
                      </Reasoned>
                      {bad && <div><small className="v3-hint" style={{ color: "var(--v3-act)" }}>{t("v3.projects.quotas.invalid")}</small></div>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {!editable && <small className="v3-hint">{t("v3.projects.quotas.readOnly")}</small>}
        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
        {editable && (
          <div>
            <Btn kind="primary" onClick={() => void save()} busy={busy} disabled={!patch}
              disabledReason={invalid.length ? t("story.reason.fixErrors") : t("story.reason.noChanges")}><Save size={14} /> {t("v3.projects.quotas.save")}</Btn>
          </div>
        )}
      </div>
    </Panel>
  );
}
