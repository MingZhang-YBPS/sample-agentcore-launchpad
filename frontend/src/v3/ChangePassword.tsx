import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage } from "../lib/api";
import { useToast } from "./hooks";
import { type PasswordChangeForm, validatePasswordChange } from "./pages/users/forms";
import { Btn, Dialog, Notice } from "./ui";

/**
 * Change the signed-in account's own password (`POST /api/auth/password`): the
 * current one, the new one twice. Works for the built-in admin too — its new
 * password then replaces the configured one (the stack's `ConsolePassword`). Every
 * other session of the account ends; this one stays signed in.
 */
export function ChangePasswordDialog({ username, builtin, onClose }: { username: string; builtin?: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [form, setForm] = useState<PasswordChangeForm>({ current: "", next: "", confirm: "" });
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errors = validatePasswordChange(form);
  const shown = touched ? errors : {};
  const set = (key: keyof PasswordChangeForm) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((prev) => ({ ...prev, [key]: e.target.value }));

  const submit = async () => {
    setTouched(true);
    if (Object.keys(errors).length > 0) return;
    setBusy(true);
    setError(null);
    try {
      await api.changePassword(form.current, form.next);
      toast("ok", t("v3.account.password.changed"));
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const field = (key: keyof PasswordChangeForm, label: string, autoComplete: string) => (
    <label className="v3-field">
      <span>{label}</span>
      <input className="v3-input" type="password" autoComplete={autoComplete} value={form[key]} onChange={set(key)}
        aria-invalid={shown[key] ? true : undefined} data-testid={`password-${key}`} />
      {shown[key] && <small className="v3-err">{t(shown[key] as string)}</small>}
    </label>
  );

  return (
    <Dialog
      title={t("v3.account.password.title")}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy} onClick={() => void submit()}>{t("v3.account.password.submit")}</Btn>
        </>
      }
    >
      <form style={{ display: "grid", gap: 12 }} onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <p style={{ margin: 0 }}>{t("v3.account.password.body", { username })}</p>
        {builtin && <Notice s="wait">{t("v3.account.password.builtinNote")}</Notice>}
        {field("current", t("v3.account.password.current"), "current-password")}
        {field("next", t("v3.account.password.new"), "new-password")}
        {field("confirm", t("v3.account.password.confirm"), "new-password")}
        <small className="v3-hint">{t("v3.account.password.hint")}</small>
        {error && <Notice s="act">{error}</Notice>}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Dialog>
  );
}
