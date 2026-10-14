import { Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ApiError, errorMessage } from "../../../lib/api";
import { type CustomRole, type ProjectDetail, type ProjectRoles, projectsApi } from "../../../lib/api/projects";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Dialog, Empty, Notice, Panel, Skeleton } from "../../ui";
import { KeyPicker } from "./KeyPicker";
import { useRoleLabel } from "./labels";
import { keySlug, roleKeyError, sodRefusal } from "./model";
import type { ProjectAbilities } from "./ProjectDetail";

/** §6.3 "Roles": the built-in templates (read-only) and this project's custom roles (§3.3). */
export function RolesTab({
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
  const [tick, setTick] = useState(0);
  const roles = useLoad(() => projectsApi.roles(project.id), `v3-project-roles:${project.id}:${tick}`);
  const roleLabel = useRoleLabel(roles.data?.custom);
  const [editing, setEditing] = useState<CustomRole | "new" | null>(null);
  const [deleting, setDeleting] = useState<CustomRole | null>(null);
  const [busy, setBusy] = useState(false);
  const canWrite = abilities.manage && project.status === "active";
  const keyLabel = (key: string) => t(`v3.projects.key.${keySlug(key)}`, key);
  const reload = () => {
    setTick((n) => n + 1);
    onChanged();
  };

  const remove = async (role: CustomRole) => {
    setBusy(true);
    try {
      await projectsApi.deleteRole(project.id, role.key);
      toast("ok", t("v3.projects.roles.deleted", { name: role.name }));
      reload();
    } catch (err) {
      if (err instanceof ApiError && err.code === "project.role_in_use") {
        const count = (err.detail as { members?: number } | null)?.members ?? role.members;
        toast("act", t("v3.projects.roles.inUse", { name: role.name, count }), err);
      } else toast("act", errorMessage(err), err);
    } finally {
      setBusy(false);
      setDeleting(null);
    }
  };

  if (roles.loading && !roles.data) return <Panel><Skeleton rows={4} /></Panel>;
  if (!roles.data) return <Notice s="act"><ApiErrorDetail error={roles.failure ?? roles.error} /></Notice>;
  const data = roles.data;

  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="project-roles">
      <Panel
        title={t("v3.projects.roles.custom")}
        flush
        end={canWrite && <Btn size="sm" kind="primary" onClick={() => setEditing("new")}><Plus size={12} /> {t("v3.projects.roles.new")}</Btn>}
      >
        {data.custom.length === 0 ? (
          <Empty title={t("v3.projects.roles.empty")}>{canWrite ? t("v3.projects.roles.emptyHint") : null}</Empty>
        ) : (
          <table className="v3-table" data-testid="custom-roles">
            <thead>
              <tr>
                <th>{t("v3.projects.roles.name")}</th>
                <th>{t("v3.projects.roles.basedOn")}</th>
                <th>{t("v3.projects.roles.keys")}</th>
                <th className="num">{t("v3.projects.roles.members")}</th>
                <th className="num">{t("v3.projects.roles.updated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.custom.map((r) => (
                <tr key={r.key}>
                  <td><div className="v3-name"><div><b>{r.name}</b><small>{r.key}</small></div></div></td>
                  <td>{r.based_on ? roleLabel(r.based_on) : "—"}</td>
                  <td><span className="v3-prj-chips">{r.permissions.map((k) => <Chip key={k} title={k}>{keyLabel(k)}</Chip>)}</span></td>
                  <td className="num">{r.members}</td>
                  <td className="num v3-prj-muted">{ago(r.updated_at)}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    {canWrite && (
                      <span style={{ display: "inline-flex", gap: 6 }}>
                        <Btn size="sm" onClick={() => setEditing(r)}><Pencil size={12} /> {t("v3.projects.members.edit")}</Btn>
                        <Btn size="sm" kind="danger" onClick={() => setDeleting(r)} disabled={busy}
                          title={r.members > 0 ? t("v3.projects.roles.inUse", { name: r.name, count: r.members }) : undefined}>
                          <Trash2 size={12} /> {t("v3.projects.remove")}
                        </Btn>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title={t("v3.projects.roles.templates")} flush>
        <table className="v3-table" data-testid="role-templates">
          <tbody>
            {data.templates.map((r) => (
              <tr key={r.key}>
                <td style={{ width: 200 }}><div className="v3-name"><div><b>{roleLabel(r.key)}</b><small>{r.key}</small></div></div></td>
                <td>
                  {r.permissions.length === 0 ? (
                    <span className="v3-prj-muted">{t("v3.projects.roles.noKeys")}</span>
                  ) : (
                    <span className="v3-prj-chips">{r.permissions.map((k) => <Chip key={k} title={k}>{keyLabel(k)}</Chip>)}</span>
                  )}
                </td>
              </tr>
            ))}
            {data.inherit_available && (
              <tr>
                <td><div className="v3-name"><div><b>{roleLabel(data.inherit)}</b><small>{data.inherit}</small></div></div></td>
                <td className="v3-prj-muted">{t("v3.projects.roles.inheritHint")}</td>
              </tr>
            )}
          </tbody>
        </table>
      </Panel>

      {editing && (
        <RoleDialog
          project={project}
          roles={data}
          role={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
      {deleting && (
        <Confirm
          title={t("v3.projects.roles.deleteTitle", { name: deleting.name })}
          confirmLabel={t("v3.projects.remove")}
          cancelLabel={t("v3.projects.cancel")}
          danger
          busy={busy}
          onConfirm={() => void remove(deleting)}
          onCancel={() => setDeleting(null)}
        >
          {t("v3.projects.roles.deleteBody", { name: deleting.name })}
        </Confirm>
      )}
    </div>
  );
}

/** Create a custom role from a template, or edit one: key (new only), name, keys. */
function RoleDialog({
  project,
  roles,
  role,
  onClose,
  onSaved,
}: {
  project: ProjectDetail;
  roles: ProjectRoles;
  role: CustomRole | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const roleLabel = useRoleLabel(roles.custom);
  const firstTemplate = roles.templates.find((r) => r.key === "developer") ?? roles.templates[0];
  const [key, setKey] = useState(role?.key ?? "");
  const [name, setName] = useState(role?.name ?? "");
  const [basedOn, setBasedOn] = useState(role ? role.based_on ?? "" : firstTemplate?.key ?? "");
  const [keys, setKeys] = useState<string[]>(role ? role.permissions : [...(firstTemplate?.permissions ?? [])]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const builtin = roles.templates.map((r) => r.key);
  const keyErr = role ? null : key ? roleKeyError(key, builtin) : null;
  const clash = !role && roles.custom.some((r) => r.key === key);
  const valid = (role !== null || (key !== "" && keyErr === null && !clash)) && name.trim() !== "";

  const pickTemplate = (next: string) => {
    setBasedOn(next);
    const template = roles.templates.find((r) => r.key === next);
    if (template) setKeys([...template.permissions]);
  };
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await projectsApi.putRole(project.id, key, { name: name.trim(), permissions: keys, based_on: basedOn || null });
      toast("ok", t("v3.projects.roles.saved", { name: name.trim() }));
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const refusal = sodRefusal(error);

  return (
    <Dialog
      wide
      title={role ? t("v3.projects.roles.editTitle", { name: role.name }) : t("v3.projects.roles.new")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.projects.cancel")}</Btn>
          <Btn kind="primary" onClick={() => void save()} disabled={busy || !valid}>{t("v3.projects.save")}</Btn>
        </>
      }
    >
      <div className="v3-prj-form" data-testid="role-dialog">
        <label className="v3-field">
          <span>{t("v3.projects.roles.name")}*</span>
          <input className="v3-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} autoFocus />
        </label>
        <label className="v3-field">
          <span>{t("v3.projects.roles.key")}*</span>
          <input className="v3-input mono" value={key} maxLength={32} disabled={role !== null}
            aria-invalid={keyErr !== null || clash} onChange={(e) => setKey(e.target.value.toLowerCase())} />
          <small className={keyErr || clash ? "v3-err" : "v3-hint"}>
            {clash ? t("v3.projects.roles.keyTaken") : keyErr === "builtin" ? t("v3.projects.roles.keyBuiltin") : t("v3.projects.roles.keyHint")}
          </small>
        </label>
        <label className="v3-field">
          <span>{t("v3.projects.roles.basedOn")}</span>
          <select className="v3-input" value={basedOn} onChange={(e) => pickTemplate(e.target.value)}>
            <option value="">—</option>
            {roles.templates.map((r) => <option key={r.key} value={r.key}>{roleLabel(r.key)}</option>)}
          </select>
          <small className="v3-hint">{t("v3.projects.roles.basedOnHint")}</small>
        </label>
        <KeyPicker projectKeys={roles.project_keys} agentKeys={roles.agent_keys} value={keys} onChange={setKeys}
          strict={Boolean(project.settings?.sod_strict)} />
        {error != null && (
          <Notice s="act">
            {refusal && <div>{t("v3.projects.sod.refused", { names: refusal.map((c) => c.username ?? c.user_id).join(", ") })}</div>}
            <ApiErrorDetail error={error} />
          </Notice>
        )}
      </div>
    </Dialog>
  );
}
