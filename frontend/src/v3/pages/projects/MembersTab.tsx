import { AlertTriangle, Pencil, Search, Trash2, UserPlus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import {
  type MemberCandidate,
  type ProjectDetail,
  type ProjectMember,
  type ProjectRoles,
  projectsApi,
} from "../../../lib/api/projects";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Btn, Chip, Confirm, Dialog, Empty, Filters, Notice, Panel, Skeleton } from "../../ui";
import { KeyPicker } from "./KeyPicker";
import { useRoleLabel, useSourceLabel } from "./labels";
import {
  applyOverrides,
  assignableRoles,
  filterMembers,
  memberRowState,
  overridesFor,
  roleKeys,
  type SourceFilter,
  sodRefusal,
} from "./model";
import type { ProjectAbilities } from "./ProjectDetail";
import { StewardSection } from "./StewardSection";
import { useStructureNames } from "./useStructureNames";

/**
 * §7.8: direct and group members, their effective keys and the combined-duties
 * hint; a manager adds, edits and removes direct rows. Group rows are managed on
 * the group; nobody but an administrator changes their own row (§3.6).
 */
export function MembersTab({
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
  const failed = useFailure();
  const { username } = useAuth();
  const [tick, setTick] = useState(0);
  const members = useLoad(() => projectsApi.members(project.id), `v3-project-members:${project.id}:${tick}`);
  const roles = useLoad(() => projectsApi.roles(project.id), `v3-project-roles:${project.id}`);
  const names = useStructureNames(abilities.admin);
  const roleLabel = useRoleLabel(roles.data?.custom);
  const sourceLabel = useSourceLabel(names.group);
  const [role, setRole] = useState("all");
  const [source, setSource] = useState<SourceFilter>("all");
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<ProjectMember | "new" | null>(null);
  const [removing, setRemoving] = useState<ProjectMember | null>(null);
  const [busy, setBusy] = useState(false);
  const all = useMemo(() => members.data?.members ?? [], [members.data]);
  const rows = useMemo(() => filterMembers(all, { role, source, q }), [all, role, source, q]);
  const strict = members.data?.sod_strict ?? false;
  const canWrite = abilities.manage && project.status === "active";
  const reload = () => {
    setTick((n) => n + 1);
    onChanged();
  };

  const remove = async (m: ProjectMember) => {
    setBusy(true);
    try {
      failed.clear();
      await projectsApi.removeMember(project.id, m.user_id);
      toast("ok", t("v3.projects.members.removed", { name: m.username ?? m.user_id }));
      reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
      setRemoving(null);
    }
  };

  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="project-members">
      {/* §11.4: an organization admin's changes here, and the ones waiting for an owner */}
      <StewardSection projectId={project.id} abilities={abilities} onChanged={reload} />
      {strict && <Notice s="info">{t("v3.projects.members.strictNotice")}</Notice>}
      <div className="v3-prj-filters">
        <select className="v3-input" aria-label={t("v3.projects.members.role")} value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="all">{t("v3.projects.members.role")}: {t("v3.projects.filter.all")}</option>
          {roles.data && assignableRoles(roles.data).map((r) => <option key={r.key} value={r.key}>{roleLabel(r.key)}</option>)}
        </select>
        <Filters
          value={source}
          onChange={setSource}
          options={[
            { value: "all", label: t("v3.projects.members.source.all") },
            { value: "direct", label: t("v3.projects.members.source.direct") },
            { value: "group", label: t("v3.projects.members.source.group") },
          ]}
        />
        <div className="v3-prj-search">
          <Search size={14} aria-hidden="true" />
          <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.projects.members.search")} aria-label={t("v3.projects.members.search")} />
        </div>
        {canWrite && <Btn kind="primary" onClick={() => setEditing("new")}><UserPlus size={14} /> {t("v3.projects.members.add")}</Btn>}
      </div>

      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-project-members-error" />
      <Panel flush>
        {members.loading && !members.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : members.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={members.failure ?? members.error} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v3.projects.members.empty")} />
        ) : (
          <table className="v3-table" data-testid="project-members-table">
            <thead>
              <tr>
                <th>{t("v3.projects.members.col.member")}</th>
                <th>{t("v3.projects.members.col.roles")}</th>
                <th>{t("v3.projects.members.col.source")}</th>
                <th>{t("v3.projects.members.col.overrides")}</th>
                <th className="num">{t("v3.projects.members.col.keys")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => {
                const state = memberRowState(m, { canManage: canWrite, isAdmin: abilities.admin, me: username });
                const overrides = Object.entries(m.direct?.overrides ?? {});
                return (
                  <tr key={m.user_id} data-testid={`member-${m.user_id}`}>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b>{m.username ?? m.user_id}{m.bypass && <> <Chip s="info">{t("v3.projects.members.admin")}</Chip></>}</b>
                          <small>{m.email ?? "—"}</small>
                        </div>
                      </div>
                    </td>
                    <td>
                      <span className="v3-prj-chips">
                        {[...new Set(m.sources.map((s) => s.role_key))].map((key) => <Chip key={key}>{roleLabel(key)}</Chip>)}
                        {m.sod_conflicts.length > 0 && (
                          <span title={t(strict ? "v3.projects.sod.strictHint" : "v3.projects.sod.hint", {
                            pairs: m.sod_conflicts.map((p) => p.join(" + ")).join("; "),
                          })} data-testid="member-sod">
                            <Chip s="wait"><AlertTriangle size={11} aria-hidden="true" /> {t("v3.projects.sod.chip")}</Chip>
                          </span>
                        )}
                      </span>
                    </td>
                    <td>{m.sources.map((s) => sourceLabel(s.source)).join(" · ")}</td>
                    <td className="mono">
                      {overrides.length === 0 ? "—" : overrides.map(([k, on]) => `${on ? "+" : "−"}${k}`).join(" ")}
                    </td>
                    <td className="num" title={m.permissions.join(", ")}>{m.permissions.length}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      {state === "editable" && (
                        <span style={{ display: "inline-flex", gap: 6 }}>
                          <Btn size="sm" onClick={() => setEditing(m)}><Pencil size={12} /> {t("v3.projects.members.edit")}</Btn>
                          <Btn size="sm" kind="danger" onClick={() => setRemoving(m)} disabled={busy}><Trash2 size={12} /> {t("v3.projects.remove")}</Btn>
                        </span>
                      )}
                      {state === "group" && (
                        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                          <span className="v3-prj-muted">{t("v3.projects.members.inGroup")}</span>
                          <Btn size="sm" onClick={() => setEditing(m)}>{t("v3.projects.members.addDirect")}</Btn>
                        </span>
                      )}
                      {state === "self" && <span className="v3-prj-muted">{t("v3.projects.members.self")}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      {editing && roles.data && (
        <MemberDialog
          project={project}
          roles={roles.data}
          member={editing === "new" ? null : editing}
          strict={strict}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
      {removing && (
        <Confirm
          title={t("v3.projects.members.removeTitle")}
          confirmLabel={t("v3.projects.remove")}
          cancelLabel={t("v3.projects.cancel")}
          danger
          busy={busy}
          onConfirm={() => void remove(removing)}
          onCancel={() => setRemoving(null)}
        >
          {t("v3.projects.members.removeBody", { name: removing.username ?? removing.user_id, project: project.name })}
        </Confirm>
      )}
    </div>
  );
}

/** Add a member (candidate search) or edit a direct row: role, then optional per-key overrides. */
function MemberDialog({
  project,
  roles,
  member,
  strict,
  onClose,
  onSaved,
}: {
  project: ProjectDetail;
  roles: ProjectRoles;
  /** null ⇒ add */
  member: ProjectMember | null;
  strict: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const roleLabel = useRoleLabel(roles.custom);
  const options = assignableRoles(roles);
  const initialRole = member?.direct?.role_key ?? member?.group?.role_key ?? (options.find((o) => o.key === "developer")?.key ?? options[0]?.key ?? "viewer");
  const [target, setTarget] = useState<{ user_id: string; username: string } | null>(
    member ? { user_id: member.user_id, username: member.username ?? member.user_id } : null,
  );
  const [roleKey, setRoleKey] = useState(initialRole);
  const base = roleKeys(roles, roleKey);
  const [custom, setCustom] = useState(Object.keys(member?.direct?.overrides ?? {}).length > 0);
  const [keys, setKeys] = useState<string[]>(() => applyOverrides(roleKeys(roles, initialRole) ?? [], member?.direct?.overrides));
  const [q, setQ] = useState("");
  const candidates = useLoad<{ candidates: MemberCandidate[] }>(
    () => (member ? Promise.resolve({ candidates: [] }) : projectsApi.candidates(project.id, q.trim())),
    `v3-project-candidates:${project.id}:${member ? "-" : q.trim()}`,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const pickRole = (next: string) => {
    setRoleKey(next);
    setKeys(roleKeys(roles, next) ?? []);
  };
  const overrides = base === null
    ? (member?.direct?.overrides && Object.keys(member.direct.overrides).length ? member.direct.overrides : null)
    : custom ? overridesFor(base, keys) : null;

  const save = async () => {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      const result = await projectsApi.putMembers(project.id, {
        upsert: [{ user_id: target.user_id, role_key: roleKey, overrides }],
      });
      const added = result.added.includes(target.user_id);
      toast("ok", t(added ? "v3.projects.members.added" : "v3.projects.members.updated", { name: target.username }));
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
      title={member ? t("v3.projects.members.editTitle", { name: target?.username }) : t("v3.projects.members.addTitle")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.projects.cancel")}</Btn>
          <Btn kind="primary" onClick={() => void save()} busy={busy} disabled={!target} disabledReason={t("story.reason.pickUser")}>{t("v3.projects.save")}</Btn>
        </>
      }
    >
      <div className="v3-prj-form" data-testid="member-dialog">
        {!member && (
          <div className="v3-field">
            <span>{t("v3.projects.members.candidates")}</span>
            <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)} autoFocus
              placeholder={t("v3.projects.members.candidatesHint")} aria-label={t("v3.projects.members.candidates")} />
            <div className="v3-prj-cands" data-testid="member-candidates">
              {(candidates.data?.candidates ?? []).map((c) => (
                <Reasoned key={c.user_id} disabledReason={t("story.reason.alreadyMember")}>
                  <button type="button" disabled={c.is_member} aria-pressed={target?.user_id === c.user_id}
                    onClick={() => setTarget({ user_id: c.user_id, username: c.username })}>
                    <b>{c.username}</b>
                    <span className="v3-prj-muted mono">{c.is_member ? t("v3.projects.members.isMember") : c.email}</span>
                  </button>
                </Reasoned>
              ))}
              {candidates.data && candidates.data.candidates.length === 0 && (
                <span className="v3-prj-muted">{t("v3.projects.members.noCandidates")}</span>
              )}
            </div>
          </div>
        )}
        {member && !member.direct && member.group && (
          <Notice s="info">{t("v3.projects.members.directOverGroup", { role: roleLabel(member.group.role_key) })}</Notice>
        )}
        <label className="v3-field">
          <span>{t("v3.projects.members.role")}</span>
          <select className="v3-input" value={roleKey} onChange={(e) => pickRole(e.target.value)}>
            {options.map((o) => <option key={o.key} value={o.key}>{roleLabel(o.key)}</option>)}
          </select>
        </label>
        {base === null ? (
          <small className="v3-hint">{t("v3.projects.members.inheritHint")}</small>
        ) : (
          <>
            <label className="v3-prj-checks">
              <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input type="checkbox" checked={custom} onChange={(e) => {
                  setCustom(e.target.checked);
                  if (!e.target.checked) setKeys(base);
                }} />
                {t("v3.projects.members.customize")}
              </span>
            </label>
            {custom && (
              <KeyPicker projectKeys={roles.project_keys} agentKeys={roles.agent_keys} value={keys} onChange={setKeys} strict={strict} />
            )}
          </>
        )}
        {error != null && (
          <Notice s="act">
            {refusal ? (
              <>
                <div>{t("v3.projects.sod.refused", { names: refusal.map((c) => c.username ?? c.user_id).join(", ") })}</div>
                <ApiErrorDetail error={error} />
              </>
            ) : (
              <ApiErrorDetail error={error} />
            )}
          </Notice>
        )}
      </div>
    </Dialog>
  );
}
