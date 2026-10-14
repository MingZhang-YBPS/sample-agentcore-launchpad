import "../users.css";

import { KeyRound, LogOut, RefreshCw, ShieldAlert, ShieldMinus, ShieldPlus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { api, type ConsoleUser, errorMessage } from "../../../lib/api";
import { type AdminAccount, adminsApi } from "../../../lib/api/projects";
import { usersApi } from "../../../lib/api/users";
import { MAX_EXTEND_DAYS } from "../../../v2/pages/users/common";
import { ChangePasswordDialog } from "../../ChangePassword";
import { ago } from "../../format";
import { useLoad } from "../../hooks";
import { Btn, Chip, Dialog, Empty, Lamp, Notice, PageHead, Panel, type Signal, Skeleton } from "../../ui";
import { useUserActions } from "../users/actions";
import { type CreateForm, oneTimePassword } from "../users/forms";
import { adminCreateBody, demoteBlock, emptyAdminForm, promotable, validateAdminForm } from "./model";

const STATE_SIGNAL: Record<ConsoleUser["state"], Signal> = { pending: "wait", active: "ok", expired: "act", disabled: "off" };

/** Demote one administrator to member / operator; warns when no project row is waiting for them. */
function DemoteDialog({ admin, busy, onCancel, onConfirm }: {
  admin: AdminAccount;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (role: "member" | "operator") => void;
}) {
  const { t } = useTranslation();
  const [role, setRole] = useState<"member" | "operator">("member");
  // the rows kept while they were admin come back into force (§7.2)
  const detail = useLoad(() => usersApi.get(admin.id), `v3-admin-demote:${admin.id}`);
  const projects = detail.data?.projects ?? null;
  return (
    <Dialog
      title={t("v3.admins.demote.title", { username: admin.username })}
      onClose={() => !busy && onCancel()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onCancel}>{t("v3.common.cancel")}</Btn>
          <Btn kind="danger" disabled={busy} onClick={() => onConfirm(role)}>{t("v3.admins.demote.action")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-admin-demote">
        <p style={{ margin: 0 }}>{t("v3.admins.demote.body", { username: admin.username })}</p>
        <label className="v3-field">
          <span>{t("v3.admins.demote.role")}</span>
          <select className="v3-select" value={role} onChange={(e) => setRole(e.target.value as "member" | "operator")}>
            <option value="member">{t("v2.users.role.member")}</option>
            <option value="operator">{t("v2.users.role.operator")}</option>
          </select>
        </label>
        {projects && projects.length === 0 && <Notice s="wait">{t("v3.admins.demote.noProjects")}</Notice>}
        {projects && projects.length > 0 && (
          <small className="v3-hint">{t("v3.admins.demote.restores", { projects: projects.map((p) => p.name).join(", ") })}</small>
        )}
      </div>
    </Dialog>
  );
}

/** "Add administrator": promote an existing account, or create a new admin account. */
function AddAdminDialog({ reservedName, onClose, onDone }: {
  reservedName: string | null;
  onClose: () => void;
  onDone: (created?: ConsoleUser) => void;
}) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<"promote" | "create">("promote");
  const [q, setQ] = useState("");
  const [pick, setPick] = useState<ConsoleUser | null>(null);
  const [form, setForm] = useState<CreateForm>(emptyAdminForm);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const search = useLoad(
    async () => (mode === "promote" && q.trim() ? promotable((await usersApi.list({ q: q.trim(), limit: 20 })).items) : []),
    `v3-admin-promote:${mode}:${q.trim()}`,
  );
  const errors = touched && mode === "create" ? validateAdminForm(form, reservedName) : {};
  const set = <K extends keyof CreateForm>(key: K, value: CreateForm[K]) => {
    setError(null);
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const submit = async () => {
    setError(null);
    if (mode === "promote") {
      if (!pick) return;
      setBusy(true);
      try {
        await usersApi.update(pick.id, { role: "admin" });
        onDone();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
      return;
    }
    setTouched(true);
    if (Object.keys(validateAdminForm(form, reservedName)).length > 0) return;
    setBusy(true);
    try {
      onDone(await api.createUser(adminCreateBody(form)));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const radio = (value: "promote" | "create") => (
    <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <input type="radio" name="add-admin-mode" checked={mode === value} onChange={() => { setMode(value); setError(null); }} />
      {t(`v3.admins.add.${value}`)}
    </label>
  );

  return (
    <Dialog
      wide
      title={t("v3.admins.add.title")}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy || (mode === "promote" && !pick)} onClick={() => void submit()}>{t("v3.admins.add.submit")}</Btn>
        </>
      }
    >
      <form style={{ display: "grid", gap: 12 }} data-testid="v3-admin-add" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>{radio("promote")}{radio("create")}</div>
        {mode === "promote" ? (
          <div className="v3-field">
            <span>{t("v3.admins.add.search")}</span>
            <input className="v3-input" value={q} onChange={(e) => { setQ(e.target.value); setPick(null); }}
              placeholder={t("v2.users.search")} aria-label={t("v3.admins.add.search")} />
            {q.trim() && search.loading && <Skeleton rows={2} />}
            {q.trim() && !search.loading && (search.data ?? []).length === 0 && <small className="v3-hint">{t("v3.admins.add.noMatch")}</small>}
            <div className="v3-usr-chips">
              {(search.data ?? []).map((u) => (
                <button key={u.id} type="button" className={pick?.id === u.id ? "v3-usr-chip on" : "v3-usr-chip"} aria-pressed={pick?.id === u.id}
                  onClick={() => setPick(u)}>
                  {u.username}
                  <small>{t(`v2.users.role.${u.role}`)} · {t("v3.admins.add.projectCount", { count: (u.projects ?? []).length })}</small>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
              <label className="v3-field">
                <span>{t("v2.users.field.username")}</span>
                <input className="v3-input" name="username" autoComplete="off" value={form.username} onChange={(e) => set("username", e.target.value)}
                  aria-invalid={errors.username ? true : undefined} />
                {errors.username ? <small className="v3-err">{t(errors.username)}</small> : <small className="v3-hint">{t("v3.users.create.usernameHint")}</small>}
              </label>
              <label className="v3-field">
                <span>{t("v2.users.field.email")}</span>
                <input className="v3-input" name="email" type="email" autoComplete="off" value={form.email} onChange={(e) => set("email", e.target.value)}
                  aria-invalid={errors.email ? true : undefined} />
                {errors.email && <small className="v3-err">{t(errors.email)}</small>}
              </label>
            </div>
            <div className="v3-field">
              <span>{t("v3.users.create.password")}</span>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
                <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                  <input type="radio" name="admin-password" checked={form.passwordChoice === "generate"} onChange={() => set("passwordChoice", "generate")} />
                  {t("v3.users.create.passwordGenerate")}
                </label>
                <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                  <input type="radio" name="admin-password" checked={form.passwordChoice === "set"} onChange={() => set("passwordChoice", "set")} />
                  {t("v3.users.create.passwordSet")}
                </label>
              </div>
              {form.passwordChoice === "set" && (
                <input className="v3-input" type="password" autoComplete="new-password" value={form.password}
                  onChange={(e) => set("password", e.target.value)} aria-label={t("v3.users.create.password")} aria-invalid={errors.password ? true : undefined} />
              )}
              {errors.password ? <small className="v3-err">{t(errors.password)}</small> : <small className="v3-hint">{t("v3.users.create.passwordHint")}</small>}
            </div>
            <label className="v3-field">
              <span>{t("v2.users.col.validity")}</span>
              <select className="v3-select" value={form.expiry} onChange={(e) => set("expiry", e.target.value as CreateForm["expiry"])}>
                <option value="never">{t("usersPage.neverExpires")}</option>
                <option value="days">{t("v3.users.create.expiryDays")}</option>
              </select>
              {form.expiry === "days" && (
                <input className="v3-input" type="number" min={1} max={MAX_EXTEND_DAYS} value={form.days}
                  onChange={(e) => set("days", e.target.value)} aria-label={t("v2.users.extendDays")} />
              )}
              {errors.days && <small className="v3-err">{t(errors.days, { max: MAX_EXTEND_DAYS })}</small>}
            </label>
          </>
        )}
        <Notice s="wait">{t("v3.admins.add.warning")}</Notice>
        {error && <Notice s="act">{error}</Notice>}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Dialog>
  );
}

function Forbidden() {
  const { t } = useTranslation();
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v3.admins.eyebrow")} title={t("v3.admins.title")} sub={t("auth.adminRequired.meta")} />
      <Panel>
        <Empty title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ShieldAlert size={16} /> {t("auth.adminRequired.title")}</span>}>
          {t("usersPage.forbiddenBody")}
        </Empty>
      </Panel>
    </div>
  );
}

function AdminList() {
  const { t } = useTranslation();
  const auth = useAuth();
  const list = useLoad(() => adminsApi.list(), "v3-admins");
  const actions = useUserActions(list.reload);
  const [demoting, setDemoting] = useState<AdminAccount | null>(null);
  const [adding, setAdding] = useState(false);
  const [changingOwn, setChangingOwn] = useState(false);
  const items = list.data?.items ?? [];
  const builtin = items.find((a) => a.builtin) ?? null;
  const isSelf = (a: AdminAccount) => a.username === auth.username && (auth.builtin ? a.builtin : !a.builtin);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.admins.eyebrow")}
        title={t("v3.admins.title")}
        sub={t("v3.admins.sub")}
        end={
          <>
            <Btn kind="ghost" onClick={list.reload} disabled={list.loading}><RefreshCw size={14} /> {t("v3.users.refresh")}</Btn>
            <Btn kind="primary" onClick={() => setAdding(true)}><ShieldPlus size={14} /> {t("v3.admins.add.open")}</Btn>
          </>
        }
      />
      {list.data && list.data.can_sign_in <= 1 && <Notice s="wait">{t("v3.admins.lastNotice")}</Notice>}

      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{t("usersPage.loadFailed", { msg: list.error })}</Notice></div>
        ) : items.length === 0 ? (
          <Empty title={t("v3.admins.empty")} />
        ) : (
          <table className="v3-table" data-testid="v3-admins-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.users.col.user")}</th>
                <th>{t("v3.admins.col.type")}</th>
                <th>{t("v2.users.col.state")}</th>
                <th className="num">{t("v2.users.col.lastLogin")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((a) => {
                const busy = actions.busyId === a.id;
                const block = demoteBlock(a);
                const self = isSelf(a);
                return (
                  <tr key={a.id}>
                    <td style={{ width: 30 }}><Lamp s={STATE_SIGNAL[a.state]} /></td>
                    <td>
                      <div className="v3-name"><div><b>{a.username}</b><small>{a.email || "—"}</small></div></div>
                      {a.builtin && (
                        <small className="v3-usr-muted">
                          {t("v3.admins.passwordSource", {
                            source: t(`v3.users.builtin.source.${a.password_source ?? "config"}`),
                            when: a.password_changed_at ? a.password_changed_at.slice(0, 10) : "—",
                          })}
                        </small>
                      )}
                    </td>
                    <td><Chip s={a.builtin ? undefined : "info"}>{t(a.builtin ? "v3.users.builtin.chip" : "v3.admins.type.account")}</Chip></td>
                    <td><Chip s={STATE_SIGNAL[a.state] === "off" ? undefined : STATE_SIGNAL[a.state]}>{t(`v2.users.state.${a.state}`)}</Chip></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{a.builtin ? "—" : ago(a.last_login_at)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        {self ? (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setChangingOwn(true)}><KeyRound size={13} /> {t("v3.account.password.title")}</Btn>
                        ) : (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => actions.ask("reset", a)}><KeyRound size={13} /> {t("v2.users.action.resetPassword")}</Btn>
                        )}
                        {!a.builtin && a.state === "expired" && (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => actions.openExtend(a)}>{t("v2.users.action.extend")}</Btn>
                        )}
                        {!a.builtin && !self && (
                          <Btn size="sm" kind="ghost" disabled={busy} title={t("v3.users.sessions.hint")} onClick={() => actions.ask("sessions", a)}>
                            <LogOut size={13} /> {t("v3.users.sessions.action")}
                          </Btn>
                        )}
                        {!a.builtin && (
                          <Btn size="sm" kind="ghost" disabled={busy || block !== null} title={block ? t(`v3.admins.block.${block}`) : undefined}
                            onClick={() => setDemoting(a)}>
                            <ShieldMinus size={13} /> {t("v3.admins.demote.action")}
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
      </Panel>
      <small className="v3-hint">{t("v3.admins.breakGlass")}</small>

      {demoting && (
        <DemoteDialog admin={demoting} busy={actions.busyId !== null} onCancel={() => setDemoting(null)}
          onConfirm={(role) => {
            const target = demoting;
            // the console's role type predates `operator`; the backend takes all three
            void actions.patch(target, { role: role as ConsoleUser["role"] }, "v3.admins.demote.done").then((ok) => ok && setDemoting(null));
          }} />
      )}
      {adding && (
        <AddAdminDialog reservedName={builtin?.username ?? null} onClose={() => setAdding(false)}
          onDone={(created) => {
            setAdding(false);
            if (created) {
              const once = oneTimePassword(created, "created");
              if (once) actions.showOneTime(once);
            }
            list.reload();
          }} />
      )}
      {changingOwn && auth.username && (
        <ChangePasswordDialog username={auth.username} builtin={auth.builtin} onClose={() => { setChangingOwn(false); list.reload(); }} />
      )}
      {actions.renderDialogs()}
    </div>
  );
}

/**
 * Administrators (projects §7.3, platform administrators only): the built-in admin
 * first, then every admin-role account (`GET /api/admins`); add one by promoting an
 * account or creating an admin account; demote with the server's guards (never the
 * built-in one, yourself or the last admin who can sign in); reset passwords and
 * force a sign-out. Hub-global: no workspace or project in any of these calls.
 */
export function V3Admins() {
  const { isAdmin } = useAuth();
  if (!isAdmin) return <Forbidden />;
  return <AdminList />;
}
