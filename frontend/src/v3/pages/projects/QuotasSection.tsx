import { Save } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { type ProjectDetail, projectQuotasApi } from "../../../lib/api/projects";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Lamp, Notice, Panel, Skeleton } from "../../ui";
import type { ProjectAbilities } from "./ProjectDetail";
import { formatQuota, type QuotaDraft, quotaDraft, quotaErrors, quotaPatch, usageSignal } from "./quotasModel";

/**
 * §11.3 "Quotas": every member sees usage against the limits (the smaller of the
 * project's value and its department's cap); an administrator, or the project's
 * department administrator, edits the project's values — never above the department
 * cap. The `/v1` window stays the platform's unless the department has a cap for it.
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
  const editable = abilities.structure && project.status === "active";

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
        <small className="v3-hint">{t("v3.projects.quotas.sub")}</small>
        <div className="v3-table-scroll">
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v3.projects.quotas.col.quota")}</th>
                <th>{t("v3.projects.quotas.col.used")}</th>
                <th>{t("v3.projects.quotas.col.limit")}</th>
                <th>{t("v3.projects.quotas.col.department")}</th>
                <th>{t("v3.projects.quotas.col.project")}</th>
              </tr>
            </thead>
            <tbody>
              {view.items.map((item) => {
                // a department administrator sets the /v1 window only under a department cap
                const locked = !abilities.admin && item.key === "v1_rate_per_minute" && item.department_cap === null;
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
                      {item.key === "monthly_cost_alert_usd" && item.limit !== null && item.used === null && (
                        <div><small className="v3-hint">{t("v3.projects.quotas.notMeasured")}</small></div>
                      )}
                    </td>
                    <td>{item.limit === null ? t("v3.projects.quotas.unlimited") : formatQuota(item, item.limit)}</td>
                    <td>{formatQuota(item, item.department_cap)}</td>
                    <td>
                      <input
                        className="v3-input"
                        inputMode={item.unit === "usd" ? "decimal" : "numeric"}
                        style={{ maxWidth: 140 }}
                        value={draft[item.key]}
                        placeholder={t("v3.projects.quotas.unset")}
                        disabled={!editable || locked}
                        aria-invalid={bad}
                        aria-label={t(`v3.projects.quotas.key.${item.key}`)}
                        onChange={(e) => setDraft({ ...draft, [item.key]: e.target.value })}
                      />
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
            <Btn kind="primary" onClick={() => void save()} disabled={busy || !patch}><Save size={14} /> {t("v3.projects.quotas.save")}</Btn>
          </div>
        )}
      </div>
    </Panel>
  );
}
