import "../cost-tags/costTags.css";

import { Plus, Save, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import type { ProjectDetail } from "../../../lib/api/projects";
import {
  type BudgetBasis,
  costTagsApi,
  MAX_CUSTOM_TAGS,
  rowErrors,
  rowsOf,
  sameTags,
  type TagRow,
  tagsOf,
} from "../../../lib/api/costTags";
import { useLoad, useToast } from "../../hooks";
import { Btn, Filters, Notice, Panel, Skeleton } from "../../ui";
import type { ProjectAbilities } from "./ProjectDetail";

/** The platform's own keys, written on every resource of the project: shown, never edited. */
const FIXED = ["launchpad:project", "launchpad:workspace", "launchpad:agent-id", "launchpad:managed-by", "launchpad:department"];

/**
 * Cost tags (cost-tags design §1.1 D-3, §1.4-4): the custom cost tags the platform
 * writes on the project's resources next to its own `launchpad:*` set — the owner's
 * (`project.members`) — and what the project's budget compares against, the estimate
 * or the billed cost — an administrator's or the department administrator's, like
 * the budget itself.
 */
export function CostTagsSection({ project, abilities }: { project: ProjectDetail; abilities: ProjectAbilities }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [tick, setTick] = useState(0);
  const loaded = useLoad(() => costTagsApi.project(project.id), `v3-project-cost-tags:${project.id}:${tick}`);
  const view = loaded.data;
  const [rows, setRows] = useState<TagRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (view) setRows(rowsOf(view.cost_tags));
  }, [view]);
  const active = project.status === "active";
  const editable = abilities.manage && active;
  const basisEditable = abilities.structure && active;

  if (loaded.loading && !view) return <Panel title={t("v3.costTags.project.title")}><Skeleton rows={3} /></Panel>;
  if (!view) {
    return (
      <Panel title={t("v3.costTags.project.title")}>
        <Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice>
      </Panel>
    );
  }

  const errors = rowErrors(rows);
  const next = tagsOf(rows);
  const dirty = next !== null && !sameTags(next, view.cost_tags);
  const set = (i: number, patch: Partial<TagRow>) => setRows((prev) => prev.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const run = async (call: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    setError(null);
    try {
      await call();
      toast("ok", t(okKey, { name: project.name }));
      setTick((n) => n + 1);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={t("v3.costTags.project.title")}>
      <div style={{ display: "grid", gap: 14 }} data-testid="project-cost-tags">
        <small className="v3-hint">{t("v3.costTags.project.sub")}</small>
        <div className="v3-ctag-fixed" aria-label={t("v3.costTags.project.fixed")}>
          {FIXED.map((key) => <span key={key} className="v3-ctag" data-primary={key === "launchpad:project"}>{key}</span>)}
          {Object.keys(view.cost_tags).map((key) => <span key={key} className="v3-ctag" data-s="ok">{key}</span>)}
        </div>

        <div className="v3-ctag-rows">
          {rows.map((row, i) => (
            <div key={i} className="v3-ctag-row" data-testid={`cost-tag-row-${i}`}>
              <input className="v3-input mono" value={row.key} maxLength={128} disabled={!editable}
                placeholder={t("v3.costTags.project.keyPlaceholder")} aria-label={t("v3.costTags.col.key")}
                aria-invalid={errors[i] !== null} onChange={(e) => set(i, { key: e.target.value })} />
              <input className="v3-input mono" value={row.value} maxLength={256} disabled={!editable}
                placeholder={t("v3.costTags.project.valuePlaceholder")} aria-label={t("v3.costTags.col.value")}
                onChange={(e) => set(i, { value: e.target.value })} />
              {editable ? (
                <Btn size="sm" kind="ghost" title={t("v3.costTags.project.remove")} onClick={() => setRows((prev) => prev.filter((_, j) => j !== i))}><X size={13} /></Btn>
              ) : <span />}
              {errors[i] && <div className="err">{t(`v3.costTags.project.error.${errors[i]}`)}</div>}
            </div>
          ))}
          {rows.length === 0 && <small className="v3-hint">{t("v3.costTags.project.none")}</small>}
        </div>
        {editable && (
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Btn size="sm" disabled={rows.length >= MAX_CUSTOM_TAGS} onClick={() => setRows((prev) => [...prev, { key: "", value: "" }])}>
              <Plus size={13} /> {t("v3.costTags.project.add")}
            </Btn>
            <span className="v3-hint">{t("v3.costTags.project.count", { count: rows.length, max: MAX_CUSTOM_TAGS })}</span>
            <span style={{ marginLeft: "auto" }}>
              <Btn kind="primary" disabled={busy || !dirty} onClick={() => next && void run(() => costTagsApi.putProject(project.id, next), "v3.costTags.project.saved")}>
                <Save size={14} /> {t("v3.projects.save")}
              </Btn>
            </span>
          </div>
        )}

        <div className="v3-field">
          <span>{t("v3.costTags.project.basis")}</span>
          {basisEditable ? (
            <Filters<BudgetBasis> value={view.budget_basis}
              onChange={(basis) => basis !== view.budget_basis && void run(() => costTagsApi.putBudgetBasis(project.id, basis), "v3.costTags.project.basisSaved")}
              options={view.budget_bases.map((value) => ({ value, label: t(`v3.costTags.project.basisName.${value}`) }))} />
          ) : (
            <b>{t(`v3.costTags.project.basisName.${view.budget_basis}`)}</b>
          )}
          <small className="v3-hint">{t(`v3.costTags.project.basisHint.${view.budget_basis}`)}</small>
        </div>
        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      </div>
    </Panel>
  );
}
