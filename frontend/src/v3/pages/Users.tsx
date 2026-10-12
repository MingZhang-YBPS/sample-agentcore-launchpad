import "./users.css";

import { ArrowLeft, KeyRound, RefreshCw, Search, ShieldAlert, ShieldCheck, Trash2, UserCheck, UserX } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { AGENT_PERMISSIONS, togglePermission, api, type ConsoleUser, type UserState } from "../../lib/api";
import { isStatusFilter, PAGE_SIZE, USER_STATES } from "../../v2/pages/users/common";
import { useWorkspace } from "../../workspace/workspace-context";
import { ago } from "../format";
import { useLoad } from "../hooks";
import { Btn, Chip, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { useUserActions } from "./users/actions";
import { GrantChip } from "./users/GrantChip";

const STATE_SIGNAL: Record<UserState, Signal> = { pending: "wait", active: "ok", expired: "act", disabled: "off" };

function StateChip({ state }: { state: UserState }) {
  const { t } = useTranslation();
  const s = STATE_SIGNAL[state];
  return <Chip s={s === "off" ? undefined : s}>{t(`v2.users.state.${state}`)}</Chip>;
}

function RoleChip({ role }: { role: ConsoleUser["role"] }) {
  const { t } = useTranslation();
  return <Chip s={role === "admin" ? "info" : undefined}>{t(`v2.users.role.${role}`)}</Chip>;
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
  const statusParam = params.get("status");
  const status = isStatusFilter(statusParam) ? statusParam : "all";
  const query = params.get("q") ?? "";
  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);
  const list = useLoad(
    () => api.listUsers({ q: query || undefined, status, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE }),
    `v3-users:${status}:${query}:${page}`,
  );
  const stats = useLoad(() => api.userStats(), "v3-users-stats");
  const reload = () => {
    list.reload();
    stats.reload();
  };
  const actions = useUserActions(reload);
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
  const rows = useMemo(() => list.data?.items ?? [], [list.data]);
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
        end={<Btn kind="ghost" onClick={reload} disabled={list.loading}><RefreshCw size={14} /> {t("v3.users.refresh")}</Btn>}
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
                <th />
                <th>{t("v2.users.col.user")}</th>
                <th>{t("v2.users.col.role")}</th>
                <th>{t("v2.users.col.state")}</th>
                <th>{t("v2.users.col.workspaces")}</th>
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
                    <td style={{ width: 30 }}><Lamp s={STATE_SIGNAL[u.state]} /></td>
                    <td><div className="v3-name"><div><b>{u.username}</b><small>{u.email}</small></div></div></td>
                    <td><RoleChip role={u.role} /></td>
                    <td><StateChip state={u.state} /></td>
                    <td>
                      {u.role === "admin" ? (
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
                        {u.state === "pending" ? (
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
                        <Btn size="sm" kind="ghost" disabled={busy} onClick={() => actions.ask("delete", u)} title={t("v2.users.action.delete")}>
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
        {total > PAGE_SIZE && (
          <div className="v3-usr-pager">
            <Btn size="sm" kind="ghost" disabled={page <= 1} onClick={() => setParam("page", String(page - 1))}>←</Btn>
            <span className="mono">{Math.min(page, pages)} / {pages}</span>
            <Btn size="sm" kind="ghost" disabled={page >= pages} onClick={() => setParam("page", String(page + 1))}>→</Btn>
          </div>
        )}
      </Panel>
      {actions.renderDialogs()}
    </div>
  );
}

/* ── one account ─────────────────────────────────────────────────────────── */

function UserDetail({ username }: { username: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const { workspaces } = useWorkspace();
  // there is no GET-by-id route: read it back through the list search, matched exactly
  const loaded = useLoad(async () => {
    const page = await api.listUsers({ q: username, limit: 200 });
    return page.items.find((u) => u.username === username) ?? null;
  }, `v3-user:${username}`);
  const actions = useUserActions(loaded.reload);
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
            <Btn disabled={busy} onClick={() => actions.ask("reset", user)}><KeyRound size={14} /> {t("v2.users.action.resetPassword")}</Btn>
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
              <Btn size="sm" disabled={busy} onClick={() => actions.ask("role", user)}>
                <ShieldCheck size={13} /> {t(isAdminRole ? "v2.users.action.makeMember" : "v2.users.action.makeAdmin")}
              </Btn>
            </div>
          </Panel>
        </div>
      </div>

      <Panel title={t("v2.users.permTitle")} end={<span>{t("v2.users.permSub")}</span>}>
        {isAdminRole ? (
          <span className="v3-usr-muted">{t("v2.users.allByRole")}</span>
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

      <Panel title={t("v2.users.wsTitle")} end={<span>{t("v2.users.wsSub")}</span>}>
        {isAdminRole ? (
          // admins reach every workspace by role; a grant row would suggest revocable access
          <span className="v3-usr-muted">{t("v2.users.allByRole")}</span>
        ) : workspaces.length === 0 ? (
          <span className="v3-usr-muted">—</span>
        ) : (
          <div className="v3-usr-chips">
            {workspaces.map((ws) => {
              const granted = user.workspaces.includes(ws.id);
              return (
                <GrantChip key={ws.id} on={granted} disabled={busy} title={`${ws.account_id} · ${ws.region}`}
                  onClick={() => void actions.patch(user, {
                    workspaces: granted ? user.workspaces.filter((id) => id !== ws.id) : [...user.workspaces, ws.id],
                  }, "usersPage.workspacesUpdated")}>
                  {ws.name || ws.id}
                  <small className="mono">{ws.id}</small>
                </GrantChip>
              );
            })}
          </div>
        )}
      </Panel>
      {actions.renderDialogs({ onDeleted: back })}
    </div>
  );
}

/**
 * Users (admin-only): registration statistics and trend, the account table with
 * approvals first, and one account at `?view=detail&user=` — validity, role,
 * agent permissions and workspace grants. Same calls and confirmations as V2;
 * members never fire the admin-only requests.
 */
export function V3Users() {
  const { isAdmin } = useAuth();
  const [params] = useSearchParams();
  if (!isAdmin) return <Forbidden />;
  const username = params.get("user");
  if (params.get("view") === "detail" && username) return <UserDetail key={username} username={username} />;
  return <UserList />;
}
