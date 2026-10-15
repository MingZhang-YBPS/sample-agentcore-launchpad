import { Save } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { type ProjectDetail, projectsApi } from "../../../lib/api/projects";
import { useProject } from "../../../workspace/project-context";
import { useToast } from "../../hooks";
import { Btn, Notice, Panel } from "../../ui";
import { CostTagsSection } from "./CostTagsSection";
import { IntegrationSection } from "./IntegrationSection";
import { IsolationSection } from "./IsolationSection";
import { LifecycleSection } from "./LifecycleSection";
import { type SettingsDraft, settingsDraft, settingsPatch } from "./model";
import type { ProjectAbilities } from "./ProjectDetail";
import { QuotasSection } from "./QuotasSection";
import { useStructureNames } from "./useStructureNames";

/**
 * §6.3 "Settings": name / department / group and `sod_strict` are an
 * administrator's (and a department administrator's inside their departments,
 * §11.1); the description and `resource_visibility` are the owner's too
 * (`project.members`). A save sends only the changed fields the caller may change.
 */
export function SettingsTab({
  project,
  abilities,
  onChanged,
}: {
  project: ProjectDetail;
  abilities: ProjectAbilities;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const names = useStructureNames(abilities.admin);
  const { managedDepartments } = useProject();
  // a department administrator moves the project between the departments they administer
  const departments = abilities.admin ? names.departments : names.departments.filter((d) => managedDepartments.some((m) => m.id === d.id));
  const [draft, setDraft] = useState<SettingsDraft>(() => settingsDraft(project));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const editable = abilities.manage && project.status === "active";
  const admin = abilities.structure && project.status === "active";
  const patch = settingsPatch(project, draft, abilities.structure);
  const set = (next: Partial<SettingsDraft>) => setDraft((prev) => ({ ...prev, ...next }));
  const groups = names.groups.filter((g) => !draft.department_id || !g.department_id || g.department_id === draft.department_id);

  const save = async () => {
    if (!patch) return;
    setBusy(true);
    setError(null);
    try {
      await projectsApi.update(project.id, patch);
      toast("ok", t("v3.projects.settings.saved", { name: draft.name || project.name }));
      onChanged();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="project-settings">
      <Panel title={t("v3.projects.settings.general")}>
        <div className="v3-prj-form">
          <label className="v3-field">
            <span>{t("v3.projects.create.name")}</span>
            <input className="v3-input" value={draft.name} maxLength={64} disabled={!admin} onChange={(e) => set({ name: e.target.value })} />
          </label>
          <label className="v3-field">
            <span>{t("v3.projects.create.department")}</span>
            <select className="v3-input" value={draft.department_id} disabled={!admin} onChange={(e) => set({ department_id: e.target.value })}>
              {abilities.admin && <option value="">{t("v3.projects.none")}</option>}
              {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              {draft.department_id && !departments.some((d) => d.id === draft.department_id) && (
                <option value={draft.department_id}>{draft.department_id}</option>
              )}
            </select>
          </label>
          <label className="v3-field">
            <span>{t("v3.projects.create.group")}</span>
            <select className="v3-input" value={draft.group_id} disabled={!admin} onChange={(e) => set({ group_id: e.target.value })}>
              <option value="">{t("v3.projects.none")}</option>
              {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
              {draft.group_id && !groups.some((g) => g.id === draft.group_id) && <option value={draft.group_id}>{draft.group_id}</option>}
            </select>
          </label>
          {!abilities.structure && <small className="v3-hint">{t("v3.projects.settings.adminOnly")}</small>}
          <label className="v3-field">
            <span>{t("v3.projects.create.description")}</span>
            <textarea className="v3-input" value={draft.description} maxLength={2000} disabled={!editable}
              onChange={(e) => set({ description: e.target.value })} />
          </label>
        </div>
      </Panel>
      <Panel title={t("v3.projects.settings.access")}>
        <div className="v3-prj-form">
          <label className="v3-field">
            <span>{t("v3.projects.settings.visibility")}</span>
            <select className="v3-input" value={draft.resource_visibility} disabled={!editable}
              onChange={(e) => set({ resource_visibility: e.target.value === "isolated" ? "isolated" : "shared" })}>
              <option value="shared">{t("v3.projects.settings.visShared")}</option>
              <option value="isolated">{t("v3.projects.settings.visIsolated")}</option>
            </select>
            <small className="v3-hint">{t("v3.projects.settings.visibilityHint")}</small>
          </label>
          <label className="v3-prj-checks">
            <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input type="checkbox" checked={draft.sod_strict} disabled={!admin} onChange={(e) => set({ sod_strict: e.target.checked })} />
              {t("v3.projects.settings.sodStrict")}
            </span>
            <small className="v3-hint">{t(abilities.structure ? "v3.projects.settings.sodStrictHint" : "v3.projects.settings.sodStrictAdmin")}</small>
          </label>
        </div>
      </Panel>
      {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      {editable && (
        <div>
          <Btn kind="primary" onClick={() => void save()} disabled={busy || !patch}><Save size={14} /> {t("v3.projects.save")}</Btn>
        </div>
      )}
      <QuotasSection project={project} abilities={abilities} />
      <CostTagsSection project={project} abilities={abilities} />
      <IsolationSection project={project} abilities={abilities} />
      <IntegrationSection project={project} />
      {abilities.admin && <LifecycleSection project={project} onChanged={onChanged} />}
    </div>
  );
}
