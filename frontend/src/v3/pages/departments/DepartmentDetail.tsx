import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { api, type ConsoleUser } from "../../../lib/api";
import { type Department, departmentsApi } from "../../../lib/api/project-groups";
import { usersApi } from "../../../lib/api/users";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Btn, Dialog, Empty, Filters, Notice, Skeleton } from "../../ui";
import { type SettingsErrors, settingsForm, settingsPatch, unopened } from "./model";

type Tab = "admins" | "workspaces" | "settings";

/**
 * One department's delegation (projects §11.1; platform administrators only): who
 * administers it, the workspaces its projects may be bound to, and the limits its
 * administrators work within. A dialog over the Departments table.
 */
export function DepartmentDetail({ dept, onClose, onChanged }: {
  dept: Department;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("admins");
  return (
    <Dialog wide title={t("v3.departments.detail.title", { name: dept.name })} onClose={onClose}
      foot={<Btn kind="ghost" onClick={onClose}>{t("v3.departments.detail.close")}</Btn>}>
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-department-detail">
        <Filters value={tab} onChange={setTab}
          options={(["admins", "workspaces", "settings"] as const).map((v) => ({ value: v, label: t(`v3.departments.tab.${v}`) }))} />
        {tab === "admins" && <AdminsTab dept={dept} />}
        {tab === "workspaces" && <WorkspacesTab dept={dept} />}
        {tab === "settings" && <SettingsTab dept={dept} onSaved={onChanged} />}
      </div>
    </Dialog>
  );
}

function AdminsTab({ dept }: { dept: Department }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const list = useLoad(() => departmentsApi.admins(dept.id), `v3-dept-admins:${dept.id}`);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const admins = list.data?.admins ?? [];
  const existing = new Set(admins.map((a) => a.user_id));
  const search = useLoad(
    async () => (q.trim() ? (await usersApi.list({ q: q.trim(), limit: 20 })).items.filter((u) => !existing.has(u.id)) : []),
    `v3-dept-admin-candidates:${dept.id}:${q.trim()}:${admins.length}`,
  );
  const put = async (body: { add?: string[]; remove?: string[] }, okKey: string, name: string) => {
    setBusy(true);
    try {
      failed.clear();
      await departmentsApi.putAdmins(dept.id, body);
      toast("ok", t(okKey, { name }));
      list.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <small className="v3-hint">{t("v3.departments.admins.hint")}</small>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-department-admins-error" />
      {list.loading && !list.data ? <Skeleton rows={2} /> : list.error ? <Notice s="act">{list.error}</Notice> : admins.length === 0 ? (
        <Empty title={t("v3.departments.admins.empty")} />
      ) : (
        <table className="v3-table" data-testid="v3-department-admins">
          <tbody>
            {admins.map((a) => (
              <tr key={a.user_id}>
                <td><div className="v3-name"><div><b>{a.username ?? a.user_id}</b><small>{a.email}</small></div></div></td>
                <td style={{ width: 1 }}>
                  <Btn size="sm" kind="ghost" disabled={busy} title={t("v3.departments.admins.remove")}
                    onClick={() => void put({ remove: [a.user_id] }, "v3.departments.admins.removed", a.username ?? a.user_id)}>
                    <Trash2 size={13} />
                  </Btn>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <label className="v3-field">
        <span>{t("v3.departments.admins.add")}</span>
        <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("v2.users.search")} />
      </label>
      <div className="v3-usr-chips">
        {(search.data ?? []).map((u: ConsoleUser) => (
          <button key={u.id} type="button" className="v3-usr-chip" disabled={busy}
            onClick={() => void put({ add: [u.id] }, "v3.departments.admins.added", u.username).then(() => setQ(""))}>
            <Plus size={12} /> {u.username}<small>{u.email}</small>
          </button>
        ))}
        {q.trim() && !search.loading && (search.data ?? []).length === 0 && <small className="v3-hint">{t("v3.admins.add.noMatch")}</small>}
      </div>
    </div>
  );
}

function WorkspacesTab({ dept }: { dept: Department }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const list = useLoad(() => departmentsApi.workspaces(dept.id), `v3-dept-workspaces:${dept.id}`);
  const all = useLoad(() => api.listWorkspaces(), "v3-dept-all-workspaces");
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const opened = list.data?.workspaces ?? [];
  const choices = unopened(all.data?.workspaces ?? [], opened);
  const put = async (body: { add?: string[]; remove?: string[] }, okKey: string, name: string) => {
    setBusy(true);
    try {
      failed.clear();
      await departmentsApi.putWorkspaces(dept.id, body);
      toast("ok", t(okKey, { name }));
      setPick("");
      list.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <small className="v3-hint">{t("v3.departments.workspaces.hint")}</small>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-department-workspaces-error" />
      {list.loading && !list.data ? <Skeleton rows={2} /> : list.error ? <Notice s="act">{list.error}</Notice> : opened.length === 0 ? (
        <Empty title={t("v3.departments.workspaces.empty")} />
      ) : (
        <table className="v3-table" data-testid="v3-department-workspaces">
          <tbody>
            {opened.map((w) => (
              <tr key={w.workspace_id}>
                <td><div className="v3-name"><div><b>{w.name}</b><small className="mono">{w.workspace_id}{w.region ? ` · ${w.region}` : ""}</small></div></div></td>
                <td style={{ width: 1 }}>
                  <Btn size="sm" kind="ghost" disabled={busy} title={t("v3.departments.workspaces.close")}
                    onClick={() => void put({ remove: [w.workspace_id] }, "v3.departments.workspaces.closed", w.name)}>
                    <Trash2 size={13} />
                  </Btn>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "end" }}>
        <label className="v3-field" style={{ flex: 1 }}>
          <span>{t("v3.departments.workspaces.open")}</span>
          <select className="v3-select" value={pick} onChange={(e) => setPick(e.target.value)}>
            <option value="">{t("v3.departments.workspaces.choose")}</option>
            {choices.map((w) => <option key={w.id} value={w.id}>{w.name} ({w.id})</option>)}
          </select>
        </label>
        <Btn kind="primary" disabled={busy || !pick}
          onClick={() => void put({ add: [pick] }, "v3.departments.workspaces.opened", choices.find((w) => w.id === pick)?.name ?? pick)}>
          <Plus size={14} /> {t("v3.departments.workspaces.openSubmit")}
        </Btn>
      </div>
    </div>
  );
}

function SettingsTab({ dept, onSaved }: { dept: Department; onSaved: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [form, setForm] = useState(() => settingsForm(dept.settings));
  const [errors, setErrors] = useState<SettingsErrors>({});
  const [busy, setBusy] = useState(false);
  const save = async () => {
    const out = settingsPatch(form);
    if ("errors" in out) {
      setErrors(out.errors);
      return;
    }
    setErrors({});
    setBusy(true);
    try {
      failed.clear();
      await departmentsApi.update(dept.id, { settings: out.settings });
      toast("ok", t("v3.departments.settings.saved", { name: dept.name }));
      onSaved();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="v3-department-settings">
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-department-settings-error" />
      <label className="v3-field" style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input type="checkbox" checked={form.canCreateUsers} onChange={(e) => setForm({ ...form, canCreateUsers: e.target.checked })} />
        <span>{t("v3.departments.settings.canCreateUsers")}</span>
      </label>
      <small className="v3-hint">{t("v3.departments.settings.canCreateUsersHint")}</small>
      <label className="v3-field">
        <span>{t("v3.departments.settings.validityDays")}</span>
        <input className="v3-input" inputMode="numeric" value={form.validityDays} onChange={(e) => setForm({ ...form, validityDays: e.target.value })} />
        {errors.validityDays && <small className="v3-err">{t(errors.validityDays)}</small>}
      </label>
      <label className="v3-field">
        <span>{t("v3.departments.settings.maxProjects")}</span>
        <input className="v3-input" inputMode="numeric" value={form.maxProjects} placeholder={t("v3.departments.settings.noCap")}
          onChange={(e) => setForm({ ...form, maxProjects: e.target.value })} />
        {errors.maxProjects && <small className="v3-err">{t(errors.maxProjects)}</small>}
      </label>
      <div><Btn kind="primary" disabled={busy} onClick={() => void save()}>{t("v3.departments.settings.save")}</Btn></div>
    </div>
  );
}
