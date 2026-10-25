import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import type { NetworkProfile } from "../../../lib/api/networkProfiles";
import {
  STORAGE_DATA_CLASSES,
  STORAGE_MODES,
  type StorageForm,
  validateStorage,
} from "../../../lib/api/storage";
import { Reasoned } from "../../reason";
import { Btn, Dialog, Notice } from "../../ui";
import { Select } from "../../select";

/**
 * Register / edit a storage entry (storage-mounts §4.4): only the access point ARN and
 * an id are needed — the check fills in the rest. The id and the access point are fixed
 * once registered (agents name the id; a stamped reference keeps the ARN).
 */
export function RegisterDialog({
  initial,
  creating,
  profiles,
  onClose,
  onSubmit,
}: {
  initial: StorageForm;
  creating: boolean;
  profiles: NetworkProfile[];
  onClose: () => void;
  onSubmit: (form: StorageForm) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<StorageForm>(initial);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errors = touched ? validateStorage(form, creating) : {};
  const set = <K extends keyof StorageForm>(key: K, value: StorageForm[K]) => {
    setError(null);
    setForm((prev) => ({ ...prev, [key]: value }));
  };
  const submit = async () => {
    setTouched(true);
    if (Object.keys(validateStorage(form, creating)).length > 0) return;
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
  const field = (key: keyof StorageForm, label: string, input: ReactNode, hint?: string) => (
    <label className="v3-field">
      <span>{label}</span>
      {input}
      {errors[key] ? <small className="v3-err">{t(errors[key] as string)}</small> : hint ? <small className="v3-hint">{hint}</small> : null}
    </label>
  );

  return (
    <Dialog
      wide
      title={t(creating ? "v3.storage.form.createTitle" : "v3.storage.form.editTitle")}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy} onClick={() => void submit()}>{t(creating ? "v3.storage.form.create" : "v3.storage.form.save")}</Btn>
        </>
      }
    >
      <form style={{ display: "grid", gap: 12 }} data-testid="v3-storage-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        {field("id", t("v3.storage.form.id"),
          <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
            <input className="v3-input mono" placeholder="datasets" value={form.id} disabled={!creating}
              aria-invalid={errors.id ? true : undefined} onChange={(e) => set("id", e.target.value)} />
          </Reasoned>,
          t("v3.storage.form.idHint"))}
        {field("name", t("v3.storage.form.name"),
          <input className="v3-input" value={form.name} aria-invalid={errors.name ? true : undefined} onChange={(e) => set("name", e.target.value)} />)}
        {field("access_point_arn", t("v3.storage.form.arn"),
          <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
            <input className="v3-input mono" disabled={!creating} value={form.access_point_arn}
              placeholder="arn:aws:s3files:us-west-2:111122223333:file-system/fs-…/access-point/fsap-…"
              aria-invalid={errors.access_point_arn ? true : undefined} onChange={(e) => set("access_point_arn", e.target.value)} />
          </Reasoned>,
          t("v3.storage.form.arnHint"))}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label className="v3-field">
            <span>{t("v3.storage.form.maxMode")}</span>
            <Select className="v3-select" value={form.max_mode} onChange={(e) => set("max_mode", e.target.value as StorageForm["max_mode"])}>
              {STORAGE_MODES.map((m) => <option key={m} value={m}>{t(`v3.storage.mode.${m}`)}</option>)}
            </Select>
          </label>
          <label className="v3-field">
            <span>{t("v3.storage.form.dataClass")}</span>
            <Select className="v3-select" value={form.data_class} onChange={(e) => set("data_class", e.target.value as StorageForm["data_class"])}>
              {STORAGE_DATA_CLASSES.map((c) => <option key={c} value={c}>{t(`v3.storage.dataClass.${c}`)}</option>)}
            </Select>
          </label>
        </div>
        <label className="v3-field">
          <span>{t("v3.storage.form.profile")}</span>
          <Select className="v3-select" value={form.network_profile_id} onChange={(e) => set("network_profile_id", e.target.value)}>
            <option value="">{t("v3.storage.form.noProfile")}</option>
            {profiles.filter((p) => !p.archived_at).map((p) => <option key={p.id} value={p.id}>{p.name} ({p.id})</option>)}
          </Select>
          <small className="v3-hint">{t("v3.storage.form.profileHint")}</small>
        </label>
        {form.max_mode === "rw" && <Notice s="info">{t("v3.storage.form.rwHint")}</Notice>}
        {error && <Notice s="act">{error}</Notice>}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Dialog>
  );
}
