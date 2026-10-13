import { Copy } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ConsoleRole, type ConsoleUser, errorMessage, type UserPatchBody } from "../../../lib/api";
import { clampDays, DEFAULT_GRANT, MAX_EXTEND_DAYS } from "../../../v2/pages/users/common";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useToast } from "../../hooks";
import { Btn, Confirm, Dialog, Notice } from "../../ui";
import { createBody, type CreateErrors, type CreateForm, emptyCreateForm, oneTimePassword, validateCreate } from "./forms";
import { GrantChip } from "./GrantChip";

/** Account actions that change access, so each one asks first. */
export type ConfirmKind = "delete" | "disable" | "reject" | "reset" | "role";

/**
 * Every account write of the page — the same PATCH / DELETE calls, bodies and
 * confirmations as V2's `useUserActions`, with V3 dialogs: approve with workspace
 * grants, custom extension, the confirmations, and the one-time generated password.
 */
export function useUserActions(onChanged: () => void, validDays = 7) {
  const { t } = useTranslation();
  const toast = useToast();
  const { workspaces } = useWorkspace();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: ConfirmKind; user: ConsoleUser } | null>(null);
  const [extendTarget, setExtendTarget] = useState<ConsoleUser | null>(null);
  const [extendDays, setExtendDays] = useState("14");
  const [approveTarget, setApproveTarget] = useState<ConsoleUser | null>(null);
  const [approveGrants, setApproveGrants] = useState<string[]>([]);
  const [reset, setReset] = useState<{ username: string; password: string; kind: "created" | "reset" } | null>(null);
  const [creating, setCreating] = useState<CreateForm | null>(null);
  const [createTouched, setCreateTouched] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [wsTarget, setWsTarget] = useState<ConsoleUser | null>(null);
  const [wsDraft, setWsDraft] = useState<string[]>([]);

  const patch = async (user: ConsoleUser, body: UserPatchBody, successKey: string) => {
    setBusyId(user.id);
    try {
      const updated = await api.updateUser(user.id, body);
      const once = oneTimePassword(updated, "reset");
      if (once) setReset(once);
      toast("ok", t(successKey, { username: user.username }));
      onChanged();
      return true;
    } catch (err) {
      toast("act", t("usersPage.actionFailed", { msg: errorMessage(err) }));
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (user: ConsoleUser) => {
    setBusyId(user.id);
    try {
      await api.deleteUser(user.id);
      toast("ok", t("usersPage.deleted", { username: user.username }));
      onChanged();
      return true;
    } catch (err) {
      toast("act", t("usersPage.actionFailed", { msg: errorMessage(err) }));
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const toggleStatus = (user: ConsoleUser) =>
    user.status === "active" ? setConfirm({ kind: "disable", user }) : void patch(user, { status: "active" }, "usersPage.enabled");

  const openExtend = (user: ConsoleUser) => {
    setExtendDays("14");
    setExtendTarget(user);
  };

  // approval decides the account's environments too, so it asks instead of
  // granting the hub silently
  const openApprove = (user: ConsoleUser) => {
    setApproveGrants(user.workspaces.length > 0 ? user.workspaces : [DEFAULT_GRANT]);
    setApproveTarget(user);
  };

  const ask = (kind: ConfirmKind, user: ConsoleUser) => setConfirm({ kind, user });

  const openCreate = () => {
    setCreating(emptyCreateForm());
    setCreateTouched(false);
    setCreateError(null);
  };

  const submitCreate = async (reservedName: string | null) => {
    if (!creating) return;
    setCreateTouched(true);
    if (Object.keys(validateCreate(creating, reservedName)).length > 0) return;
    setBusyId("create");
    setCreateError(null);
    try {
      const created = await api.createUser(createBody(creating));
      setCreating(null);
      toast("ok", t("v3.users.create.created", { username: created.username }));
      setReset(oneTimePassword(created, "created"));
      onChanged();
    } catch (err) {
      // the dialog stays open with what was typed, so a taken name is a quick fix
      setCreateError(errorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const openWorkspaces = (user: ConsoleUser) => {
    setWsDraft(user.workspaces);
    setWsTarget(user);
  };

  const runConfirm = async (onDeleted?: () => void) => {
    if (!confirm) return;
    const { kind, user } = confirm;
    let ok: boolean;
    if (kind === "delete") {
      ok = await remove(user);
      if (ok) onDeleted?.();
    } else if (kind === "disable") ok = await patch(user, { status: "disabled" }, "usersPage.disabled");
    else if (kind === "reject") ok = await patch(user, { status: "disabled" }, "usersPage.rejected");
    else if (kind === "reset") ok = await patch(user, { password: null }, "usersPage.passwordReset");
    else ok = await patch(user, { role: user.role === "admin" ? "member" : "admin" }, "v2.users.roleUpdated");
    if (ok) setConfirm(null);
  };

  const copyFor = (kind: ConfirmKind, user: ConsoleUser) => {
    const username = user.username;
    switch (kind) {
      case "delete":
        return { title: t("usersPage.deleteTitle"), body: t("usersPage.deleteBody", { username }), label: t("v2.users.action.delete"), danger: true };
      case "disable":
        return { title: t("v2.users.confirm.disableTitle"), body: t("v2.users.confirm.disableBody", { username }), label: t("v2.users.action.disable"), danger: true };
      case "reject":
        return { title: t("v2.users.confirm.rejectTitle"), body: t("v2.users.confirm.rejectBody", { username }), label: t("v2.users.action.reject"), danger: true };
      case "reset":
        return { title: t("v2.users.confirm.resetTitle"), body: t("v2.users.confirm.resetBody", { username }), label: t("v2.users.action.resetPassword"), danger: false };
      default:
        return user.role === "admin"
          ? { title: t("v2.users.confirm.demoteTitle"), body: t("v2.users.confirm.demoteBody", { username }), label: t("v2.users.action.makeMember"), danger: true }
          : { title: t("v2.users.confirm.promoteTitle"), body: t("v2.users.confirm.promoteBody", { username }), label: t("v2.users.action.makeAdmin"), danger: false };
    }
  };

  const renderDialogs = (opts: { onDeleted?: () => void; reservedName?: string | null } = {}) => {
    const copy = confirm ? copyFor(confirm.kind, confirm.user) : null;
    const createErrors: CreateErrors = creating && createTouched ? validateCreate(creating, opts.reservedName ?? null) : {};
    const setField = <K extends keyof CreateForm>(key: K, value: CreateForm[K]) => {
      setCreateError(null);
      setCreating((prev) => (prev ? { ...prev, [key]: value } : prev));
    };
    return (
      <>
        {creating && (
          <Dialog
            wide
            title={t("v3.users.create.title")}
            onClose={() => busyId === null && setCreating(null)}
            foot={
              <>
                <Btn kind="ghost" disabled={busyId !== null} onClick={() => setCreating(null)}>{t("v3.common.cancel")}</Btn>
                <Btn kind="primary" disabled={busyId !== null} onClick={() => void submitCreate(opts.reservedName ?? null)}>{t("v3.users.create.submit")}</Btn>
              </>
            }
          >
            <form style={{ display: "grid", gap: 12 }} data-testid="v3-user-create" onSubmit={(e) => { e.preventDefault(); void submitCreate(opts.reservedName ?? null); }}>
              <p style={{ margin: 0 }}>{t("v3.users.create.body")}</p>
              <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
                <label className="v3-field">
                  <span>{t("v2.users.field.username")}</span>
                  <input className="v3-input" name="username" autoComplete="off" value={creating.username} onChange={(e) => setField("username", e.target.value)}
                    aria-invalid={createErrors.username ? true : undefined} />
                  {createErrors.username ? <small className="v3-err">{t(createErrors.username)}</small> : <small className="v3-hint">{t("v3.users.create.usernameHint")}</small>}
                </label>
                <label className="v3-field">
                  <span>{t("v2.users.field.email")}</span>
                  <input className="v3-input" name="email" type="email" autoComplete="off" value={creating.email} onChange={(e) => setField("email", e.target.value)}
                    aria-invalid={createErrors.email ? true : undefined} />
                  {createErrors.email ? <small className="v3-err">{t(createErrors.email)}</small> : <small className="v3-hint">{t("v3.users.create.emailHint")}</small>}
                </label>
              </div>
              <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
                <label className="v3-field">
                  <span>{t("v2.users.col.role")}</span>
                  <select className="v3-select" name="role" value={creating.role} onChange={(e) => setField("role", e.target.value as ConsoleRole)}>
                    {(["member", "operator", "admin"] as ConsoleRole[]).map((role) => (
                      <option key={role} value={role}>{t(`v2.users.role.${role}`)}</option>
                    ))}
                  </select>
                  <small className="v3-hint">{t(`v3.users.create.roleHint.${creating.role}`)}</small>
                </label>
                <label className="v3-field">
                  <span>{t("v2.users.col.validity")}</span>
                  <select className="v3-select" name="expiry" value={creating.expiry} onChange={(e) => setField("expiry", e.target.value as CreateForm["expiry"])}>
                    <option value="default">{t("v3.users.create.expiryDefault", { count: validDays })}</option>
                    <option value="days">{t("v3.users.create.expiryDays")}</option>
                    <option value="never">{t("usersPage.neverExpires")}</option>
                  </select>
                  {creating.expiry === "days" && (
                    <input className="v3-input" name="days" type="number" min={1} max={MAX_EXTEND_DAYS} value={creating.days}
                      onChange={(e) => setField("days", e.target.value)} aria-label={t("v2.users.extendDays")} aria-invalid={createErrors.days ? true : undefined} />
                  )}
                  {createErrors.days && <small className="v3-err">{t(createErrors.days, { max: MAX_EXTEND_DAYS })}</small>}
                </label>
              </div>
              <div className="v3-field">
                <span>{t("v3.users.create.password")}</span>
                <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
                  <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                    <input type="radio" name="password-choice" checked={creating.passwordChoice === "generate"} onChange={() => setField("passwordChoice", "generate")} />
                    {t("v3.users.create.passwordGenerate")}
                  </label>
                  <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                    <input type="radio" name="password-choice" checked={creating.passwordChoice === "set"} onChange={() => setField("passwordChoice", "set")} />
                    {t("v3.users.create.passwordSet")}
                  </label>
                </div>
                {creating.passwordChoice === "set" && (
                  <input className="v3-input" name="password" type="password" autoComplete="new-password" value={creating.password}
                    onChange={(e) => setField("password", e.target.value)} aria-label={t("v3.users.create.password")} aria-invalid={createErrors.password ? true : undefined} />
                )}
                {createErrors.password ? <small className="v3-err">{t(createErrors.password)}</small> : <small className="v3-hint">{t("v3.users.create.passwordHint")}</small>}
              </div>
              <div className="v3-field">
                <span>{t("v2.users.col.workspaces")}</span>
                {creating.role === "admin" ? (
                  <small className="v3-hint">{t("v2.users.allByRole")}</small>
                ) : (
                  <>
                    <div className="v3-usr-chips">
                      {workspaces.map((ws) => {
                        const on = creating.workspaces.includes(ws.id);
                        return (
                          <GrantChip key={ws.id} on={on} title={ws.name}
                            onClick={() => setField("workspaces", on ? creating.workspaces.filter((id) => id !== ws.id) : [...creating.workspaces, ws.id])}>
                            {ws.id}
                          </GrantChip>
                        );
                      })}
                    </div>
                    {creating.workspaces.length === 0 && <Notice s="wait">{t("v3.users.create.noGrant")}</Notice>}
                  </>
                )}
              </div>
              {createError && <Notice s="act">{createError}</Notice>}
              <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
            </form>
          </Dialog>
        )}

        {wsTarget && (
          <Dialog
            wide
            title={t("v3.users.ws.title")}
            onClose={() => busyId === null && setWsTarget(null)}
            foot={
              <>
                <Btn kind="ghost" disabled={busyId !== null} onClick={() => setWsTarget(null)}>{t("v3.common.cancel")}</Btn>
                <Btn kind="primary" disabled={busyId !== null}
                  onClick={() => {
                    const target = wsTarget;
                    void patch(target, { workspaces: wsDraft }, "usersPage.workspacesUpdated").then((ok) => ok && setWsTarget(null));
                  }}>
                  {t("v3.users.ws.save")}
                </Btn>
              </>
            }
          >
            <p style={{ marginTop: 0 }}>{t("v3.users.ws.body", { username: wsTarget.username })}</p>
            <div className="v3-usr-chips" data-testid="v3-user-workspaces">
              {workspaces.map((ws) => {
                const on = wsDraft.includes(ws.id);
                return (
                  <GrantChip key={ws.id} on={on} title={`${ws.account_id} · ${ws.region}`}
                    onClick={() => setWsDraft((prev) => (on ? prev.filter((id) => id !== ws.id) : [...prev, ws.id]))}>
                    {ws.name || ws.id}
                    <small className="mono">{ws.id}</small>
                  </GrantChip>
                );
              })}
            </div>
            <small className="v3-hint" style={{ display: "block", marginTop: 10 }}>{t("v3.users.ws.hint")}</small>
            {wsDraft.length === 0 && <div style={{ marginTop: 12 }}><Notice s="wait">{t("v3.users.ws.noGrant")}</Notice></div>}
          </Dialog>
        )}

        {copy && (
          <Confirm title={copy.title} confirmLabel={copy.label} cancelLabel={t("v3.common.cancel")} danger={copy.danger}
            busy={busyId !== null} onCancel={() => setConfirm(null)} onConfirm={() => void runConfirm(opts.onDeleted)}>
            {copy.body}
          </Confirm>
        )}

        {extendTarget && (
          <Dialog
            title={t("usersPage.extendTitle")}
            onClose={() => setExtendTarget(null)}
            foot={
              <>
                <Btn kind="ghost" onClick={() => setExtendTarget(null)}>{t("v3.common.cancel")}</Btn>
                <Btn kind="primary" disabled={busyId !== null}
                  onClick={() => {
                    const target = extendTarget;
                    setExtendTarget(null);
                    void patch(target, { extend_days: clampDays(extendDays) }, "usersPage.extended");
                  }}>
                  {t("v2.users.action.extend")}
                </Btn>
              </>
            }
          >
            <p style={{ marginTop: 0 }}>{t("usersPage.extendBody", { username: extendTarget.username })}</p>
            <label className="v3-field">
              <span>{t("v2.users.extendDays")}</span>
              <input className="v3-input" type="number" min={1} max={MAX_EXTEND_DAYS} value={extendDays}
                onChange={(e) => setExtendDays(e.target.value)} />
              <small className="v3-hint">{t("v2.users.extendHint", { max: MAX_EXTEND_DAYS })}</small>
            </label>
          </Dialog>
        )}

        {approveTarget && (
          <Dialog
            wide
            title={t("usersPage.approveTitle")}
            onClose={() => setApproveTarget(null)}
            foot={
              <>
                <Btn kind="ghost" onClick={() => setApproveTarget(null)}>{t("v3.common.cancel")}</Btn>
                <Btn kind="primary" disabled={busyId !== null}
                  onClick={() => {
                    const target = approveTarget;
                    setApproveTarget(null);
                    void patch(target, { status: "active", workspaces: approveGrants }, "usersPage.approved");
                  }}>
                  {t("v2.users.action.approve")}
                </Btn>
              </>
            }
          >
            <p style={{ marginTop: 0 }}>{t("usersPage.approveBody", { username: approveTarget.username })}</p>
            <div className="v3-field">
              <span>{t("v2.users.col.workspaces")}</span>
              <div className="v3-usr-chips">
                {workspaces.map((ws) => {
                  const on = approveGrants.includes(ws.id);
                  return (
                    <GrantChip key={ws.id} on={on} title={ws.name}
                      onClick={() => setApproveGrants((prev) => (on ? prev.filter((id) => id !== ws.id) : [...prev, ws.id]))}>
                      {ws.id}
                    </GrantChip>
                  );
                })}
                {workspaces.length === 0 && <span className="v3-hint">—</span>}
              </div>
              <small className="v3-hint">{t("v2.users.approveHint")}</small>
            </div>
            {approveGrants.length === 0 && <div style={{ marginTop: 12 }}><Notice s="wait">{t("v2.users.approveNoGrant")}</Notice></div>}
          </Dialog>
        )}

        {reset && (
          <Dialog
            title={t(reset.kind === "created" ? "v3.users.create.passwordTitle" : "usersPage.resetTitle")}
            onClose={() => setReset(null)}
            foot={
              <>
                <Btn onClick={() => {
                  void navigator.clipboard?.writeText(reset.password);
                  toast("ok", t("usersPage.copied"));
                }}>
                  <Copy size={14} /> {t("v2.users.copy")}
                </Btn>
                <Btn kind="primary" onClick={() => setReset(null)}>{t("v3.users.close")}</Btn>
              </>
            }
          >
            <Notice s="wait">{t(reset.kind === "created" ? "v3.users.create.passwordBody" : "usersPage.resetBody", { username: reset.username })}</Notice>
            <pre className="v3-pre" data-testid="v3-one-time-password" style={{ marginTop: 12, color: "var(--v3-text)", fontSize: 15 }}>{reset.password}</pre>
          </Dialog>
        )}
      </>
    );
  };

  return { busyId, patch, ask, toggleStatus, openExtend, openApprove, openCreate, openWorkspaces, renderDialogs };
}
