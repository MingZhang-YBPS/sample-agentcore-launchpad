import "../users.css";

import { Archive, ArchiveRestore, ArrowLeft, Pencil, Plus, RefreshCw, ShieldAlert, Trash2, UserPlus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { type ConsoleUser, errorMessage } from "../../../lib/api";
import {
  departmentsApi,
  type GroupMember,
  type GroupProject,
  type ProjectGroupDetail,
  projectGroupsApi,
  type StructureStatus,
} from "../../../lib/api/project-groups";
import { projectsApi } from "../../../lib/api/projects";
import { usersApi } from "../../../lib/api/users";
import { useProject } from "../../../workspace/project-context";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Btn, Chip, Confirm, Dialog, Empty, Filters, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { useRoleLabel } from "../users/structure";
import { emptyStructureForm, groupsOfDepartments, projectChoices, projectSummary } from "./model";
import { StructureDialog } from "./StructureDialog";

function Forbidden() {
  const { t } = useTranslation();
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v3.groups.eyebrow")} title={t("v3.groups.title")} sub={t("auth.adminRequired.meta")} />
      <Panel>
        <Empty title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ShieldAlert size={16} /> {t("auth.adminRequired.title")}</span>}>
          {t("usersPage.forbiddenBody")}
        </Empty>
      </Panel>
    </div>
  );
}

/* ── list ────────────────────────────────────────────────────────────────── */

/** What the caller manages here: everything (an administrator), or their departments' groups (§11.1). */
function useGroupScope() {
  const { isAdmin } = useAuth();
  const { managedDepartments } = useProject();
  const managed = isAdmin ? [] : managedDepartments.map((d) => d.id);
  return { isAdmin, managed };
}

function GroupList() {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin, managed } = useGroupScope();
  const [params, setParams] = useSearchParams();
  const status = (["active", "archived", "all"].includes(params.get("status") ?? "") ? params.get("status") : "active") as StructureStatus;
  // a department administrator lists per department (`?department_id=` is required)
  const list = useLoad(
    async () => (isAdmin
      ? projectGroupsApi.list({ status })
      : { groups: groupsOfDepartments(await Promise.all(managed.map((d) => projectGroupsApi.list({ status, department_id: d })))) }),
    `v3-groups:${status}:${managed.join(",")}`,
  );
  const departments = useLoad(() => departmentsApi.list("all"), "v3-groups-departments");
  const [creating, setCreating] = useState(false);
  const deptName = (id: string | null) => (id ? (departments.data?.departments.find((d) => d.id === id)?.name ?? id) : "—");
  const groups = list.data?.groups ?? [];
  // the create / edit dialogs offer only the departments the caller may place a group in
  const deptChoices = (departments.data?.departments ?? []).filter((d) => isAdmin || managed.includes(d.id));

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.groups.eyebrow")}
        title={t("v3.groups.title")}
        sub={t("v3.groups.sub")}
        end={
          <>
            <Btn kind="ghost" onClick={list.reload} disabled={list.loading}><RefreshCw size={14} /> {t("v3.users.refresh")}</Btn>
            <Btn kind="primary" onClick={() => setCreating(true)}><Plus size={14} /> {t("v3.groups.create")}</Btn>
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
        ) : groups.length === 0 ? (
          <Empty title={t("v3.groups.empty")}>{t("v3.groups.emptyBody")}</Empty>
        ) : (
          <table className="v3-table" data-testid="v3-groups-table">
            <thead>
              <tr>
                <th>{t("v3.groups.col.group")}</th>
                <th>{t("v3.groups.col.department")}</th>
                <th>{t("v3.groups.col.projects")}</th>
                <th className="num">{t("v3.groups.col.members")}</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => {
                const summary = projectSummary(g.projects);
                return (
                  <tr key={g.id} className="click" onClick={() => setParams({ id: g.id })}>
                    <td>
                      <div className="v3-name"><div><b>{g.name}</b><small className="mono">{g.id}</small></div></div>
                      {g.archived && <Chip>{t("v3.groups.status.archived")}</Chip>}
                    </td>
                    <td>{deptName(g.department_id)}</td>
                    <td>
                      {summary.names || <span className="v3-usr-muted">—</span>}
                      {summary.more > 0 && <span className="v3-usr-muted"> +{summary.more}</span>}
                    </td>
                    <td className="num">{g.member_count}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {creating && (
        <StructureDialog kind="group" creating departments={deptChoices} requireDepartment={!isAdmin}
          initial={{ ...emptyStructureForm(), department_id: isAdmin ? "" : (managed[0] ?? "") }}
          onClose={() => setCreating(false)}
          onSubmit={async (form) => {
            const created = await projectGroupsApi.create({
              id: form.id, name: form.name, description: form.description, department_id: form.department_id || null,
            });
            setCreating(false);
            toast("ok", t("v3.groups.created", { name: created.name }));
            setParams({ id: created.id });
          }} />
      )}
    </div>
  );
}

/* ── one group ───────────────────────────────────────────────────────────── */

/** Add accounts to the group with one built-in template (§7.7 "batch add"). */
type Candidate = Pick<ConsoleUser, "id" | "username" | "email">;

/** Accounts to add: the user directory for an administrator; a department
 *  administrator (no `GET /api/users`) searches a group project's candidates. */
async function searchCandidates(isAdmin: boolean, group: ProjectGroupDetail, q: string): Promise<Candidate[]> {
  if (isAdmin) return (await usersApi.list({ q, limit: 20 })).items;
  const via = group.projects[0]?.id;
  if (!via) return [];
  return (await projectsApi.candidates(via, q)).candidates.map((c) => ({ id: c.user_id, username: c.username, email: c.email }));
}

function AddMembersDialog({ group, templates, onClose, onDone }: {
  group: ProjectGroupDetail;
  templates: string[];
  onClose: () => void;
  onDone: (result: ProjectGroupDetail) => void;
}) {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  const label = useRoleLabel();
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<Candidate[]>([]);
  const [role, setRole] = useState(templates.includes("developer") ? "developer" : (templates[0] ?? ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const existing = new Set(group.members.map((m) => m.user_id));
  const search = useLoad(
    async () => (q.trim() ? (await searchCandidates(isAdmin, group, q.trim())).filter((u) => !existing.has(u.id)) : []),
    `v3-group-candidates:${group.id}:${q.trim()}`,
  );
  const toggle = (u: Candidate) =>
    setPicked((prev) => (prev.some((p) => p.id === u.id) ? prev.filter((p) => p.id !== u.id) : [...prev, u]));

  const submit = async () => {
    if (picked.length === 0 || !role) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await projectGroupsApi.putMembers(group.id, { upsert: picked.map((u) => ({ user_id: u.id, role_key: role })) }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      wide
      title={t("v3.groups.members.addTitle", { name: group.name })}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" busy={busy} disabled={picked.length === 0 || !role}
            disabledReason={picked.length === 0 ? t("story.reason.pickPeople") : t("story.reason.pickRole")} onClick={() => void submit()}>
            {t("v3.groups.members.addSubmit", { count: picked.length })}
          </Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-group-add-members">
        <label className="v3-field">
          <span>{t("v3.admins.add.search")}</span>
          <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("v2.users.search")} />
        </label>
        <div className="v3-usr-chips">
          {(search.data ?? []).map((u) => {
            const on = picked.some((p) => p.id === u.id);
            return (
              <button key={u.id} type="button" className={on ? "v3-usr-chip on" : "v3-usr-chip"} aria-pressed={on} onClick={() => toggle(u)}>
                {u.username}<small>{u.email}</small>
              </button>
            );
          })}
          {q.trim() && !search.loading && (search.data ?? []).length === 0 && <small className="v3-hint">{t("v3.admins.add.noMatch")}</small>}
        </div>
        {!isAdmin && group.projects.length === 0 && <small className="v3-hint">{t("v3.groups.members.needProject")}</small>}
        {picked.length > 0 && <small className="v3-hint">{t("v3.groups.members.picked", { names: picked.map((u) => u.username).join(", ") })}</small>}
        <label className="v3-field">
          <span>{t("v3.users.access.role")}</span>
          <select className="v3-select" value={role} onChange={(e) => setRole(e.target.value)}>
            {templates.map((key) => <option key={key} value={key}>{label(key)}</option>)}
          </select>
          <small className="v3-hint">{t("v3.groups.members.templatesOnly")}</small>
        </label>
        {error && <Notice s="act">{error}</Notice>}
      </div>
    </Dialog>
  );
}

function GroupDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const { isAdmin, managed } = useGroupScope();
  const toast = useToast();
  const failed = useFailure();
  const label = useRoleLabel();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "members" ? "members" : "projects";
  const loaded = useLoad(() => projectGroupsApi.get(id), `v3-group:${id}`);
  const departments = useLoad(() => departmentsApi.list("all"), "v3-groups-departments");
  const projects = useLoad(() => projectsApi.list({ status: "active" }), "v3-group-projects");
  const templates = useLoad(() => projectsApi.roleTemplates(), "v3-group-templates");
  const [editing, setEditing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [addingProject, setAddingProject] = useState("");
  const [removingProject, setRemovingProject] = useState<GroupProject | null>(null);
  const [removingMember, setRemovingMember] = useState<GroupMember | null>(null);
  const [addingMembers, setAddingMembers] = useState(false);
  const [busy, setBusy] = useState(false);
  const back = () => setParams({});
  const group = loaded.data;
  const templateKeys = templates.data?.templates.map((tpl) => tpl.key) ?? [];

  const run = async <T,>(call: () => Promise<T>, ok: (result: T) => string) => {
    setBusy(true);
    try {
      failed.clear();
      const result = await call();
      toast("ok", ok(result));
      loaded.reload();
      projects.reload();
      return true;
    } catch (err) {
      failed.fail(err);
      return false;
    } finally {
      setBusy(false);
    }
  };

  if (!group) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        <div><button type="button" className="v3-btn ghost sm" onClick={back}><ArrowLeft size={14} /> {t("v3.groups.title")}</button></div>
        {loaded.loading ? <Skeleton rows={5} /> : <Notice s="act">{loaded.error ?? t("v3.groups.notFound", { id })}</Notice>}
      </div>
    );
  }
  const dept = group.department_id ? departments.data?.departments.find((d) => d.id === group.department_id) : null;
  // a department administrator adds only their department's projects to its groups
  const choices = projectChoices(
    (projects.data?.projects ?? []).filter((p) => isAdmin || (p.department_id !== null && p.department_id === group.department_id)),
    group,
  );
  const deptChoices = (departments.data?.departments ?? []).filter((d) => isAdmin || managed.includes(d.id));
  const memberCount = group.members.length;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div><button type="button" className="v3-btn ghost sm" onClick={back}><ArrowLeft size={14} /> {t("v3.groups.title")}</button></div>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-group-error" />
      <PageHead
        eyebrow={`${t("v3.groups.eyebrow")} · ${group.id}`}
        title={group.name}
        sub={group.description || (dept ? dept.name : t("v3.groups.noDepartment"))}
        end={
          <>
            <Btn disabled={busy} onClick={() => setEditing(true)}><Pencil size={14} /> {t("v3.groups.edit")}</Btn>
            {group.archived ? (
              <Btn disabled={busy} onClick={() => void run(() => projectGroupsApi.unarchive(group.id), () => t("v3.groups.unarchived", { name: group.name }))}>
                <ArchiveRestore size={14} /> {t("v3.groups.unarchive")}
              </Btn>
            ) : (
              <Btn kind="danger" disabled={busy} onClick={() => setArchiving(true)}><Archive size={14} /> {t("v3.groups.archive")}</Btn>
            )}
          </>
        }
      />
      {group.archived && <Notice s="wait">{t("v3.groups.archivedNotice")}</Notice>}
      <Filters
        value={tab}
        onChange={(v) => setParams(v === "projects" ? { id } : { id, tab: v })}
        options={[
          { value: "projects", label: t("v3.groups.tab.projects"), count: group.projects.length },
          { value: "members", label: t("v3.groups.tab.members"), count: memberCount },
        ]}
      />

      {tab === "projects" ? (
        <Panel
          title={t("v3.groups.tab.projects")}
          end={
            <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <Reasoned disabledReason={group.archived ? t("story.reason.archivedGroup") : null}>
                <select className="v3-select" style={{ width: "auto" }} value={addingProject} disabled={busy || group.archived}
                  aria-label={t("v3.groups.projects.add")} onChange={(e) => setAddingProject(e.target.value)}>
                  <option value="">{t("v3.groups.projects.pick")}</option>
                  {choices.map((c) => (
                    <option key={c.id} value={c.id} disabled={c.otherGroup !== null}>
                      {c.name}{c.otherGroup ? ` · ${t("v3.groups.projects.inOther", { group: c.otherGroup })}` : ""}
                    </option>
                  ))}
                </select>
              </Reasoned>
              <Btn size="sm" kind="primary" busy={busy} disabled={!addingProject}
                disabledReason={group.archived ? t("story.reason.archivedGroup") : t("story.reason.pickProject")}
                onClick={() => {
                  const pid = addingProject;
                  void run(() => projectGroupsApi.putProjects(group.id, { add: [pid] }), () => t("v3.groups.projects.added")).then((ok) => ok && setAddingProject(""));
                }}>
                <Plus size={13} /> {t("v3.groups.projects.add")}
              </Btn>
            </span>
          }
        >
          <p className="v3-usr-muted" style={{ margin: "0 0 10px" }}>{t("v3.groups.projects.sub")}</p>
          {group.projects.length === 0 ? (
            <span className="v3-usr-muted">{t("v3.groups.projects.none")}</span>
          ) : (
            <table className="v3-table" data-testid="v3-group-projects">
              <tbody>
                {group.projects.map((p) => (
                  <tr key={p.id}>
                    <td><div className="v3-name"><div><b>{p.name}</b><small className="mono">{p.id}</small></div></div></td>
                    <td>{p.status !== "active" && <Chip>{p.status}</Chip>}</td>
                    <td style={{ width: 1 }}>
                      <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setRemovingProject(p)}>{t("v3.groups.projects.remove")}</Btn>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      ) : (
        <Panel
          title={t("v3.groups.tab.members")}
          end={<Btn size="sm" kind="primary" busy={busy} disabled={group.archived} disabledReason={t("story.reason.archivedGroup")} onClick={() => setAddingMembers(true)}><UserPlus size={13} /> {t("v3.groups.members.add")}</Btn>}
          flush={group.members.length > 0}
        >
          {group.members.length === 0 ? (
            <span className="v3-usr-muted">{t("v3.groups.members.none")}</span>
          ) : (
            <table className="v3-table" data-testid="v3-group-members">
              <thead>
                <tr>
                  <th>{t("v2.users.col.user")}</th>
                  <th>{t("v3.users.access.role")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {group.members.map((m) => (
                  <tr key={m.user_id}>
                    <td>
                      <div className="v3-name"><div><b>{m.username ?? m.user_id}</b><small>{m.email ?? ""}</small></div></div>
                      {m.sod_conflicts.length > 0 && (
                        <Chip s="wait" title={m.sod_conflicts.map((c) => `${c.project_id}: ${c.pairs.map((p) => p.join(" + ")).join("; ")}`).join("\n")}>
                          {t("v3.groups.members.sod", { count: m.sod_conflicts.length })}
                        </Chip>
                      )}
                    </td>
                    <td>
                      <Reasoned disabledReason={group.archived ? t("story.reason.archivedGroup") : null}>
                        <select className="v3-select" style={{ width: "auto" }} value={m.role_key} disabled={busy || group.archived}
                          aria-label={t("v3.users.access.role")}
                          onChange={(e) => {
                            const role = e.target.value;
                            void run(() => projectGroupsApi.putMembers(group.id, { upsert: [{ user_id: m.user_id, role_key: role }] }),
                              () => t("v3.groups.members.updated", { username: m.username ?? m.user_id }));
                          }}>
                          {templateKeys.map((key) => <option key={key} value={key}>{label(key)}</option>)}
                          {!templateKeys.includes(m.role_key) && <option value={m.role_key}>{label(m.role_key)}</option>}
                        </select>
                      </Reasoned>
                    </td>
                    <td style={{ width: 1 }}>
                      <Btn size="sm" kind="ghost" disabled={busy} title={t("v3.groups.members.remove")} onClick={() => setRemovingMember(m)}>
                        <Trash2 size={13} />
                      </Btn>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      )}

      {editing && (
        <StructureDialog kind="group" creating={false} departments={deptChoices} requireDepartment={!isAdmin}
          initial={{ id: group.id, name: group.name, description: group.description, department_id: group.department_id ?? "" }}
          onClose={() => setEditing(false)}
          onSubmit={async (form) => {
            await projectGroupsApi.update(group.id, { name: form.name, description: form.description, department_id: form.department_id || null });
            setEditing(false);
            toast("ok", t("v3.groups.saved", { name: form.name }));
            loaded.reload();
          }} />
      )}
      {archiving && (
        <Confirm title={t("v3.groups.archiveTitle", { name: group.name })} confirmLabel={t("v3.groups.archive")} cancelLabel={t("v3.common.cancel")}
          danger busy={busy} onCancel={() => setArchiving(false)}
          onConfirm={() => void run(() => projectGroupsApi.archive(group.id), (r) => t("v3.groups.archived", { name: group.name, keys: r.keys_disabled }))
            .then((ok) => ok && setArchiving(false))}>
          {t("v3.groups.archiveBody", { count: memberCount })}
        </Confirm>
      )}
      {removingProject && (
        <Confirm title={t("v3.groups.projects.removeTitle", { name: removingProject.name })} confirmLabel={t("v3.groups.projects.remove")}
          cancelLabel={t("v3.common.cancel")} danger busy={busy} onCancel={() => setRemovingProject(null)}
          onConfirm={() => {
            const pid = removingProject.id;
            void run(() => projectGroupsApi.putProjects(group.id, { remove: [pid] }), () => t("v3.groups.projects.removed"))
              .then((ok) => ok && setRemovingProject(null));
          }}>
          {t("v3.groups.projects.removeBody", { count: memberCount })}
        </Confirm>
      )}
      {removingMember && (
        <Confirm title={t("v3.groups.members.removeTitle", { username: removingMember.username ?? removingMember.user_id })}
          confirmLabel={t("v3.groups.members.remove")} cancelLabel={t("v3.common.cancel")} danger busy={busy}
          onCancel={() => setRemovingMember(null)}
          onConfirm={() => {
            const uid = removingMember.user_id;
            void run(() => projectGroupsApi.putMembers(group.id, { remove: [uid] }),
              () => t("v3.groups.members.removed", { username: removingMember.username ?? uid })).then((ok) => ok && setRemovingMember(null));
          }}>
          {t("v3.groups.members.removeBody", { count: group.projects.length })}
        </Confirm>
      )}
      {addingMembers && (
        <AddMembersDialog group={group} templates={templateKeys} onClose={() => setAddingMembers(false)}
          onDone={(result) => {
            setAddingMembers(false);
            toast("ok", t("v3.groups.members.addedMany", { count: result.members.length - memberCount }));
            loaded.reload();
          }} />
      )}
    </div>
  );
}

/**
 * Project groups (projects §7.7; platform administrators, and since §11.1 a
 * department administrator for their own departments' groups): the list with each
 * group's department, projects and member count; one group at `?id=` with its
 * projects (join / leave — leaving revokes the access the group gave, direct
 * members keep theirs) and its members (built-in templates only). Archive stops
 * the group's access; its projects keep their group. Hub-global.
 */
export function V3ProjectGroups() {
  const { isAdmin } = useAuth();
  const { managedDepartments } = useProject();
  const [params] = useSearchParams();
  if (!isAdmin && managedDepartments.length === 0) return <Forbidden />;
  const id = params.get("id");
  return id ? <GroupDetail key={id} id={id} /> : <GroupList />;
}
