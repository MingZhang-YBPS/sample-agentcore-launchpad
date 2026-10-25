import "../users.css";

import { Archive, ArchiveRestore, Pencil, Plus, RefreshCw, ShieldAlert, Trash2, UserPlus, Users } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { type Department, departmentsApi, type StructureStatus } from "../../../lib/api/departments";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Filters, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { DepartmentDetail } from "./DepartmentDetail";
import { InviteDialog } from "./InviteDialog";
import { deletable, emptyStructureForm } from "./model";
import { StructureDialog } from "./StructureDialog";

/**
 * Departments (projects §7.9, §7.10, §11.1): they label projects and group
 * the project switcher. A platform administrator creates, edits, archives / unarchives
 * and deletes them, and per department appoints its administrators, opens workspaces
 * to it and sets its limits (`DepartmentDetail`). A department administrator sees the
 * departments they administer and, where allowed, invites accounts into them.
 * Hub-global.
 */
export function V3Departments() {
  const { isAdmin } = useAuth();
  return isAdmin ? <AdminDepartments /> : <MyDepartments />;
}

/** What a department administrator sees: their departments (§11.1). */
function MyDepartments() {
  const { t } = useTranslation();
  const mine = useLoad(() => departmentsApi.mine(), "v3-departments-mine");
  const all = useLoad(() => departmentsApi.list("active"), "v3-departments:active:member");
  const [inviting, setInviting] = useState<Department | null>(null);
  const managed = mine.data ?? [];
  const byId = new Map((all.data?.departments ?? []).map((d) => [d.id, d]));

  if (mine.loading && !mine.data) return <div style={{ padding: 20 }}><Skeleton rows={3} /></div>;
  if (managed.length === 0) {
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
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v3.departments.eyebrow")} title={t("v3.departments.mine.title")} sub={t("v3.departments.mine.sub")} />
      <Panel flush>
        <table className="v3-table" data-testid="v3-departments-mine">
          <thead>
            <tr>
              <th>{t("v3.departments.col.department")}</th>
              <th>{t("v3.departments.col.workspaces")}</th>
              <th className="num">{t("v3.departments.col.projects")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {managed.map((m) => {
              const d = byId.get(m.id);
              return (
                <tr key={m.id}>
                  <td><div className="v3-name"><div><b>{m.name}</b><small className="mono">{m.id}</small></div></div></td>
                  <td className="mono">{m.workspaces.length ? m.workspaces.join(", ") : <span className="v3-usr-muted">{t("v3.departments.mine.noWorkspace")}</span>}</td>
                  <td className="num">{d?.project_count ?? "—"}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <Btn size="sm" kind="ghost" disabled={!d?.settings.can_create_users}
                      disabledReason={d ? t("v3.departments.invite.disabled") : t("story.reason.notLoaded")}
                      onClick={() => d && setInviting(d)}>
                      <UserPlus size={13} /> {t("v3.departments.invite.open")}
                    </Btn>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>
      {inviting && <InviteDialog dept={inviting} onClose={() => setInviting(null)} />}
    </div>
  );
}

/** The platform administrator's table. */
function AdminDepartments() {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [params, setParams] = useSearchParams();
  const status = (["active", "archived", "all"].includes(params.get("status") ?? "") ? params.get("status") : "active") as StructureStatus;
  const list = useLoad(() => departmentsApi.list(status), `v3-departments:${status}`);
  const [editing, setEditing] = useState<Department | "new" | null>(null);
  const [confirm, setConfirm] = useState<{ kind: "archive" | "delete"; dept: Department } | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [managing, setManaging] = useState<Department | null>(null);

  const run = async (dept: Department, call: () => Promise<unknown>, okKey: string) => {
    setBusyId(dept.id);
    try {
      failed.clear();
      await call();
      toast("ok", t(okKey, { name: dept.name }));
      list.reload();
      return true;
    } catch (err) {
      failed.fail(err);
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
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-departments-error" />
      <Filters
        value={status}
        onChange={(v) => setParams(v === "active" ? {} : { status: v })}
        options={(["active", "archived", "all"] as const).map((v) => ({ value: v, label: t(`v3.structure.status.${v}`) }))}
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
                <th>{t("v3.structure.form.description")}</th>
                <th className="num">{t("v3.departments.col.projects")}</th>
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
                      {d.archived && <Chip>{t("v3.structure.status.archived")}</Chip>}
                    </td>
                    <td className="v3-usr-muted">{d.description || "—"}</td>
                    <td className="num">{d.project_count}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setManaging(d)}><Users size={13} /> {t("v3.departments.manage")}</Btn>
                        <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setEditing(d)}><Pencil size={13} /> {t("v3.structure.edit")}</Btn>
                        {d.archived ? (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => void run(d, () => departmentsApi.unarchive(d.id), "v3.departments.unarchived")}>
                            <ArchiveRestore size={13} /> {t("v3.structure.unarchive")}
                          </Btn>
                        ) : (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setConfirm({ kind: "archive", dept: d })}>
                            <Archive size={13} /> {t("v3.structure.archive")}
                          </Btn>
                        )}
                        <Btn size="sm" kind="ghost" busy={busy} disabled={!deletable(d)} disabledReason={t("v3.departments.inUse")}
                          title={t("v3.departments.delete")} aria-label={t("v3.departments.delete")}
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

      {managing && <DepartmentDetail dept={managing} onClose={() => setManaging(null)} onChanged={list.reload} />}
      {editing && (
        <StructureDialog
          creating={editing === "new"}
          initial={editing === "new" ? emptyStructureForm() : { id: editing.id, name: editing.name, description: editing.description }}
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
          confirmLabel={t(confirm.kind === "archive" ? "v3.structure.archive" : "v3.departments.delete")}
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
