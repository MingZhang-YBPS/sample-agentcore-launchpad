import { Copy } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ConsoleUser, errorMessage, type UserPatchBody } from "../../../lib/api";
import { clampDays, DEFAULT_GRANT, MAX_EXTEND_DAYS } from "../../../v2/pages/users/common";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useToast } from "../../hooks";
import { Btn, Confirm, Dialog, Notice } from "../../ui";
import { GrantChip } from "./GrantChip";

/** Account actions that change access, so each one asks first. */
export type ConfirmKind = "delete" | "disable" | "reject" | "reset" | "role";

/**
 * Every account write of the page — the same PATCH / DELETE calls, bodies and
 * confirmations as V2's `useUserActions`, with V3 dialogs: approve with workspace
 * grants, custom extension, the confirmations, and the one-time generated password.
 */
export function useUserActions(onChanged: () => void) {
  const { t } = useTranslation();
  const toast = useToast();
  const { workspaces } = useWorkspace();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: ConfirmKind; user: ConsoleUser } | null>(null);
  const [extendTarget, setExtendTarget] = useState<ConsoleUser | null>(null);
  const [extendDays, setExtendDays] = useState("14");
  const [approveTarget, setApproveTarget] = useState<ConsoleUser | null>(null);
  const [approveGrants, setApproveGrants] = useState<string[]>([]);
  const [reset, setReset] = useState<{ username: string; password: string } | null>(null);

  const patch = async (user: ConsoleUser, body: UserPatchBody, successKey: string) => {
    setBusyId(user.id);
    try {
      const updated = await api.updateUser(user.id, body);
      if (updated.generated_password) setReset({ username: updated.username, password: updated.generated_password });
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

  const renderDialogs = (opts: { onDeleted?: () => void } = {}) => {
    const copy = confirm ? copyFor(confirm.kind, confirm.user) : null;
    return (
      <>
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
            title={t("usersPage.resetTitle")}
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
            <Notice s="wait">{t("usersPage.resetBody", { username: reset.username })}</Notice>
            <pre className="v3-pre" style={{ marginTop: 12, color: "var(--v3-text)", fontSize: 15 }}>{reset.password}</pre>
          </Dialog>
        )}
      </>
    );
  };

  return { busyId, patch, ask, toggleStatus, openExtend, openApprove, renderDialogs };
}
