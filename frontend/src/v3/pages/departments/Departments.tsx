import "../users.css";

import { Archive, ArchiveRestore, Pencil, Plus, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { errorMessage } from "../../../lib/api";
import { type Department, departmentsApi, type StructureStatus } from "../../../lib/api/project-groups";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Filters, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { emptyStructureForm } from "../groups/model";
import { StructureDialog } from "../groups/StructureDialog";
import { deletable } from "./model";

/**
 * Departments (projects §7.7, §7.9; platform administrators only): phase 1 has a
 * name and a description only — they label projects and groups and group the
 * project switcher. Create, edit, archive / unarchive, and delete an unused one.
 * Hub-global.
 */
export function V3Departments() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const status = (["active", "archived", "all"].includes(params.get("status") ?? "") ? params.get("status") : "active") as StructureStatus;
  const list = useLoad(async () => (isAdmin ? departmentsApi.list(status) : { departments: [] }), `v3-departments:${status}:${isAdmin}`);
  const [editing, setEditing] = useState<Department | "new" | null>(null);
  const [confirm, setConfirm] = useState<{ kind: "archive" | "delete"; dept: Department } | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.departments.eyebrow")} title={t("v3.departments.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <Empty title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ShieldAlert size={16} /> {t("auth.adminRequired.title")}</span>}>
            {t("usersPage.forbiddenBody")}
          </Empty>
        </Panel>
      </div>
    );
  }

  const run = async (dept: Department, call: () => Promise<unknown>, okKey: string) => {
    setBusyId(dept.id);
    try {
      await call();
      toast("ok", t(okKey, { name: dept.name }));
      list.reload();
      return true;
    } catch (err) {
      toast("act", errorMessage(err), err);
      return false;
    } finally {
      setBusyId(null);
    }
  };
  const departments = list.data?.departments ?? [];

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.departments.eyebrow")}
        title={t("v3.departments.title")}
        sub={t("v3.departments.sub")}
        end={
          <>
            <Btn kind="ghost" onClick={list.reload} disabled={list.loading}><RefreshCw size={14} /> {t("v3.users.refresh")}</Btn>
            <Btn kind="primary" onClick={() => setEditing("new")}><Plus size={14} /> {t("v3.departments.create")}</Btn>
          </>
        }
      />
      <Filters
        value={status}
        onChange={(v) => setParams(v === "active" ? {} : { status: v })}
        options={(["active", "archived", "all"] as const).map((v) => ({ value: v, label: t(`v3.groups.status.${v}`) }))}
      />
      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : departments.length === 0 ? (
          <Empty title={t("v3.departments.empty")}>{t("v3.departments.emptyBody")}</Empty>
        ) : (
          <table className="v3-table" data-testid="v3-departments-table">
            <thead>
              <tr>
                <th>{t("v3.departments.col.department")}</th>
                <th>{t("v3.groups.form.description")}</th>
                <th className="num">{t("v3.departments.col.projects")}</th>
                <th className="num">{t("v3.departments.col.groups")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {departments.map((d) => {
                const busy = busyId === d.id;
                return (
                  <tr key={d.id}>
                    <td>
                      <div className="v3-name"><div><b>{d.name}</b><small className="mono">{d.id}</small></div></div>
                      {d.archived && <Chip>{t("v3.groups.status.archived")}</Chip>}
                    </td>
                    <td className="v3-usr-muted">{d.description || "—"}</td>
                    <td className="num">{d.project_count}</td>
                    <td className="num">{d.group_count}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setEditing(d)}><Pencil size={13} /> {t("v3.groups.edit")}</Btn>
                        {d.archived ? (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => void run(d, () => departmentsApi.unarchive(d.id), "v3.departments.unarchived")}>
                            <ArchiveRestore size={13} /> {t("v3.groups.unarchive")}
                          </Btn>
                        ) : (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setConfirm({ kind: "archive", dept: d })}>
                            <Archive size={13} /> {t("v3.groups.archive")}
                          </Btn>
                        )}
                        <Btn size="sm" kind="ghost" disabled={busy || !deletable(d)}
                          title={deletable(d) ? t("v3.departments.delete") : t("v3.departments.inUse")}
                          onClick={() => setConfirm({ kind: "delete", dept: d })}>
                          <Trash2 size={13} />
                        </Btn>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      {editing && (
        <StructureDialog
          kind="department"
          creating={editing === "new"}
          initial={editing === "new" ? emptyStructureForm() : { id: editing.id, name: editing.name, description: editing.description, department_id: "" }}
          onClose={() => setEditing(null)}
          onSubmit={async (form) => {
            if (editing === "new") await departmentsApi.create({ id: form.id, name: form.name, description: form.description });
            else await departmentsApi.update(editing.id, { name: form.name, description: form.description });
            setEditing(null);
            toast("ok", t(editing === "new" ? "v3.departments.created" : "v3.departments.saved", { name: form.name }));
            list.reload();
          }} />
      )}
      {confirm && (
        <Confirm
          title={t(confirm.kind === "archive" ? "v3.departments.archiveTitle" : "v3.departments.deleteTitle", { name: confirm.dept.name })}
          confirmLabel={t(confirm.kind === "archive" ? "v3.groups.archive" : "v3.departments.delete")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={busyId !== null}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const { kind, dept } = confirm;
            const call = kind === "archive" ? () => departmentsApi.archive(dept.id) : () => departmentsApi.remove(dept.id);
            void run(dept, call, kind === "archive" ? "v3.departments.archived" : "v3.departments.deleted").then((ok) => ok && setConfirm(null));
          }}
        >
          {t(confirm.kind === "archive" ? "v3.departments.archiveBody" : "v3.departments.deleteBody", { name: confirm.dept.name })}
        </Confirm>
      )}
    </div>
  );
}
