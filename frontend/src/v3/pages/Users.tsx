import "./users.css";

import { ArrowLeft, FolderPlus, KeyRound, LogOut, Pencil, RefreshCw, Search, ShieldAlert, ShieldCheck, Trash2, UserCheck, UserPlus, UserX } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { AGENT_PERMISSIONS, togglePermission, api, type ConsoleUser, type UserState } from "../../lib/api";
import { isSteward } from "../../lib/api/steward";
import { type ProjectUser, usersApi } from "../../lib/api/users";
import { PAGE_SIZE, USER_STATES } from "../../v2/pages/users/common";
import { useWorkspace } from "../../workspace/workspace-context";
import { ago } from "../format";
import { useLoad } from "../hooks";
import { ChangePasswordDialog } from "../ChangePassword";
import { Btn, Chip, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { AccessPanel } from "./users/AccessPanel";
import { useUserActions } from "./users/actions";
import { BulkAddDialog } from "./users/BulkAdd";
import { isPlatformRole, listFilterFrom } from "./users/forms";
import { GrantChip } from "./users/GrantChip";
import { useRoleLabel, useStructure } from "./users/structure";

const STATE_SIGNAL: Record<UserState, Signal> = { pending: "wait", active: "ok", expired: "act", disabled: "off" };

function StateChip({ state }: { state: UserState }) {
  const { t } = useTranslation();
  const s = STATE_SIGNAL[state];
  return <Chip s={s === "off" ? undefined : s}>{t(`v2.users.state.${state}`)}</Chip>;
}

function RoleChip({ role, builtin }: { role: ConsoleUser["role"]; builtin?: boolean }) {
  const { t } = useTranslation();
  return (
    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
      <Chip s={role === "admin" ? "info" : role === "steward" ? "wait" : undefined}>{t(`v2.users.role.${role}`)}</Chip>
      {builtin && <Chip>{t("v3.users.builtin.chip")}</Chip>}
    </span>
  );
}

/** Expiry + days left; a pending account's window only starts on approval. */
function Validity({ user }: { user: ConsoleUser }) {
  const { t } = useTranslation();
  if (!user.expires_at) {
    return <span className="v3-usr-muted">{t(user.state === "pending" ? "usersPage.startsOnApproval" : "usersPage.neverExpires")}</span>;
  }
  const soon = user.state === "active" && (user.days_remaining ?? 0) <= 3;
  return (
    <span className={soon ? "v3-usr-soon" : undefined}>
      {t("usersPage.daysRemaining", { count: user.days_remaining ?? 0 })}
      <small className="v3-usr-muted"> · {user.expires_at.slice(0, 10)}</small>
    </span>
  );
}

/** "Project (role) +n" of a list row; an administrator reaches all of them by role. */
function ProjectsCell({ user }: { user: ProjectUser }) {
  const { t } = useTranslation();
  const label = useRoleLabel();
  if (user.role === "admin") return <span className="v3-usr-muted">{t("v3.users.access.allProjects")}</span>;
  const projects = user.projects ?? [];
  if (projects.length === 0) return <span className="v3-usr-muted">—</span>;
  return (
    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }} title={projects.map((p) => `${p.name} (${label(p.role)})`).join(", ")}>
      {projects.slice(0, 2).map((p) => <Chip key={p.id}>{p.name} · {label(p.role)}</Chip>)}
      {projects.length > 2 && <span className="v3-usr-muted">+{projects.length - 2}</span>}
    </span>
  );
}

function Forbidden() {
  const { t } = useTranslation();
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v3.users.eyebrow")} title={t("nav.users")} sub={t("auth.adminRequired.meta")} />
      <Panel>
        <Empty title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ShieldAlert size={16} /> {t("auth.adminRequired.title")}</span>}>
          {t("usersPage.forbiddenBody")}
        </Empty>
      </Panel>
    </div>
  );
}

/* ── list ────────────────────────────────────────────────────────────────── */

function UserList() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const { page, ...filter } = listFilterFrom(params, PAGE_SIZE);
  const status = filter.status ?? "all";
  const query = filter.q ?? "";
  const list = useLoad(
    () => usersApi.list(filter),
    `v3-users:${status}:${query}:${filter.role ?? ""}:${filter.project ?? ""}:${filter.group ?? ""}:${page}`,
  );
  const stats = useLoad(() => api.userStats(), "v3-users-stats");
  const structure = useStructure();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkOpen, setBulkOpen] = useState(false);
  const reload = () => {
    list.reload();
    stats.reload();
  };
  const actions = useUserActions(reload, stats.data?.valid_days ?? 7, structure);
  // the project columns, filters and bulk action once the platform has more than the default project
  const multi = structure.multiProject;
  const liveGroups = structure.groups.filter((g) => !g.archived);
  const setParam = (key: string, value: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "page") next.delete("page"); // any filter change restarts paging
        return next;
      },
      { replace: key === "q" },
    );
  // the built-in admin has no row to page over: pinned first on page 1 when it matches
  const builtin = list.data?.builtin ?? null;
  const rows = useMemo<ProjectUser[]>(
    () => [...(builtin && page === 1 ? [builtin as ProjectUser] : []), ...(list.data?.items ?? [])],
    [list.data, builtin, page],
  );
  const selectable = rows.filter((u) => !u.builtin);
  const chosen = selectable.filter((u) => selected.has(u.id));
  const toggleOne = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allChosen = selectable.length > 0 && chosen.length === selectable.length;
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const s = stats.data;
  const open = (u: ConsoleUser) => setParams({ view: "detail", user: u.username });
  const trend = s?.registrations ?? [];
  const peak = Math.max(1, ...trend.map((p) => p.count));

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.users.eyebrow")}
        title={t("nav.users")}
        sub={t("usersPage.meta", { days: s?.valid_days ?? 7 })}
        end={
          <>
            <Btn kind="ghost" onClick={reload} disabled={list.loading}><RefreshCw size={14} /> {t("v3.users.refresh")}</Btn>
            {multi && (
              <Btn disabled={chosen.length === 0} onClick={() => setBulkOpen(true)} title={t("v3.users.bulk.hint")}>
                <FolderPlus size={14} /> {t("v3.users.bulk.open", { count: chosen.length })}
              </Btn>
            )}
            <Btn kind="primary" onClick={actions.openCreate}><UserPlus size={14} /> {t("v3.users.create.open")}</Btn>
          </>
        }
      />

      {s && s.pending > 0 && (
        <Notice s="wait">
          {t("v3.users.pendingNotice", { count: s.pending })}{" "}
          <button type="button" className="v3-btn sm" onClick={() => setParam("status", "pending")}>{t("v3.users.showPending")}</button>
        </Notice>
      )}

      <div className="v3-usr-stats">
        <Panel signal={s?.pending ? "wait" : undefined}><Stat label={t("v2.users.stat.pending")} value={s?.pending ?? "—"} signal={s?.pending ? "wait" : undefined} foot={t("v2.users.stat.pendingSub")} /></Panel>
        <Panel><Stat label={t("v2.users.stat.total")} value={s?.total ?? "—"} foot={t("usersPage.stats.registeredLast7d", { count: s?.registered_last_7d ?? 0 })} /></Panel>
        <Panel><Stat label={t("v2.users.stat.active")} value={s?.active ?? "—"} foot={t("usersPage.stats.activeLast7d", { count: s?.active_last_7d ?? 0 })} /></Panel>
        <Panel signal={s?.expiring_soon ? "wait" : undefined}><Stat label={t("v2.users.stat.expiringSoon")} value={s?.expiring_soon ?? "—"} foot={t("usersPage.stats.expiringSoonFoot")} /></Panel>
        <Panel><Stat label={t("v2.users.stat.expiredDisabled")} value={s ? s.expired + s.disabled : "—"}
          foot={t("usersPage.stats.expiredDisabledFoot", { expired: s?.expired ?? 0, disabled: s?.disabled ?? 0 })} /></Panel>
      </div>

      <Panel title={t("v2.users.trendTitle")} end={<span>{t("usersPage.trendSub")}</span>}>
        {trend.length === 0 ? (
          <Skeleton rows={1} />
        ) : (
          <div className="v3-usr-trend" role="img" aria-label={t("v2.users.trendTitle")}>
            {trend.map((p) => (
              <div className="col" key={p.date} title={`${p.date} · ${p.count}`}>
                <span className="n">{p.count || ""}</span>
                <div className="wrap"><div className="bar" style={{ height: `${Math.round((p.count / peak) * 100)}%` }} /></div>
                <span className="d">{p.date.slice(5)}</span>
              </div>
            ))}
          </div>
        )}
        {s && s.top_domains.length > 0 && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginTop: 12 }}>
            <span className="v3-usr-muted">{t("v2.users.topDomains")}</span>
            {s.top_domains.map((d) => <Chip key={d.domain}>{d.domain} · {d.count}</Chip>)}
          </div>
        )}
      </Panel>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={status}
          onChange={(v) => setParam("status", v === "all" ? null : v)}
          options={[
            { value: "all", label: t("v3.users.all") },
            ...USER_STATES.map((v) => ({ value: v, label: t(`v2.users.state.${v}`), s: STATE_SIGNAL[v] })),
          ]}
        />
        <select className="v3-select" style={{ width: "auto" }} value={filter.role ?? ""} aria-label={t("v3.users.filter.role")}
          onChange={(e) => setParam("role", e.target.value || null)}>
          <option value="">{t("v3.users.filter.anyRole")}</option>
          {(["admin", "steward", "operator", "member"] as const).map((r) => <option key={r} value={r}>{t(`v2.users.role.${r}`)}</option>)}
        </select>
        {(multi || filter.project) && (
          <select className="v3-select" style={{ width: "auto" }} value={filter.project ?? ""} aria-label={t("v3.users.filter.project")}
            onChange={(e) => setParam("project", e.target.value || null)}>
            <option value="">{t("v3.users.filter.anyProject")}</option>
            {structure.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            {filter.project && !structure.projects.some((p) => p.id === filter.project) && <option value={filter.project}>{filter.project}</option>}
          </select>
        )}
        {(liveGroups.length > 0 || filter.group) && (
          <select className="v3-select" style={{ width: "auto" }} value={filter.group ?? ""} aria-label={t("v3.users.filter.group")}
            onChange={(e) => setParam("group", e.target.value || null)}>
            <option value="">{t("v3.users.filter.anyGroup")}</option>
            {liveGroups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            {filter.group && !liveGroups.some((g) => g.id === filter.group) && <option value={filter.group}>{filter.group}</option>}
          </select>
        )}
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={query} onChange={(e) => setParam("q", e.target.value || null)}
            placeholder={t("v2.users.search")} aria-label={t("v2.users.search")} />
        </div>
        <span className="v3-usr-muted mono">{t("v3.users.total", { count: total })}</span>
      </div>

      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}>
            <Notice s="act">{t("usersPage.loadFailed", { msg: list.error })}</Notice>
          </div>
        ) : rows.length === 0 ? (
          <Empty title={t("usersPage.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                {multi && (
                  <th style={{ width: 30 }}>
                    <input type="checkbox" checked={allChosen} aria-label={t("v3.users.bulk.selectAll")}
                      onChange={() => setSelected(allChosen ? new Set() : new Set(selectable.map((u) => u.id)))} />
                  </th>
                )}
                <th />
                <th>{t("v2.users.col.user")}</th>
                <th>{t("v2.users.col.role")}</th>
                <th>{t("v2.users.col.state")}</th>
                <th>{t(multi ? "v3.users.col.projects" : "v2.users.col.workspaces")}</th>
                <th>{t("v2.users.col.validity")}</th>
                <th className="num">{t("v2.users.col.lastLogin")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((u) => {
                const busy = actions.busyId === u.id;
                return (
                  <tr key={u.id} className="click" onClick={() => open(u)}>
                    {multi && (
                      <td style={{ width: 30 }} onClick={(e) => e.stopPropagation()}>
                        {!u.builtin && (
                          <input type="checkbox" checked={selected.has(u.id)} aria-label={t("v3.users.bulk.select", { username: u.username })}
                            onChange={() => toggleOne(u.id)} />
                        )}
                      </td>
                    )}
                    <td style={{ width: 30 }}><Lamp s={STATE_SIGNAL[u.state]} /></td>
                    <td><div className="v3-name"><div><b>{u.username}</b><small>{u.email}</small></div></div></td>
                    <td><RoleChip role={u.role} builtin={u.builtin} /></td>
                    <td><StateChip state={u.state} /></td>
                    <td>
                      {multi ? (
                        <ProjectsCell user={u} />
                      ) : u.role === "admin" ? (
                        <span className="v3-usr-muted">{t("v2.users.allByRole")}</span>
                      ) : u.workspaces.length === 0 ? (
                        <span className="v3-usr-muted">—</span>
                      ) : (
                        <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }} title={u.workspaces.join(", ")}>
                          {u.workspaces.slice(0, 2).map((id) => <Chip key={id}>{id}</Chip>)}
                          {u.workspaces.length > 2 && <span className="v3-usr-muted">+{u.workspaces.length - 2}</span>}
                        </span>
                      )}
                    </td>
                    <td><Validity user={u} /></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>
                      {ago(u.last_login_at)}
                      <small style={{ display: "block" }}>{t("v2.users.logins", { count: u.login_count })}</small>
                    </td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        {u.builtin ? null : u.state === "pending" ? (
                          <>
                            <Btn size="sm" kind="primary" disabled={busy} onClick={() => actions.openApprove(u)}>{t("v2.users.action.approve")}</Btn>
                            <Btn size="sm" disabled={busy} onClick={() => actions.ask("reject", u)}>{t("v2.users.action.reject")}</Btn>
                          </>
                        ) : (
                          <>
                            <Btn size="sm" kind="ghost" disabled={busy} onClick={() => actions.openExtend(u)}>{t("v2.users.action.extend")}</Btn>
                            <Btn size="sm" kind="ghost" disabled={busy} onClick={() => actions.toggleStatus(u)}>
                              {t(u.status === "active" ? "v2.users.action.disable" : "v2.users.action.enable")}
                            </Btn>
                          </>
                        )}
                        {!u.builtin && (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => actions.ask("delete", u)} title={t("v2.users.action.delete")}>
                            <Trash2 size={13} />
                          </Btn>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {total > PAGE_SIZE && (
          <div className="v3-usr-pager">
            <Btn size="sm" kind="ghost" disabled={page <= 1} onClick={() => setParam("page", String(page - 1))}>←</Btn>
            <span className="mono">{Math.min(page, pages)} / {pages}</span>
            <Btn size="sm" kind="ghost" disabled={page >= pages} onClick={() => setParam("page", String(page + 1))}>→</Btn>
          </div>
        )}
      </Panel>
      {actions.renderDialogs({ reservedName: builtin?.username ?? null })}
      {bulkOpen && (
        <BulkAddDialog users={chosen} structure={structure} onClose={() => setBulkOpen(false)}
          onDone={() => {
            setBulkOpen(false);
            setSelected(new Set());
            reload();
          }} />
      )}
    </div>
  );
}

/* ── one account ─────────────────────────────────────────────────────────── */

function UserDetail({ username }: { username: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const { workspaces } = useWorkspace();
  const auth = useAuth();
  const [changingOwn, setChangingOwn] = useState(false);
  // there is no GET-by-id route: read it back through the list search, matched exactly
  const loaded = useLoad<ProjectUser | null>(async () => {
    const page = await usersApi.list({ q: username, limit: 200 });
    if (page.builtin && page.builtin.username === username) return page.builtin as ProjectUser;
    return page.items.find((u) => u.username === username) ?? null;
  }, `v3-user:${username}`);
  const structure = useStructure();
  const access = useLoad(
    async () => (loaded.data && !loaded.data.builtin ? usersApi.access(loaded.data.id) : null),
    `v3-user-access:${loaded.data?.builtin ? "" : (loaded.data?.id ?? "")}`,
  );
  const reload = () => {
    loaded.reload();
    access.reload();
  };
  const actions = useUserActions(reload, 7, structure);
  const back = () => setParams({});
  const user = loaded.data;

  if (!user) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        <div><button type="button" className="v3-btn ghost sm" onClick={back}><ArrowLeft size={14} /> {t("nav.users")}</button></div>
        {loaded.loading ? <Skeleton rows={5} /> : (
          <Notice s="act">{loaded.error ? t("usersPage.loadFailed", { msg: loaded.error }) : t("v2.users.notFound", { username })}</Notice>
        )}
      </div>
    );
  }
  const busy = actions.busyId === user.id;
  const isAdminRole = user.role === "admin";
  // §11.4: a steward holds no grant, membership or agent permission (structure only)
  const isStewardRole = user.role === "steward";
  // only an administrator changes the platform roles; stewards are managed on the Administrators page
  const canToggleRole = auth.isAdmin && !isStewardRole;
  // the signed-in account itself: change (current + new) instead of a reset that
  // would end this very session
  const isSelf = auth.username === user.username && (auth.builtin ? Boolean(user.builtin) : !user.builtin);
  const passwordAction = isSelf ? (
    <Btn disabled={busy} onClick={() => setChangingOwn(true)}><KeyRound size={14} /> {t("v3.account.password.title")}</Btn>
  ) : (
    <Btn disabled={busy} onClick={() => actions.ask("reset", user)}><KeyRound size={14} /> {t("v2.users.action.resetPassword")}</Btn>
  );
  const ownPasswordDialog = changingOwn && (
    <ChangePasswordDialog username={user.username} builtin={user.builtin} onClose={() => { setChangingOwn(false); loaded.reload(); }} />
  );

  if (user.builtin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <div><button type="button" className="v3-btn ghost sm" onClick={back}><ArrowLeft size={14} /> {t("nav.users")}</button></div>
        <PageHead
          eyebrow={`${t("v3.users.eyebrow")} · ${t("v3.users.builtin.chip")}`}
          title={
            <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
              <Lamp s="ok" live />
              {user.username}
            </span>
          }
          sub={t("v3.users.builtin.sub")}
          end={passwordAction}
        />
        <Notice s="wait">{t("v3.users.builtin.notice")}</Notice>
        <div className="v3-grid c4">
          <Panel signal="ok"><Stat label={t("v2.users.col.state")} value={t("v2.users.state.active")} /></Panel>
          <Panel><Stat label={t("v2.users.col.role")} value={t("v2.users.role.admin")} /></Panel>
          <Panel><Stat label={t("v2.users.col.validity")} value={t("usersPage.neverExpires")} /></Panel>
          <Panel>
            <Stat label={t("v3.users.builtin.passwordSource")} value={t(`v3.users.builtin.source.${user.password_source ?? "config"}`)}
              foot={user.password_changed_at ? ago(user.password_changed_at) : undefined} />
          </Panel>
        </div>
        {actions.renderDialogs({ onDeleted: back })}
        {ownPasswordDialog}
      </div>
    );
  }

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div><button type="button" className="v3-btn ghost sm" onClick={back}><ArrowLeft size={14} /> {t("nav.users")}</button></div>
      <PageHead
        eyebrow={`${t("v3.users.eyebrow")} · ${user.id.slice(0, 8)}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={STATE_SIGNAL[user.state]} live={user.state === "active"} />
            {user.username}
          </span>
        }
        sub={user.email}
        end={
          <>
            {user.state === "pending" ? (
              <>
                <Btn disabled={busy} onClick={() => actions.ask("reject", user)}><UserX size={14} /> {t("v2.users.action.reject")}</Btn>
                <Btn kind="primary" disabled={busy} onClick={() => actions.openApprove(user)}><UserCheck size={14} /> {t("v2.users.action.approve")}</Btn>
              </>
            ) : (
              <Btn disabled={busy} onClick={() => actions.toggleStatus(user)}>
                {user.status === "active" ? <UserX size={14} /> : <UserCheck size={14} />}
                {t(user.status === "active" ? "v2.users.action.disable" : "v2.users.action.enable")}
              </Btn>
            )}
            {passwordAction}
            {!isSelf && (
              <Btn disabled={busy || user.state === "pending"} title={t("v3.users.sessions.hint")} onClick={() => actions.ask("sessions", user)}>
                <LogOut size={14} /> {t("v3.users.sessions.action")}
              </Btn>
            )}
            <Btn kind="danger" disabled={busy} onClick={() => actions.ask("delete", user)}><Trash2 size={14} /> {t("v2.users.action.delete")}</Btn>
          </>
        }
      />

      {user.state === "pending" && <Notice s="wait">{t("v2.users.pendingAlert")}</Notice>}
      {user.state === "expired" && <Notice s="act">{t("v2.users.expiredAlert")}</Notice>}
      {user.state === "disabled" && <Notice s="wait">{t("v2.users.disabledAlert")}</Notice>}

      <div className="v3-grid c4">
        <Panel signal={STATE_SIGNAL[user.state] === "off" ? undefined : STATE_SIGNAL[user.state]}>
          <Stat label={t("v2.users.col.state")} value={t(`v2.users.state.${user.state}`)} />
        </Panel>
        <Panel><Stat label={t("v2.users.col.role")} value={t(`v2.users.role.${user.role}`)} /></Panel>
        <Panel><Stat label={t("v2.users.col.validity")} value={<Validity user={user} />} /></Panel>
        <Panel><Stat label={t("v2.users.col.lastLogin")} value={ago(user.last_login_at)} foot={t("v2.users.logins", { count: user.login_count })} /></Panel>
      </div>

      <div className="v3-grid v3-split">
        <Panel title={t("v2.users.overview")}>
          <dl className="v3-kv">
            <dt>{t("v2.users.field.username")}</dt><dd>{user.username}</dd>
            <dt>{t("v2.users.field.email")}</dt><dd>{user.email}</dd>
            <dt>{t("v2.users.col.created")}</dt><dd>{user.created_at ? `${user.created_at.slice(0, 10)} · ${ago(user.created_at)}` : "—"}</dd>
            <dt>{t("v2.users.field.createdBy")}</dt><dd>{user.created_by || "—"}</dd>
            <dt>ID</dt><dd className="mono">{user.id}</dd>
          </dl>
        </Panel>
        <div style={{ display: "grid", gap: 16, alignContent: "start" }}>
          <Panel title={t("v2.users.validityTitle")}>
            <p className="v3-usr-muted" style={{ margin: "0 0 10px" }}>{t("v2.users.validitySub")}</p>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Btn size="sm" disabled={busy} onClick={() => void actions.patch(user, { extend_days: 7 }, "usersPage.extended")}>{t("v2.users.extendBy", { count: 7 })}</Btn>
              <Btn size="sm" disabled={busy} onClick={() => void actions.patch(user, { extend_days: 30 }, "usersPage.extended")}>{t("v2.users.extendBy", { count: 30 })}</Btn>
              <Btn size="sm" kind="ghost" disabled={busy} onClick={() => actions.openExtend(user)}>{t("v2.users.extendCustom")}</Btn>
            </div>
          </Panel>
          <Panel title={t("v2.users.roleTitle")}>
            <p className="v3-usr-muted" style={{ margin: "0 0 10px" }}>{t("v2.users.roleSub")}</p>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <RoleChip role={user.role} />
              {canToggleRole && (
                <Btn size="sm" disabled={busy} onClick={() => actions.ask("role", user)}>
                  <ShieldCheck size={13} /> {t(isAdminRole ? "v2.users.action.makeMember" : "v2.users.action.makeAdmin")}
                </Btn>
              )}
            </div>
          </Panel>
        </div>
      </div>

      {(structure.multiProject || (user.groups ?? []).length > 0 || (user.projects ?? []).some((p) => !p.is_default)) && (
        <AccessPanel user={user} access={access.data} loading={access.loading} error={access.error} busy={busy}
          onEdit={() => actions.openMemberships(user)} />
      )}

      <Panel
        title={t(structure.multiProject ? "v3.users.access.globalPermTitle" : "v2.users.permTitle")}
        end={<span>{t(structure.multiProject ? "v3.users.access.globalPermSub" : "v2.users.permSub")}</span>}
      >
        {isAdminRole ? (
          <span className="v3-usr-muted">{t("v2.users.allByRole")}</span>
        ) : isStewardRole ? (
          <span className="v3-usr-muted">{t("v3.steward.typeHint")}</span>
        ) : (
          <div className="v3-usr-chips">
            {AGENT_PERMISSIONS.map((key) => {
              const granted = user.permissions?.[key] !== false;
              return (
                <GrantChip key={key} on={granted} disabled={busy} title={key}
                  onClick={() => void actions.patch(user, { permissions: togglePermission(user.permissions, key, !granted) }, "usersPage.permissionsUpdated")}>
                  {t(`v2.users.perm.${key.replace(".", "_")}`)}
                  <small className="mono">{key}</small>
                </GrantChip>
              );
            })}
          </div>
        )}
      </Panel>

      <Panel
        title={t("v2.users.wsTitle")}
        end={isPlatformRole(user.role) ? undefined : (
          <Btn size="sm" disabled={busy} onClick={() => actions.openWorkspaces(user)}><Pencil size={13} /> {t("v3.users.ws.open")}</Btn>
        )}
      >
        {isAdminRole ? (
          // admins reach every workspace by role; a grant row would suggest revocable access
          <span className="v3-usr-muted">{t("v2.users.allByRole")}</span>
        ) : isStewardRole ? (
          <span className="v3-usr-muted">{t("v3.steward.typeHint")}</span>
        ) : user.workspaces.length === 0 ? (
          <Notice s="wait">{t("v3.users.ws.none")}</Notice>
        ) : (
          <div className="v3-usr-chips" data-testid="v3-user-granted">
            {user.workspaces.map((id) => {
              const ws = workspaces.find((w) => w.id === id);
              return (
                <Chip key={id} s="ok">
                  {ws?.name || id}
                  {ws && ws.name !== id && <small className="mono" style={{ marginLeft: 6 }}>{id}</small>}
                </Chip>
              );
            })}
          </div>
        )}
        {!isPlatformRole(user.role) && <small className="v3-hint" style={{ display: "block", marginTop: 10 }}>{t("v3.users.ws.hint")}</small>}
      </Panel>
      {actions.renderDialogs({ onDeleted: back })}
      {ownPasswordDialog}
    </div>
  );
}

/**
 * Users (administrators and stewards, §11.4): registration statistics and trend, the account table with
 * approvals first, and one account at `?view=detail&user=` — validity, role,
 * agent permissions and workspace grants. Same calls and confirmations as V2;
 * members never fire the admin-only requests.
 *
 * Projects (§7.4–§7.6): the list filters by `?role=`, `?project=`, `?group=`; once
 * the platform has a project besides the default one, rows show their projects
 * (role), can be selected for "add to project…", and create / approve choose
 * projects and groups. The detail adds the account's projects with their sources,
 * its groups and reachable workspaces (`GET /api/users/{id}/access`), and a forced
 * sign-out. A single-project hub keeps the dialogs and columns it had.
 */
export function V3Users() {
  const { isAdmin, role } = useAuth();
  const [params] = useSearchParams();
  // a steward manages accounts too (§11.4) — the server refuses it the platform roles
  if (!isAdmin && !isSteward(role)) return <Forbidden />;
  const username = params.get("user");
  if (params.get("view") === "detail" && username) return <UserDetail key={username} username={username} />;
  return <UserList />;
}
