import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import { type ProfileForm, validateProfile } from "../../../lib/api/networkProfiles";
import { Btn, Dialog, Notice } from "../../ui";

/**
 * Create / edit a network profile: name, VPC, subnets (2-8, two AZs), security groups
 * (1-5), purpose, internet, declared private targets. Changing the network itself
 * (VPC, subnets, SGs, internet) makes the last preflight stale — said in the hint.
 */
export function ProfileDialog({
  initial,
  creating,
  onClose,
  onSubmit,
}: {
  initial: ProfileForm;
  creating: boolean;
  onClose: () => void;
  onSubmit: (form: ProfileForm) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<ProfileForm>(initial);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errors = touched ? validateProfile(form) : {};
  const set = <K extends keyof ProfileForm>(key: K, value: ProfileForm[K]) => {
    setError(null);
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const submit = async () => {
    setTouched(true);
    if (Object.keys(validateProfile(form)).length > 0) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(form);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const field = (key: keyof ProfileForm, label: string, input: ReactNode, hint?: string) => (
    <label className="v3-field">
      <span>{label}</span>
      {input}
      {errors[key] ? <small className="v3-err">{t(errors[key] as string)}</small> : hint ? <small className="v3-hint">{hint}</small> : null}
    </label>
  );

  return (
    <Dialog
      wide
      title={t(creating ? "v3.network.form.createTitle" : "v3.network.form.editTitle")}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy} onClick={() => void submit()}>{t(creating ? "v3.network.form.create" : "v3.network.form.save")}</Btn>
        </>
      }
    >
      <form style={{ display: "grid", gap: 12 }} data-testid="v3-network-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        {field("name", t("v3.network.form.name"),
          <input className="v3-input" value={form.name} aria-invalid={errors.name ? true : undefined} onChange={(e) => set("name", e.target.value)} />)}
        {field("vpc_id", "VPC",
          <input className="v3-input mono" placeholder="vpc-0123456789abcdef0" value={form.vpc_id} aria-invalid={errors.vpc_id ? true : undefined}
            onChange={(e) => set("vpc_id", e.target.value)} />)}
        {field("subnets", t("v3.network.col.subnets"),
          <textarea className="v3-input mono" rows={2} value={form.subnets} aria-invalid={errors.subnets ? true : undefined}
            onChange={(e) => set("subnets", e.target.value)} />, t("v3.network.form.subnetsHint"))}
        {field("security_groups", t("v3.network.form.groups"),
          <textarea className="v3-input mono" rows={2} value={form.security_groups} aria-invalid={errors.security_groups ? true : undefined}
            onChange={(e) => set("security_groups", e.target.value)} />, t("v3.network.form.groupsHint"))}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label className="v3-field">
            <span>{t("v3.network.col.purpose")}</span>
            <select className="v3-select" value={form.purpose} onChange={(e) => set("purpose", e.target.value as ProfileForm["purpose"])}>
              {(["agents", "tools", "both"] as const).map((v) => <option key={v} value={v}>{t(`v3.network.purpose.${v}`)}</option>)}
            </select>
          </label>
          <label className="v3-field">
            <span>{t("v3.network.form.internet")}</span>
            <select className="v3-select" value={form.internet} onChange={(e) => set("internet", e.target.value as ProfileForm["internet"])}>
              {(["none", "nat"] as const).map((v) => <option key={v} value={v}>{t(`v3.network.internet.${v}`)}</option>)}
            </select>
          </label>
        </div>
        {field("reachable", t("v3.network.form.reachable"),
          <textarea className="v3-input mono" rows={3} placeholder="10.20.0.0/16:5432 core db" value={form.reachable}
            aria-invalid={errors.reachable ? true : undefined} onChange={(e) => set("reachable", e.target.value)} />,
          t("v3.network.form.reachableHint"))}
        {!creating && <Notice s="info">{t("v3.network.form.staleHint")}</Notice>}
        {error && <Notice s="act">{error}</Notice>}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Dialog>
  );
}
