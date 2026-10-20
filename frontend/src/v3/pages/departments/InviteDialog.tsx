import { useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import { type Department, departmentsApi, type Invitation } from "../../../lib/api/departments";
import { projectsApi } from "../../../lib/api/projects";
import { useLoad } from "../../hooks";
import { Btn, Dialog, Notice } from "../../ui";
import { useRoleLabel } from "../users/structure";
import { parseCount } from "./model";
import { Select } from "../../select";

/**
 * Invite a new member account straight into one of the department's projects
 * (projects §11.1): valid at most the department's `user_validity_max_days`; the
 * generated password is shown once here.
 */
export function InviteDialog({ dept, onClose }: { dept: Department; onClose: () => void }) {
  const { t } = useTranslation();
  const label = useRoleLabel();
  const projects = useLoad(() => projectsApi.list({ department_id: dept.id, status: "active" }), `v3-dept-invite-projects:${dept.id}`);
  const templates = useLoad(() => projectsApi.roleTemplates(), "v3-dept-invite-templates");
  const max = dept.settings.user_validity_max_days;
  const [form, setForm] = useState({ username: "", email: "", project_id: "", role_key: "developer", days: String(max) });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<Invitation | null>(null);
  const days = parseCount(form.days, max);
  const projectId = form.project_id || projects.data?.projects[0]?.id || "";
  const ready = Boolean(form.username.trim() && form.email.trim() && projectId && typeof days === "number");

  const submit = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      setDone(await departmentsApi.invite(dept.id, {
        username: form.username.trim(), email: form.email.trim(), project_id: projectId,
        role_key: form.role_key, valid_days: days as number,
      }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t("v3.departments.invite.title", { name: dept.name })}
      onClose={() => !busy && onClose()}
      foot={done ? (
        <Btn kind="primary" onClick={onClose}>{t("v3.departments.detail.close")}</Btn>
      ) : (
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" busy={busy} disabled={!ready} disabledReason={t("story.reason.fillRequired")} onClick={() => void submit()}>{t("v3.departments.invite.submit")}</Btn>
        </>
      )}
    >
      {done ? (
        <div style={{ display: "grid", gap: 8 }} data-testid="v3-department-invited">
          <Notice s="ok">{t("v3.departments.invite.done", { name: done.username, project: done.project_id })}</Notice>
          {done.generated_password && (
            <label className="v3-field">
              <span>{t("v3.departments.invite.password")}</span>
              <input className="v3-input mono" readOnly value={done.generated_password} onFocus={(e) => e.target.select()} />
              <small className="v3-hint">{t("v3.departments.invite.passwordOnce")}</small>
            </label>
          )}
        </div>
      ) : (
        <div style={{ display: "grid", gap: 12 }} data-testid="v3-department-invite">
          <label className="v3-field">
            <span>{t("v3.departments.invite.username")}</span>
            <input className="v3-input" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
          </label>
          <label className="v3-field">
            <span>{t("v3.departments.invite.email")}</span>
            <input className="v3-input" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          </label>
          <label className="v3-field">
            <span>{t("v3.departments.invite.project")}</span>
            <Select className="v3-select" value={projectId} onChange={(e) => setForm({ ...form, project_id: e.target.value })}>
              {(projects.data?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name} ({p.id})</option>)}
            </Select>
            {projects.data && projects.data.projects.length === 0 && <small className="v3-hint">{t("v3.departments.invite.noProject")}</small>}
          </label>
          <label className="v3-field">
            <span>{t("v3.users.access.role")}</span>
            <Select className="v3-select" value={form.role_key} onChange={(e) => setForm({ ...form, role_key: e.target.value })}>
              {(templates.data?.templates ?? []).map((tpl) => <option key={tpl.key} value={tpl.key}>{label(tpl.key)}</option>)}
            </Select>
          </label>
          <label className="v3-field">
            <span>{t("v3.departments.invite.days", { max })}</span>
            <input className="v3-input" inputMode="numeric" value={form.days} onChange={(e) => setForm({ ...form, days: e.target.value })} />
            {days === undefined && <small className="v3-err">{t("v3.departments.invite.daysErr", { max })}</small>}
          </label>
          {error && <Notice s="act">{error}</Notice>}
        </div>
      )}
    </Dialog>
  );
}
